"""desktop.routes_blm — BLM (银行对账单匹配) 工作流端点。"""

from __future__ import annotations

import csv
import json
import os

import yaml as yaml_lib
from fastapi import APIRouter, HTTPException, Form

from .common import (
    logger,
    token_tracker,
    _project_root,
    _default_llm_yml_path,
    _default_matching_yml_path,
    _resolve_task_config_paths,
    _upsert_task_config,
    _workflow_state,
    _resolve_workflow_state_key,
    _load_llm_config,
)
from audit_workflow.bank_ledger_match import pipeline as blm_pipeline
from audit_workflow.bank_ledger_match import file_detector

router = APIRouter()


def _build_full_config(customer_name: str, task_name: str) -> dict:
    """构建完整的 pipeline 配置：llm.yml + task.yml。
    优先从 task_configs 表查询 yml 路径，找不到则用默认路径。
    LLM 开关完全由前端传入的 parser 参数控制，不再从 task.yml 读取。
    """
    paths = _resolve_task_config_paths(customer_name, task_name)

    # 加载 llm.yml
    llm_cfg = {}
    if os.path.exists(paths["llm_yml_path"]):
        with open(paths["llm_yml_path"], "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}

    matching_yml_path = _default_matching_yml_path()
    if os.path.exists(matching_yml_path):
        with open(matching_yml_path, "r", encoding="utf-8") as f:
            matching_cfg = yaml_lib.safe_load(f) or {}
        llm_cfg.setdefault("llm", {})
        llm_cfg["llm"]["matching"] = matching_cfg.get("matching", {})

    # 加载 task.yml
    task_cfg = {}
    if os.path.exists(paths["task_yml_path"]):
        with open(paths["task_yml_path"], "r", encoding="utf-8") as f:
            task_cfg = yaml_lib.safe_load(f) or {}

    cfg = file_detector.merge_llm_into_full_config(task_cfg, llm_cfg)

    return cfg


def _apply_parser_to_config(cfg: dict, parser: str) -> None:
    """根据前端传入的 parser 参数调整 config 中的 LLM 开关。
    parser 以 'llm' 开头 → 启用 LLM；否则关闭 LLM。
    同时设置 matching.llm.enabled（用于 LLM 补充匹配）。
    """
    if parser:
        llm_on = parser.startswith("llm")
        cfg.setdefault("llm", {})["enabled"] = llm_on
        cfg.setdefault("matching", {}).setdefault("llm", {})
        cfg["matching"]["llm"]["enabled"] = llm_on


@router.post("/workflow/bank_ledger_match/match")
def workflow_match(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
):
    logger.info("开始 Match: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    _apply_parser_to_config(cfg, parser)
    try:
        matches, unmatched_bank, unmatched_ledger = blm_pipeline.run_match(cfg)
        return {"matches": str(matches), "unmatched_bank": str(unmatched_bank), "unmatched_ledger": str(unmatched_ledger)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/approve")
def workflow_approve(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        a, b, c, d = blm_pipeline.run_approve(cfg)
        return {"result": [str(x) for x in (a, b, c, d)]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/verify")
def workflow_verify(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
):
    """Step - Verify: 将人工复核通过的项目加入 matches.csv，从 unmatched 中移除。"""
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    _apply_parser_to_config(cfg, parser)
    try:
        if not cfg:
            raise HTTPException(status_code=400, detail="缺少配置参数")
        # 调用 approver 执行实际的人工复核合并逻辑
        matches_path, unmatched_bank_path, unmatched_ledger_path, review_path = blm_pipeline.run_approve(cfg)
        # 统计行数
        def _count_rows(p):
            if not p.exists():
                return 0
            with open(p, "r", encoding="utf-8", errors="ignore", newline="") as f:
                return max(sum(1 for _ in csv.reader(f)) - 1, 0)
        result = {
            "ok": True,
            "matches": {"path": str(matches_path), "rows": _count_rows(matches_path)},
            "unmatched_bank": {"path": str(unmatched_bank_path), "rows": _count_rows(unmatched_bank_path)},
            "unmatched_ledger": {"path": str(unmatched_ledger_path), "rows": _count_rows(unmatched_ledger_path)},
            "review": {"path": str(review_path), "rows": _count_rows(review_path)},
        }
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/detect")
async def workflow_detect(
    customer_name: str = Form(...),
    task_name: str = Form("bank_ledger_match"),
    use_llm: str = Form("true"),
):
    """
    Step 1 - Detect: 扫描 inputs/{customer}/{task}/ 下的文件，
                     用 LLM 自动识别文件类型、银行、时间段，生成 task.yml。
    use_llm: "true" → LLM 识别, "false" → 本地脚本/关键词识别。
    """
    logger.info("开始 Detect: customer=%s, task=%s, use_llm=%s", customer_name, task_name, use_llm)
    try:
        # 1. 扫描文件
        root_dir = _project_root()
        inputs_dir = os.path.join(root_dir, "inputs")
        files = file_detector.scan_input_files(inputs_dir, customer_name, task_name)

        if not files:
            return {
                "ok": False,
                "error": f"在 inputs/{customer_name}/{task_name}/ 下未找到任何文件。请先在数据源页面上传文件。",
                "files": [],
                "identifications": [],
            }

        # 2. 项目信息
        project_info = {
            "name": customer_name,
            "client_name": customer_name,
            "task": task_name,
            "audit_year": 2022,
        }

        # 3. 识别文件
        use_llm_flag = str(use_llm).lower() in ("1", "true", "yes", "on")
        llm_cfg = _load_llm_config()
        llm_enabled = llm_cfg.get("llm", {}).get("enabled", False) and use_llm_flag
        llm_error = None

        if llm_enabled:
            try:
                identifications, detect_usage = await file_detector.identify_files_with_llm_async(
                    files,
                    llm_cfg.get("llm", {}),
                    project_info,
                )
                token_tracker.record(detect_usage)
            except Exception as llm_exc:
                # LLM 调用失败：记录错误原因，回退到本地识别
                import traceback
                llm_error = {
                    "type": type(llm_exc).__name__,
                    "message": str(llm_exc),
                    "traceback": traceback.format_exc(),
                }
                identifications = file_detector._identify_files_local(files, project_info)
        else:
            identifications = file_detector._identify_files_local(files, project_info)

        # 4. 生成 task.yml 配置
        task_cfg = file_detector.generate_task_config(
            identifications, project_info
        )

        # 5. 保存到 outputs/{customer}/{task}/task.yml
        task_yml_path = os.path.join(
            root_dir, "outputs", customer_name, task_name, "task.yml"
        )
        saved_path = file_detector.save_task_config(task_cfg, task_yml_path)

        # 写入 DB 映射：公司 + 任务 → yml 路径
        _upsert_task_config(
            customer_name, task_name,
            task_yml_path=str(saved_path),
            llm_yml_path=_default_llm_yml_path(),
        )

        # 6. 保存状态
        key = _resolve_workflow_state_key(customer_name, task_name)
        _workflow_state[key] = {
            "files": files,
            "identifications": identifications,
            "task_config": task_cfg,
            "task_yml_path": str(saved_path),
            "llm_used": llm_enabled,
        }

        # 去掉 excel_preview 减少返回体积（那是给 LLM 看的 prompt 数据）
        files_light = [{k: v for k, v in f.items() if k != "excel_preview"} for f in files]

        logger.info("Detect 完成: customer=%s, task=%s, files=%d, llm_used=%s", customer_name, task_name, len(files), llm_enabled)
        return {
            "ok": True,
            "files_count": len(files),
            "files": files_light,
            "identifications": identifications,
            "task_config": task_cfg,
            "task_yml_path": str(saved_path),
            "llm_used": llm_enabled,
            "llm_error": llm_error,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflow/config")
def workflow_get_config(
    customer_name: str = "",
    task_name: str = "bank_ledger_match",
):
    """获取已生成的 task.yml 配置内容。"""
    paths = _resolve_task_config_paths(customer_name, task_name)
    task_yml_path = paths["task_yml_path"]
    if not os.path.exists(task_yml_path):
        return {"ok": False, "error": "尚未生成配置，请先执行 Detect 步骤。", "content": ""}

    with open(task_yml_path, "r", encoding="utf-8") as f:
        content = f.read()

    return {"ok": True, "content": content, "path": task_yml_path}


@router.post("/workflow/config/save")
def workflow_save_config(
    customer_name: str = Form(...),
    task_name: str = Form("bank_ledger_match"),
    content: str = Form(...),
):
    """
    Step 2 - Confirm: 前端用户确认/修改 task.yml 后保存。
    """
    logger.info("保存配置: customer=%s, task=%s", customer_name, task_name)
    try:
        paths = _resolve_task_config_paths(customer_name, task_name)
        task_yml_path = paths["task_yml_path"]
        os.makedirs(os.path.dirname(task_yml_path), exist_ok=True)

        # 验证 YAML 语法
        try:
            parsed = yaml_lib.safe_load(content)
        except yaml_lib.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"YAML 语法错误: {e}")

        with open(task_yml_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 同步 DB 映射
        _upsert_task_config(
            customer_name, task_name,
            task_yml_path=task_yml_path,
            llm_yml_path=paths["llm_yml_path"],
        )

        # 更新状态
        key = _resolve_workflow_state_key(customer_name, task_name)
        _workflow_state[key] = {
            **_workflow_state.get(key, {}),
            "task_config": parsed,
            "task_yml_path": task_yml_path,
            "confirmed": True,
        }

        return {
            "ok": True,
            "path": task_yml_path,
            "parsed": parsed,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/clean")
def workflow_bank_ledger_match_clean(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """
    Step 3 - Clean: 用 task.yml 配置执行清洗。
    """
    logger.info("开始 Clean: customer=%s, task=%s", customer_name, task_name)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        bank_csv, ledger_csv = blm_pipeline.run_clean(cfg)
        return {"bank_csv": str(bank_csv), "ledger_csv": str(ledger_csv)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/clean_bank")
def workflow_bank_ledger_match_clean_bank(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
):
    """
    Clean Bank: 仅清洗银行流水，parser 由前端传入。
    """
    logger.info("开始 Clean Bank: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    _apply_parser_to_config(cfg, parser)
    try:
        bank_csv = blm_pipeline.run_clean_bank(cfg, parser=parser or None)
        return {"bank_csv": str(bank_csv)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/clean_ledger")
def workflow_bank_ledger_match_clean_ledger(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
):
    """
    Clean Ledger: 仅清洗序时账，parser 由前端传入。
    """
    logger.info("开始 Clean Ledger: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    _apply_parser_to_config(cfg, parser)
    try:
        ledger_csv = blm_pipeline.run_clean_ledger(cfg, parser=parser or None)
        return {"ledger_csv": str(ledger_csv)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/check")
def workflow_bank_ledger_match_check(
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """
    Step 4 - Check: 读取 monthly_flow_check.csv 返回数据完备性报告。
    检查银行流水和序时账按月/账号的流入流出是否一致。
    """
    logger.info("开始 Check: customer=%s, task=%s", customer_name, task_name)
    try:
        check_path = os.path.join(
            _project_root(), "outputs", customer_name, task_name,
            "matches", "monthly_flow_check.csv",
        )
        if not os.path.exists(check_path):
            return {
                "ok": False,
                "error": "monthly_flow_check.csv 不存在。请先执行 Match 步骤生成该文件，或直接运行 Clean → Match。",
                "summary": {"total_rows": 0, "ok_count": 0, "mismatch_count": 0},
                "rows": [],
            }

        with open(check_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        summary = {
            "total_rows": len(rows),
            "ok_count": sum(1 for r in rows if r.get("status", "") == "ok"),
            "mismatch_count": sum(1 for r in rows if r.get("status", "") == "mismatch"),
        }

        mismatches = [r for r in rows if r.get("status", "") == "mismatch"]

        return {
            "ok": True,
            "summary": summary,
            "all_ok": summary["mismatch_count"] == 0,
            "rows": rows[:200],  # 限制返回行数
            "mismatches": mismatches[:50],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/fill")
def workflow_bank_ledger_match_fill(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    logger.info("开始 Fill: customer=%s, task=%s", customer_name, task_name)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        path = blm_pipeline.run_fill(cfg)
        logger.info("Fill 完成: %s", path)
        return {"working_paper": str(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/fill_llm")
def workflow_bank_ledger_match_fill_llm(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """使用 LLM 生成填表代码并执行，自适应任意模板布局。"""
    logger.info("开始 Fill (LLM): customer=%s, task=%s", customer_name, task_name)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        path, usage = blm_pipeline.run_fill_llm(cfg)
        token_tracker.record(usage)
        return {"working_paper": str(path), "usage": usage}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
