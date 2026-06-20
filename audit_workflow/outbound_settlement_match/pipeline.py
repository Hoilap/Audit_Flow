"""Pipeline orchestrator for outbound_settlement_match workflow.

Provides step-level entry points that can be called independently
or chained together via run_all().

Steps:
1. detect   — Scan inputs, identify files, classify sheet types
2. clean_settlement — Clean and aggregate platform settlement CSVs
3. clean_outbound   — Clean outbound Excel files (sellout/refund/return/transfer)
4. match    — Filter net outbound and match against settlement by ID
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import inputs_dir, output_dir, ensure_dirs
from .settlement_cleaner import clean_settlement as _clean_settlement
from .outbound_cleaner import clean_outbound as _clean_outbound, classify_sheet
from .matcher import match_outbound_settlement as _match

logger = logging.getLogger(__name__)


# ── Step 1: Detect ──────────────────────────────────────────

def run_detect(config: dict[str, Any]) -> dict[str, Any]:
    """Scan input files and classify them.

    Returns:
        dict with:
            'settlement_files': list of settlement CSV file info
            'outbound_files': list of outbound Excel file info with sheet classifications
            'settlement_dir': path to settlement directory
            'outbound_dir': path to outbound directory
    """
    inp = inputs_dir(config)
    settle_dir = inp / "settlement"
    outbound_dir = inp / "outbound"

    result: dict[str, Any] = {
        "settlement_files": [],
        "outbound_files": [],
        "settlement_dir": str(settle_dir),
        "outbound_dir": str(outbound_dir),
    }

    # Scan settlement files
    if settle_dir.exists():
        for csv_path in sorted(settle_dir.rglob("*.csv")):
            result["settlement_files"].append({
                "path": str(csv_path.relative_to(inp)),
                "name": csv_path.name,
                "size": csv_path.stat().st_size,
            })
        logger.info("Detected %d settlement CSV files", len(result["settlement_files"]))
    else:
        logger.warning("Settlement directory not found: %s", settle_dir)

    # Scan outbound files with sheet classification
    if outbound_dir.exists():
        import pandas as pd
        for xlsx_path in sorted(outbound_dir.iterdir()):
            if xlsx_path.suffix.lower() not in (".xlsx", ".xls") or xlsx_path.name.startswith("~"):
                continue
            file_info: dict[str, Any] = {
                "path": str(xlsx_path.relative_to(inp)),
                "name": xlsx_path.name,
                "size": xlsx_path.stat().st_size,
                "sheets": [],
            }
            try:
                xl = pd.ExcelFile(str(xlsx_path), engine="openpyxl")
                for sheet_name in xl.sheet_names:
                    stype = classify_sheet(sheet_name)
                    file_info["sheets"].append({
                        "name": sheet_name,
                        "type": stype,
                    })
                xl.close()
            except Exception as e:
                file_info["error"] = str(e)
            result["outbound_files"].append(file_info)
        logger.info("Detected %d outbound Excel files", len(result["outbound_files"]))
    else:
        logger.warning("Outbound directory not found: %s", outbound_dir)

    return result


# ── Step 2: Clean Settlement ────────────────────────────────

def run_clean_settlement(config: dict[str, Any]) -> tuple[Path, Path]:
    """Clean and aggregate platform settlement CSV files.

    When ``config["llm"]["enabled"]`` is true, uses LLM-generated
    cleaning scripts that handle arbitrary column layouts.  Falls back
    to the hardcoded column-mapping logic otherwise (or on failure).

    Cleansing mode and any fallback reasons are recorded in
    ``config["_cleaning"]["settlement"]`` so that API layers can
    surface the information to users.

    Returns:
        (settlement_all_csv_path, monthly_summary_csv_path)
    """
    ensure_dirs(config)
    info = _cleaning_info(config, "settlement")

    llm_cfg = config.get("llm", {})
    if llm_cfg.get("enabled", False):
        reason = _check_llm_ready(llm_cfg)
        if reason:
            info["mode"] = "hardcoded"
            info["fallback_reason"] = reason
            logger.warning("LLM not available for settlement cleaning: %s", reason)
            return _clean_settlement(config)
        try:
            from .settlement_cleaner import clean_settlement_llm
            result = clean_settlement_llm(config)
            info["mode"] = "llm"
            return result
        except Exception as e:
            reason = str(e)
            info["mode"] = "hardcoded"
            info["fallback_reason"] = f"LLM 清洗失败，已回退到硬编码：{reason}"
            logger.warning(
                "LLM settlement cleaning failed entirely, falling back: %s", e
            )
    else:
        info["mode"] = "hardcoded"
    return _clean_settlement(config)


# ── Step 3: Clean Outbound ──────────────────────────────────

def run_clean_outbound(config: dict[str, Any]) -> dict[str, Path | None]:
    """Clean outbound Excel files.

    When ``config["llm"]["enabled"]`` is true, uses LLM-generated
    cleaning scripts that handle arbitrary column layouts.  Falls back
    to the hardcoded column-mapping logic otherwise (or on failure).

    Cleansing mode and any fallback reasons are recorded in
    ``config["_cleaning"]["outbound"]`` so that API layers can
    surface the information to users.

    Returns:
        dict with keys 'sellout', 'refund', 'return', 'transfer',
        values are paths to cleaned CSVs (or None if no data found).
    """
    ensure_dirs(config)
    info = _cleaning_info(config, "outbound")

    llm_cfg = config.get("llm", {})
    if llm_cfg.get("enabled", False):
        reason = _check_llm_ready(llm_cfg)
        if reason:
            info["mode"] = "hardcoded"
            info["fallback_reason"] = reason
            logger.warning("LLM not available for outbound cleaning: %s", reason)
            return _clean_outbound(config)
        try:
            from .outbound_cleaner import clean_outbound_llm
            result = clean_outbound_llm(config)
            info["mode"] = "llm"
            return result
        except Exception as e:
            reason = str(e)
            info["mode"] = "hardcoded"
            info["fallback_reason"] = f"LLM 清洗失败，已回退到硬编码：{reason}"
            logger.warning(
                "LLM outbound cleaning failed entirely, falling back: %s", e
            )
    else:
        info["mode"] = "hardcoded"
    return _clean_outbound(config)


# ── Step 4: Match ───────────────────────────────────────────

def run_match(
    config: dict[str, Any],
    outbound_paths: dict[str, Path | None] | None = None,
    settlement_path: Path | None = None,
) -> dict[str, Any]:
    """Filter net outbound and match against settlement.

    If outbound_paths and settlement_path are not provided,
    they are loaded from the standard output locations.

    Returns:
        dict with match results (see matcher.match_outbound_settlement).
    """
    ensure_dirs(config)
    out = output_dir(config)
    clean_dir = out / "clean"

    # Load paths from default locations if not provided
    if outbound_paths is None:
        outbound_paths = {}
        for name in ("sellout", "refund", "return", "transfer"):
            path = clean_dir / f"{name}.csv"
            outbound_paths[name] = path if path.exists() else None

    if settlement_path is None:
        settlement_path = clean_dir / "settlement_all.csv"
        if not settlement_path.exists():
            raise FileNotFoundError(
                f"Settlement file not found at {settlement_path}. "
                "Run clean_settlement first."
            )

    return _match(config, outbound_paths, settlement_path)


# ── Full Pipeline ────────────────────────────────────────────

def run_all(config: dict[str, Any]) -> dict[str, Any]:
    """Run the complete pipeline: detect → clean → match.

    Returns:
        dict with all intermediate and final results.
    """
    result: dict[str, Any] = {}

    # Step 1: Detect
    logger.info("═══ Step 1: Detect ═══")
    result["detect"] = run_detect(config)

    # Step 2: Clean settlement
    logger.info("═══ Step 2: Clean Settlement ═══")
    settlement_path, summary_path = run_clean_settlement(config)
    result["settlement_path"] = settlement_path
    result["settlement_summary_path"] = summary_path

    # Step 3: Clean outbound
    logger.info("═══ Step 3: Clean Outbound ═══")
    outbound_paths = run_clean_outbound(config)
    result["outbound_paths"] = outbound_paths

    # Step 4: Match
    logger.info("═══ Step 4: Match ═══")
    match_result = run_match(config, outbound_paths, settlement_path)
    result["match"] = match_result

    return result


# ── Internal helpers ────────────────────────────────────────────

def _cleaning_info(config: dict[str, Any], step: str) -> dict[str, Any]:
    """Get or create the cleaning-mode tracking dict for *step*.

    Writes into ``config["_cleaning"][step]`` so that callers (API
    layer) can read the mode and fallback reason after the pipeline
    returns.
    """
    cleaning = config.setdefault("_cleaning", {})
    info: dict[str, Any] = {"mode": "hardcoded"}
    cleaning[step] = info
    return info


def _check_llm_ready(llm_cfg: dict[str, Any]) -> str | None:
    """Return a human-readable reason if LLM is *not* ready, else None.

    Checks that the config has providers (or flat api_key) so that
    code generation will not fail with a cryptic error.
    """
    import os

    providers = llm_cfg.get("providers")
    if providers and isinstance(providers, dict):
        # Providers format — check that at least one provider has a usable api_key
        default_name = llm_cfg.get("default") or next(iter(providers))
        pcfg = providers.get(default_name, {})
        api_key_env = pcfg.get("api_key_env", "")
        api_key = pcfg.get("api_key", "")
        if api_key:
            return None
        if api_key_env and os.getenv(api_key_env):
            return None
        return (
            f"LLM provider '{default_name}' 未配置 API Key"
            f"（环境变量 {api_key_env} 未设置）"
        )

    # Flat format
    api_key = llm_cfg.get("api_key", "")
    api_key_env = llm_cfg.get("api_key_env", "")
    if api_key:
        return None
    if api_key_env and os.getenv(api_key_env):
        return None
    return (
        "LLM 未配置 API Key"
        f"（{api_key_env or 'api_key'} 未设置）"
    )
