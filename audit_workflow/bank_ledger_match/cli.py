from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .pipeline import run_all, run_approve, run_clean, run_fill, run_match


def main() -> None:
    parser = argparse.ArgumentParser(prog="audit_workflow")
    parser.add_argument("command", nargs="?", choices=["clean", "match", "approve", "fill", "run"], default="run")
    parser.add_argument("-c", "--config", default="config.example.yml", help="工作流配置文件")
    args = parser.parse_args()

    config = load_config(args.config)
    command = args.command

    if command == "clean":
        paths = run_clean(config)
    elif command == "match":
        paths = run_match(config)
    elif command == "approve":
        paths = run_approve(config)
    elif command == "fill":
        paths = (run_fill(config),)
    elif command == "run":
        result = run_all(config)
        paths = tuple(_flatten(result.values()))
    else:
        raise ValueError(f"未知命令：{command}")

    print("完成：")
    for path in paths:
        print(f"- {Path(path)}")


def _flatten(values):
    for value in values:
        if isinstance(value, tuple):
            yield from value
        else:
            yield value
