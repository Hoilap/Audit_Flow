from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .approver import apply_manual_approvals
from .cleaners import clean_to_csv, clean_bank_to_csv, clean_ledger_to_csv
from .config import output_dir
from .matcher import match_to_csv, write_monthly_flow_check
from .filler import fill_working_paper
from .llm_filler import fill_working_paper_llm


def run_clean(config: dict[str, Any]) -> tuple[Path, Path]:
    return clean_to_csv(config)


def run_clean_bank(config: dict[str, Any], parser: str | None = None) -> Path:
    """仅清洗银行流水，parser 可由前端传入覆盖 task.yml 配置。"""
    return clean_bank_to_csv(config, parser=parser or None)


def run_clean_ledger(config: dict[str, Any], parser: str | None = None) -> Path:
    """仅清洗序时账，parser 可由前端传入覆盖 task.yml 配置。"""
    return clean_ledger_to_csv(config, parser=parser or None)


def run_check(config: dict[str, Any]) -> Path:
    """基于当前 clean 数据重新生成 monthly_flow_check.csv。"""
    return write_monthly_flow_check(config)


def run_match(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    return match_to_csv(config)


def run_approve(config: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    return apply_manual_approvals(config)


def run_fill(config: dict[str, Any]) -> Path:
    _ensure_can_fill(config)
    return fill_working_paper(config)


def run_fill_llm(config: dict[str, Any]) -> tuple[Path, dict]:
    _ensure_can_fill(config)
    return fill_working_paper_llm(config)


def run_all(config: dict[str, Any]) -> dict[str, Path | tuple[Path, ...]]:
    bank_path, ledger_path = run_clean(config)
    matches_path, unmatched_bank_path, unmatched_ledger_path = run_match(config)
    result: dict[str, Path | tuple[Path, ...]] = {
        "bank_csv": bank_path,
        "ledger_csv": ledger_path,
        "matches": matches_path,
        "unmatched_bank": unmatched_bank_path,
        "unmatched_ledger": unmatched_ledger_path,
    }
    if _can_fill(config, unmatched_bank_path, unmatched_ledger_path):
        result["working_paper"] = fill_working_paper(config)
    return result


def _can_fill(
    config: dict[str, Any], unmatched_bank_path: Path | None = None, unmatched_ledger_path: Path | None = None
) -> bool:
    if not config.get("working_paper", {}).get("fill_only_when_fully_matched", False):
        return True
    out = output_dir(config)
    unmatched_bank = unmatched_bank_path or out / "matches" / "unmatched_bank.csv"
    unmatched_ledger = unmatched_ledger_path or out / "matches" / "unmatched_ledger.csv"
    if not unmatched_bank.exists() or not unmatched_ledger.exists():
        return False
    return len(pd.read_csv(unmatched_bank)) == 0 and len(pd.read_csv(unmatched_ledger)) == 0


def _ensure_can_fill(config: dict[str, Any]) -> None:
    if _can_fill(config):
        return
    raise RuntimeError("仍存在未匹配银行流水或序时账，按配置不写入底稿。")
