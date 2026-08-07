"""desktop.routes_blm — BLM (银行对账单匹配) 工作流端点。"""

from __future__ import annotations

import csv
import json
import os
import traceback
from typing import Any

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
    notify_frontend,
)
from audit_workflow.bank_ledger_match import pipeline as blm_pipeline
from audit_workflow.bank_ledger_match import file_detector
from audit_workflow.bank_ledger_match.llm_cleaner import normalize_ledger_columns

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
    """根据前端传入的 parser 参数配置 LLM 开关和解析器选择。

    parser 取值:
      - "llm_step/llm_step_all" → 使用 LLM 缓存的解析器（有缓存直接用，无则生成）
                       → 匹配时跳过已匹配记录，仅对未匹配记录进行 LLM 辅助匹配
                       → 不去重，所有候选送 LLM（适合复杂/重叠候选场景）
      - "llm_step_once" → 同 llm_step，但仅将无重复的单一候选送 LLM（简单样本，节省 token）
      - "llm_init" → 跳过缓存检查，重新调 LLM 生成解析器
                       → 规则匹配始终执行，LLM 不复用决策缓存
                       → 去重，仅保留无重复候选
      - "script"         → 使用硬编码脚本解析，不启用 LLM 辅助匹配
      - 其他具体解析器名  → 直接使用（如 icbc_historydetail, xinjiyuan_bank_ledger）
    """
    if not parser:
        return
    cfg["parser"] = parser
    is_llm = parser in ("llm", "llm_regenerate", "llm_init", "llm_step", "llm_step_once", "llm_step_all")
    cfg.setdefault("llm", {})["enabled"] = is_llm
    cfg.setdefault("matching", {}).setdefault("llm", {})
    cfg["matching"]["llm"]["enabled"] = is_llm

    # llm_step_once / llm_regenerate / llm_init → 去重，仅保留无重复候选（简单样本）
    # llm_step_all / llm / llm_step → 不去重，所有候选送 LLM（全面匹配）
    cfg["matching"]["llm"]["deduplicate_candidates"] = (parser in ("llm_init", "llm_step_once"))

    # reuse_decisions: 控制是否复用 llm_decisions.csv 缓存
    # llm_step_once / llm_step_all → 复用缓存（节省 LLM 调用）
    # 其他 parser → 不复用，每次重新调用 LLM
    cfg["matching"]["llm"]["reuse_decisions"] = (parser in ("llm_step_once", "llm_step_all"))
    
    # llm_regenerate / llm_init: 设置强制重新生成标志，ensure_* 函数跳过缓存
    if parser in ("llm_regenerate", "llm_init"):
        cfg["_force_regenerate"] = True


def _normalize_parser_for_bank(parser: str) -> str:
    """将前端 parser 值映射为 bank cleaner 可识别的名称。
    llm / llm_regenerate / llm_init / llm_step → llm_bank
    """
    if parser in ("llm", "llm_regenerate", "llm_init", "llm_step", "llm_step_once", "llm_step_all"):
        return "llm_bank"
    return parser


def _normalize_parser_for_ledger(parser: str) -> str:
    """将前端 parser 值映射为 ledger cleaner 可识别的名称。
    llm / llm_regenerate / llm_init / llm_step → llm_ledger
    """
    if parser in ("llm", "llm_regenerate", "llm_init", "llm_step", "llm_step_once", "llm_step_all"):
        return "llm_ledger"
    return parser


@router.post("/workflow/bank_ledger_match/match")
def workflow_match(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
    requirement: str = Form(""),
):
    logger.info("开始 Match: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    _apply_parser_to_config(cfg, parser)
    if requirement:
        cfg["_user_requirement"] = requirement
    try:
        matches, unmatched_bank, unmatched_ledger, stats = blm_pipeline.run_match(cfg)
        notify_frontend("match_completed", stats)
        return {"matches": str(matches), "unmatched_bank": str(unmatched_bank), "unmatched_ledger": str(unmatched_ledger), "stats": stats}
    except Exception as e:
        logger.error("Match 失败: customer=%s, task=%s, error=%s\n%s", customer_name, task_name, e, traceback.format_exc())
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
        logger.error("Approve 失败: error=%s\n%s", e, traceback.format_exc())
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
        logger.error("Verify 失败: customer=%s, task=%s, error=%s\n%s", customer_name, task_name, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/detect")
async def workflow_detect(
    customer_name: str = Form(...),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
    use_llm: str = Form("true"),
    requirement: str = Form(""),
):
    """
    Step 1 - Detect: 扫描 inputs/{customer}/{task}/ 下的文件，
                     根据子目录结构 (bank/ledger/working paper) 自动分类。
    parser:
      - "llm" / "llm_regenerate" → 目录分类 + LLM 提取元数据（银行名称、账号、日期）
      - 其他 / 空 → 目录分类 + 脚本提取元数据（关键词 + 预览）
    """
    # 统一 parser 参数：优先用 parser，否则回退到旧的 use_llm
    if not parser:
        parser = "llm" if str(use_llm).lower() in ("1", "true", "yes", "on") else "script"
    use_llm_flag = parser in ("llm", "llm_regenerate", "llm_init", "llm_step", "llm_step_once", "llm_step_all")
    logger.info("开始 Detect: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
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
                "warnings": [],
            }

        # 2. 项目信息
        project_info = {
            "name": customer_name,
            "client_name": customer_name,
            "task": task_name,
            "audit_year": 2022,
        }

        # 3. 识别文件（目录分类 + 元数据提取）
        scan_dir = os.path.join(inputs_dir, customer_name, task_name)
        llm_error = None
        detect_usage = {}

        if use_llm_flag:
            llm_cfg = _load_llm_config()
            llm_inner = llm_cfg.get("llm", {})
            if llm_inner.get("enabled", False):
                try:
                    identifications, warnings, detect_usage = await file_detector.identify_files_with_llm_async(
                        files, llm_inner, project_info, base_dir=scan_dir,
                    )
                    token_tracker.record(detect_usage)
                except Exception as llm_exc:
                    import traceback
                    llm_error = {
                        "type": type(llm_exc).__name__,
                        "message": str(llm_exc),
                        "traceback": traceback.format_exc(),
                    }
                    # LLM 失败不兜底，直接返回错误
                    files_light = [{k: v for k, v in f.items() if k != "excel_preview"} for f in files]
                    logger.warning("Detect LLM 失败: customer=%s, error=%s", customer_name, llm_exc)
                    return {
                        "ok": False,
                        "error": f"LLM 元数据提取失败: {llm_exc}",
                        "files_count": len(files),
                        "files": files_light,
                        "identifications": [],
                        "task_config": {},
                        "task_yml_path": "",
                        "llm_used": True,
                        "llm_error": llm_error,
                        "warnings": [],
                    }
            else:
                # LLM 未启用，回退到 script
                identifications, warnings = file_detector._identify_files_local(
                    files, project_info, base_dir=scan_dir,
                )
                use_llm_flag = False
        else:
            identifications, warnings = file_detector._identify_files_local(
                files, project_info, base_dir=scan_dir,
            )

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
            "llm_used": use_llm_flag,
            "user_requirement": requirement,
            "warnings": warnings,
        }

        # 去掉 excel_preview 减少返回体积
        files_light = [{k: v for k, v in f.items() if k != "excel_preview"} for f in files]

        logger.info("Detect 完成: customer=%s, task=%s, files=%d, llm=%s, warnings=%d",
                     customer_name, task_name, len(files), use_llm_flag, len(warnings))
        return {
            "ok": True,
            "files_count": len(files),
            "files": files_light,
            "identifications": identifications,
            "task_config": task_cfg,
            "task_yml_path": str(saved_path),
            "llm_used": use_llm_flag,
            "llm_error": llm_error,
            "warnings": warnings,
        }
    except Exception as e:
        logger.error("Detect 失败: customer=%s, task=%s, error=%s\n%s", customer_name, task_name, e, traceback.format_exc())
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
        logger.error("保存配置失败: customer=%s, task=%s, error=%s\n%s", customer_name, task_name, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/clean")
def workflow_bank_ledger_match_clean(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """
    Step 3 - Clean: 用 task.yml 配置执行清洗（bank + ledger 一起）。
    parser 由 task.yml 中每个 input item 的配置决定，不接受前端覆盖。
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
        resp: dict[str, Any] = {"bank_csv": str(bank_csv), "ledger_csv": str(ledger_csv)}
        warnings = cfg.get("_cleaning", {}).get("warnings", [])
        if warnings:
            resp["warnings"] = warnings
        return resp
    except Exception as e:
        logger.error("Clean 失败: customer=%s, task=%s, error=%s\n%s", customer_name, task_name, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/clean_bank")
def workflow_bank_ledger_match_clean_bank(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
    requirement: str = Form(""),
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
    if requirement:
        cfg["_user_requirement"] = requirement
    try:
        effective_parser = _normalize_parser_for_bank(parser)
        bank_csv = blm_pipeline.run_clean_bank(cfg, parser=effective_parser or None)
        resp: dict[str, Any] = {"bank_csv": str(bank_csv)}
        warnings = cfg.get("_cleaning", {}).get("warnings", [])
        if warnings:
            resp["warnings"] = warnings
        return resp
    except Exception as e:
        logger.error("Clean Bank 失败: customer=%s, task=%s, parser=%s, error=%s\n%s", customer_name, task_name, parser, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/clean_ledger")
def workflow_bank_ledger_match_clean_ledger(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
    requirement: str = Form(""),
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
    if requirement:
        cfg["_user_requirement"] = requirement
    try:
        effective_parser = _normalize_parser_for_ledger(parser)
        ledger_csv = blm_pipeline.run_clean_ledger(cfg, parser=effective_parser or None)
        resp: dict[str, Any] = {"ledger_csv": str(ledger_csv)}
        warnings = cfg.get("_cleaning", {}).get("warnings", [])
        if warnings:
            resp["warnings"] = warnings
        # ── Phase 2: 列格式检查 + LLM 替换 ──
        normalization = normalize_ledger_columns(cfg, ledger_csv)
        if normalization:
            resp["normalization"] = normalization
        return resp
    except Exception as e:
        logger.error("Clean Ledger 失败: customer=%s, task=%s, parser=%s, error=%s\n%s", customer_name, task_name, parser, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/check")
def workflow_bank_ledger_match_check(
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """
    Step 4 - Check: 基于当前 clean 数据实时重新生成 monthly_flow_check.csv，
    然后返回数据完备性报告。检查银行流水和序时账按月/账号的流入流出是否一致。
    """
    logger.info("开始 Check: customer=%s, task=%s", customer_name, task_name)
    try:
        # 构建配置并重新生成 monthly_flow_check.csv
        if customer_name:
            cfg = _build_full_config(customer_name, task_name)
        else:
            cfg = {}

        # 检查 clean 数据是否存在
        out_dir = os.path.join(
            _project_root(), "outputs", customer_name, task_name
        )
        bank_csv = os.path.join(out_dir, "clean", "bank_transactions.csv")
        ledger_csv = os.path.join(out_dir, "clean", "ledger_entries.csv")
        if not os.path.exists(bank_csv) and not os.path.exists(ledger_csv):
            return {
                "ok": False,
                "error": "清洗数据不存在。请先执行 Clean 步骤生成 bank_transactions.csv 和 ledger_entries.csv。",
                "summary": {"total_rows": 0, "ok_count": 0, "mismatch_count": 0},
                "rows": [],
            }

        # 基于当前 clean 数据重新生成
        blm_pipeline.run_check(cfg)

        check_path = os.path.join(out_dir, "matches", "monthly_flow_check.csv")
        if not os.path.exists(check_path):
            return {
                "ok": False,
                "error": "monthly_flow_check.csv 生成失败。",
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
        logger.error("Check 失败: customer=%s, task=%s, error=%s\n%s", customer_name, task_name, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/fill")
def workflow_bank_ledger_match_fill(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    parser: str = Form(""),
    requirement: str = Form(""),
):
    """Step 8 - Fill: 将匹配结果填入工作底稿。
    parser:
      - "llm" / "llm_regenerate" / "llm_init" / "llm_step" → LLM 自适应填表
      - "script" / 其他 / 空       → 脚本填表（硬编码列映射）
    """
    use_llm = parser in ("llm", "llm_regenerate", "llm_init", "llm_step", "llm_step_once", "llm_step_all")
    logger.info("开始 Fill: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        if use_llm:
            path, usage = blm_pipeline.run_fill_llm(cfg)
            token_tracker.record(usage)
            return {"working_paper": str(path), "llm_used": True, "usage": usage}
        else:
            path = blm_pipeline.run_fill(cfg)
            return {"working_paper": str(path), "llm_used": False}
    except Exception as e:
        logger.error("Fill 失败: customer=%s, task=%s, parser=%s, error=%s\n%s", customer_name, task_name, parser, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/bank_ledger_match/fill_llm")
def workflow_bank_ledger_match_fill_llm(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
    requirement: str = Form(""),
):
    """[已废弃] 请使用 /fill 并传 parser=llm。保留向后兼容。"""
    return workflow_bank_ledger_match_fill(
        config=config,
        customer_name=customer_name,
        task_name=task_name,
        parser="llm",
        requirement=requirement,
    )
