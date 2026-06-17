"""银行流水匹配专用 LLM Agent —— 委托给 audit_workflow.llm_agent 通用模块。"""

from __future__ import annotations

import json
from typing import Any

from audit_workflow.llm_agent import build_agent, _agent_output


def run_bank_parser_agent(config: dict[str, Any], prompt: dict[str, Any], system_prompt: str) -> Any:
    models = _output_models()
    agent = build_agent(config.get("llm", {}), system_prompt, models["BankParserScript"])
    result = agent.run_sync(json.dumps(prompt, ensure_ascii=False))
    return _agent_output(result)


def run_match_decision_agent(
    config: dict[str, Any], prompt: dict[str, Any], system_prompt: str
) -> Any:
    models = _output_models()
    llm_config = config.get("llm", {})
    task_config = config.get("matching", {}).get("llm", {})
    agent = build_agent(llm_config, system_prompt, models["MatchDecisionBatch"], task_config=task_config)
    result = agent.run_sync(json.dumps(prompt, ensure_ascii=False))
    return _agent_output(result)


def _output_models() -> dict[str, Any]:
    try:
        from pydantic import Field, create_model
    except ImportError as exc:
        raise RuntimeError("请先安装 PydanticAI：pip install pydantic-ai") from exc

    match_decision = create_model(
        "MatchDecision",
        candidate_id=(str, ...),
        approve=(bool, ...),
        confidence=(float, Field(ge=0, le=1)),
        reason=(str, ...),
    )
    match_decision_batch = create_model("MatchDecisionBatch", decisions=(list[match_decision], ...))
    bank_parser_script = create_model(
        "BankParserScript",
        code=(str, Field(description="完整 Python 源码，必须包含 parse(path: str, config: dict) -> list[dict]")),
    )
    return {
        "BankParserScript": bank_parser_script,
        "MatchDecisionBatch": match_decision_batch,
    }