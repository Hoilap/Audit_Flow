"""OSM 专用 LLM Agent —— 委托给 audit_workflow.llm_agent 通用模块。"""

from __future__ import annotations

import json
from typing import Any

from audit_workflow.llm_agent import build_agent, _agent_output, _extract_token_usage


def run_osm_cleaner_agent(
    config: dict[str, Any], prompt: dict[str, Any], system_prompt: str
) -> tuple[Any, Any]:
    """调用 LLM 生成 OSM 清洗脚本代码。

    使用 OsmCleanerScript 结构化输出模型，确保返回的 code 字段包含
    完整的 Python 清洗脚本。

    Returns:
        (output, raw_result) — output 为 OsmCleanerScript 对象，
        raw_result 为 pydantic-ai 原始 RunResult（用于提取 token 用量）。
    """
    models = _output_models()
    llm_config = config.get("llm", {})
    agent = build_agent(llm_config, system_prompt, models["OsmCleanerScript"])
    result = agent.run_sync(json.dumps(prompt, ensure_ascii=False))
    return _agent_output(result), result


def _output_models() -> dict[str, Any]:
    """构建 Pydantic 输出模型。"""
    try:
        from pydantic import Field, create_model
    except ImportError as exc:
        raise RuntimeError("请先安装 PydanticAI：pip install pydantic-ai") from exc

    osm_cleaner_script = create_model(
        "OsmCleanerScript",
        code=(
            str,
            Field(
                description="完整 Python 源码，必须包含 def clean(...) -> list[dict]"
            ),
        ),
    )
    return {"OsmCleanerScript": osm_cleaner_script}
