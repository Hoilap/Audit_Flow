"""desktop.routes_osm — OSM（出库结算匹配）工作流端点。"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Form

from .common import (
    logger,
    token_tracker,
    _project_root,
    _default_llm_config,
)
from audit_workflow.outbound_settlement_match import pipeline as osm_pipeline

router = APIRouter()


def _build_osm_config(customer_name: str, task_name: str) -> dict:
    """Build configuration dict for outbound_settlement_match pipeline.

    Constructs project metadata and paths so the pipeline can locate
    inputs and outputs without requiring a task.yml file.  Also injects
    LLM configuration so the cleaning steps can use LLM-generated scripts.
    """
    root = _project_root()

    # Inject LLM config so pipeline can use LLM-generated cleaning scripts
    llm_cfg: dict = {}
    try:
        llm_cfg = _default_llm_config()
    except Exception as e:
        logger.warning("Failed to load LLM config for OSM: %s", e)

    return {
        "project": {
            "customer_name": customer_name,
            "task_name": task_name,
            "inputs_dir": os.path.join(root, "inputs", customer_name, task_name),
            "output_dir": os.path.join(root, "outputs", customer_name, task_name),
        },
        "matching": {
            "outbound_order_id_key": "order_id",
            "settlement_txn_id_key": "partner_txn_id",
        },
        "llm": llm_cfg,
        "_project_root": root,
        "_root": root,
    }


def _apply_parser_to_config(cfg: dict, parser: str) -> None:
    """根据前端传入的 parser 参数配置 LLM 开关和解析器选择。

    parser 取值:
      - "llm"            → 使用 LLM 缓存的解析器（有缓存直接用，无则生成）
      - "llm_regenerate" → 跳过缓存检查，重新调 LLM 生成解析器
      - "script"         → 使用硬编码脚本解析
    """
    if not parser:
        return
    cfg["parser"] = parser
    is_llm = parser in ("llm", "llm_regenerate")
    cfg.setdefault("llm", {})["enabled"] = is_llm
    # llm_regenerate: 设置强制重新生成标志，ensure_* 函数跳过缓存
    if parser == "llm_regenerate":
        cfg["_force_regenerate"] = True


@router.post("/workflow/outbound_settlement_match/detect")
def workflow_osm_detect(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Step 1 - Detect: Scan inputs, classify settlement files and outbound sheets."""
    logger.info("OSM Detect: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        result = osm_pipeline.run_detect(cfg)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/outbound_settlement_match/clean_settlement")
def workflow_osm_clean_settlement(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
    parser: str = Form(""),
    requirement: str = Form(""),
):
    """OSM Step 2 - Clean Settlement: Aggregate platform settlement CSVs."""
    logger.info("OSM Clean Settlement: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        _apply_parser_to_config(cfg, parser)
        if requirement:
            cfg["_user_requirement"] = requirement
        settlement_path, summary_path = osm_pipeline.run_clean_settlement(cfg)
        token_tracker.record_from_config(cfg, "settlement")
        cleaning_info = cfg.get("_cleaning", {}).get("settlement", {})
        return {
            "ok": True,
            "settlement_csv": str(settlement_path),
            "monthly_summary": str(summary_path),
            "cleaning_mode": cleaning_info.get("mode", "hardcoded"),
            "fallback_reason": cleaning_info.get("fallback_reason"),
            "file_fallbacks": cleaning_info.get("file_fallbacks", []),
            "usage": cleaning_info.get("usage", {}),
            "script_dir": cleaning_info.get("script_dir"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/outbound_settlement_match/clean_outbound")
def workflow_osm_clean_outbound(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
    parser: str = Form(""),
    requirement: str = Form(""),
):
    """OSM Step 3 - Clean Outbound: Parse Excel sheets into standardized CSVs."""
    logger.info("OSM Clean Outbound: customer=%s, task=%s, parser=%s", customer_name, task_name, parser)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        _apply_parser_to_config(cfg, parser)
        if requirement:
            cfg["_user_requirement"] = requirement
        paths = osm_pipeline.run_clean_outbound(cfg)
        token_tracker.record_from_config(cfg, "outbound")
        cleaning_info = cfg.get("_cleaning", {}).get("outbound", {})
        return {
            "ok": True,
            "paths": {k: str(v) if v else None for k, v in paths.items()},
            "cleaning_mode": cleaning_info.get("mode", "hardcoded"),
            "fallback_reason": cleaning_info.get("fallback_reason"),
            "file_fallbacks": cleaning_info.get("file_fallbacks", []),
            "sheet_tasks": cleaning_info.get("sheet_tasks", []),
            "usage": cleaning_info.get("usage", {}),
            "script_dir": cleaning_info.get("script_dir"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/outbound_settlement_match/clean_outbound_sheet")
def workflow_osm_clean_outbound_sheet(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
    file: str = Form(...),
    sheet: str = Form(...),
    sheet_type: str = Form(...),
    column_signature: str = Form(""),
    force_regenerate: bool = Form(False),
    parser: str = Form(""),
    requirement: str = Form(""),
):
    """OSM: Retry cleaning a single outbound sheet.

    Re-generates (or reuses cached) LLM script for one sheet and runs it.
    Returns per-sheet status, script name, row count, and token usage.
    """
    logger.info(
        "OSM Clean Sheet: customer=%s, file=%s, sheet=%s, type=%s, force=%s",
        customer_name, file, sheet, sheet_type, force_regenerate,
    )
    try:
        import pandas as pd
        from audit_workflow.outbound_settlement_match.llm_cleaner import (
            ensure_llm_outbound_cleaner,
            safe_run_cleaner,
            compute_column_signature,
            patch_rename_dedup,
        )
        from audit_workflow.outbound_settlement_match.outbound_cleaner import (
            _read_sheet_auto_header,
        )

        cfg = _build_osm_config(customer_name, task_name)
        _apply_parser_to_config(cfg, parser)
        if requirement:
            cfg["_user_requirement"] = requirement
        # force_regenerate 表单参数也触发强制重新生成
        if force_regenerate:
            cfg["_force_regenerate"] = True
        inputs = Path(cfg["project"]["inputs_dir"])
        outbound_dir = inputs / "outbound"
        xlsx_path = outbound_dir / file

        if not xlsx_path.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在: {file}")

        # Read sheet and compute column signature
        xl = pd.ExcelFile(str(xlsx_path), engine="openpyxl")
        df = _read_sheet_auto_header(xl, sheet)
        xl.close()

        if df.empty:
            return {"ok": True, "status": "failed", "error": "工作表为空", "rows": 0}

        df.columns = [str(c).strip() for c in df.columns]
        col_sig = column_signature or compute_column_signature(list(df.columns))

        # Generate / get cached script
        script_path, usage = ensure_llm_outbound_cleaner(
            cfg, xlsx_path, sheet, sheet_type, col_sig,
        )
        token_tracker.record(usage)

        # Run the script
        month = ""
        import re as _re
        m = _re.search(r"(\d{1,2})月", file)
        if m:
            month = f"2022-{int(m.group(1)):02d}"

        try:
            records = safe_run_cleaner(
                script_path, str(xlsx_path), sheet, file, month, sheet_type,
            )
        except Exception as run_err:
            err_str = str(run_err)
            # Auto-patch: if the error is "truth value of a Series is ambiguous",
            # it means the generated script has duplicate column names after rename.
            # Inject a dedup line right after the rename call and retry.
            if "truth value" in err_str.lower() or "series" in err_str.lower():
                logger.warning("Script %s has post-rename duplicate columns, auto-patching...", script_path.name)
                original_code = script_path.read_text(encoding="utf-8")
                patched_code = patch_rename_dedup(original_code)
                if patched_code != original_code:
                    script_path.write_text(patched_code, encoding="utf-8")
                    try:
                        records = safe_run_cleaner(
                            script_path, str(xlsx_path), sheet, file, month, sheet_type,
                        )
                        logger.info("Auto-patch succeeded for %s", script_path.name)
                    except Exception as patch_err:
                        logger.warning("Auto-patch also failed: %s", patch_err)
                        return {
                            "ok": True,
                            "status": "failed",
                            "script_name": script_path.name,
                            "rows": 0,
                            "error": f"原始错误: {err_str}\n修补后错误: {patch_err}",
                        }
                else:
                    return {
                        "ok": True,
                        "status": "failed",
                        "script_name": script_path.name,
                        "rows": 0,
                        "error": f"无法自动修补脚本: {err_str}",
                    }
            else:
                raise

        # Success — update persisted sheet_tasks.json and return refreshed totals
        import json as _json
        from audit_workflow.outbound_settlement_match.config import output_dir as _output_dir
        clean_dir = _output_dir(cfg) / "clean"
        st_path = clean_dir / "sheet_tasks.json"
        total_usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}

        # Update the matching entry in persisted sheet_tasks
        if st_path.exists():
            try:
                persisted = _json.loads(st_path.read_text(encoding="utf-8"))
                for task in persisted.get("sheet_tasks", []):
                    if task["file"] == file and task["sheet"] == sheet:
                        task["status"] = "llm_success"
                        task["script_name"] = script_path.name
                        task["column_signature"] = col_sig
                        task["rows"] = len(records)
                        task["error"] = None
                    # Accumulate usage from all tasks (approximate: cached=0 for old entries)
                # Add the new retry's usage to persisted totals
                old_usage = persisted.get("usage", {})
                for k in total_usage:
                    total_usage[k] = old_usage.get(k, 0) + usage.get(k, 0)
                persisted["usage"] = total_usage
                st_path.write_text(
                    _json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as persist_err:
                logger.warning("Failed to update sheet_tasks.json: %s", persist_err)
        else:
            # No persisted file — just accumulate this retry's usage
            total_usage = dict(usage)

        return {
            "ok": True,
            "status": "llm_success",
            "script_name": script_path.name,
            "column_signature": col_sig,
            "rows": len(records),
            "usage": usage,
            "total_usage": total_usage,
            "error": None,
        }
    except Exception as e:
        logger.warning("Clean sheet failed: %s", e)
        return {
            "ok": True,
            "status": "failed",
            "script_name": None,
            "rows": 0,
            "error": str(e),
        }


@router.post("/workflow/outbound_settlement_match/match")
def workflow_osm_match(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Step 4 - Match: Filter net outbound and match against settlement by ID."""
    logger.info("OSM Match: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        result = osm_pipeline.run_match(cfg)

        def _count_rows(p):
            if not p or not Path(p).exists():
                return 0
            with open(p, "r", encoding="utf-8", errors="ignore", newline="") as f:
                return max(sum(1 for _ in csv.reader(f)) - 1, 0)

        return {
            "ok": True,
            "summary": result["summary"],
            "net_outbound": {"path": str(result["net_outbound"]), "rows": _count_rows(result["net_outbound"])},
            "matched": {"path": str(result["matched"]), "rows": _count_rows(result["matched"])},
            "unmatched_outbound": {"path": str(result["unmatched_outbound"]), "rows": _count_rows(result["unmatched_outbound"])},
            "unmatched_settlement": {"path": str(result["unmatched_settlement"]), "rows": _count_rows(result["unmatched_settlement"])},
            "monthly_summary": {"path": str(result["monthly_summary"]), "rows": _count_rows(result["monthly_summary"])},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/outbound_settlement_match/run_all")
def workflow_osm_run_all(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Full Pipeline: Detect → Clean → Match (one-shot)."""
    logger.info("OSM Run All: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        result = osm_pipeline.run_all(cfg)
        token_tracker.record_from_config(cfg, "settlement", "outbound")
        cleaning = cfg.get("_cleaning", {})
        return {
            "ok": True,
            "summary": result.get("match", {}).get("summary", {}),
            "detect": {
                "settlement_files": len(result.get("detect", {}).get("settlement_files", [])),
                "outbound_files": len(result.get("detect", {}).get("outbound_files", [])),
            },
            "cleaning_settlement": cleaning.get("settlement", {}),
            "cleaning_outbound": cleaning.get("outbound", {}),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
