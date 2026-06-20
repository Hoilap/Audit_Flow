"""Configuration helpers for outbound_settlement_match module.

Follows the same pattern as bank_ledger_match.config but simplified
(no env-var interpolation needed for this workflow).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load YAML config file."""
    path = Path(config_path).resolve()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data["_config_path"] = str(path)
    data["_root"] = str(path.parent)
    return data


def root_dir(config: dict[str, Any]) -> Path:
    """Return the root directory of the config file, or cwd."""
    return Path(config.get("_root") or ".").resolve()


def resolve_path(config: dict[str, Any], value: str | Path | None) -> Path | None:
    """Resolve a path relative to config root. Returns None for empty input."""
    if value in (None, ""):
        return None
    p = Path(str(value))
    if p.is_absolute():
        return p
    return root_dir(config) / p


def output_dir(config: dict[str, Any]) -> Path:
    """Get the output directory from config.

    Priority:
    1. config["project"]["output_dir"]  (absolute or relative to config root)
    2. Construct from _project_root + outputs + customer + task
    """
    project = config.get("project", {})
    out = project.get("output_dir")
    if out:
        path = resolve_path(config, out)
        if path:
            return path

    # Fallback: construct from project root + customer + task
    proj_root = config.get("_project_root", "")
    customer = project.get("customer_name", "")
    task = project.get("task_name", "outbound_settlement_match")
    if proj_root:
        return Path(proj_root) / "outputs" / customer / task
    return root_dir(config) / "outputs" / customer / task


def inputs_dir(config: dict[str, Any]) -> Path:
    """Get the inputs directory from config.

    Priority:
    1. config["project"]["inputs_dir"]
    2. Construct from _project_root + inputs + customer + task
    """
    project = config.get("project", {})
    inp = project.get("inputs_dir")
    if inp:
        path = resolve_path(config, inp)
        if path:
            return path

    proj_root = config.get("_project_root", "")
    customer = project.get("customer_name", "")
    task = project.get("task_name", "outbound_settlement_match")
    if proj_root:
        return Path(proj_root) / "inputs" / customer / task
    return root_dir(config) / "inputs" / customer / task


def ensure_dirs(config: dict[str, Any]) -> Path:
    """Create output subdirectories and return the output root."""
    out = output_dir(config)
    for sub in ("clean", "matches"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    return out
