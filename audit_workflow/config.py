from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2) or ""
        return os.getenv(name, default)

    return ENV_PATTERN.sub(repl, value)


def load_config(config_path: str | Path) -> dict[str, Any]:
    load_dotenv()
    path = Path(config_path).resolve()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data = _interpolate(data)
    data["_config_path"] = str(path)
    data["_root"] = str(path.parent)
    return data


def root_dir(config: dict[str, Any]) -> Path:
    return Path(config.get("_root") or ".").resolve()


def resolve_path(config: dict[str, Any], value: str | Path | None) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    return root_dir(config) / path


def output_dir(config: dict[str, Any]) -> Path:
    project = config.get("project", {})
    out = project.get("output_dir", "outputs")
    path = resolve_path(config, out)
    assert path is not None
    return path
