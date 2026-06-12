from __future__ import annotations

import json
import os
from typing import Any

from .utils import text


def run_bank_parser_agent(config: dict[str, Any], prompt: dict[str, Any], system_prompt: str) -> Any:
    models = _output_models()
    agent = _build_agent(config.get("llm", {}), system_prompt, models["BankParserScript"])
    result = agent.run_sync(json.dumps(prompt, ensure_ascii=False))
    return _agent_output(result)


def run_match_decision_agent(
    config: dict[str, Any], prompt: dict[str, Any], system_prompt: str
) -> Any:
    models = _output_models()
    agent = _build_agent(config.get("matching", {}).get("llm", {}), system_prompt, models["MatchDecisionBatch"])
    result = agent.run_sync(json.dumps(prompt, ensure_ascii=False))
    return _agent_output(result)


def _build_agent(llm_config: dict[str, Any], system_prompt: str, output_type: Any):
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as exc:
        raise RuntimeError("请先安装 PydanticAI：pip install pydantic-ai") from exc

    api_key = _llm_api_key(llm_config)
    model_name = _llm_model(llm_config)
    base_url = _llm_base_url(llm_config)
    if not api_key:
        raise RuntimeError("LLM 已启用，但没有设置 API Key。请检查配置中的 api_key_env 或 OPENAI_API_KEY。")
    if not model_name:
        raise RuntimeError("LLM 已启用，但没有设置模型名。请检查配置中的 model/model_env 或 OPENAI_MODEL。")

    provider = OpenAIProvider(api_key=api_key, base_url=base_url or None)
    model = OpenAIModel(model_name, provider=provider)
    try:
        return Agent(model, system_prompt=system_prompt, output_type=output_type)
    except TypeError:
        return Agent(model, system_prompt=system_prompt, result_type=output_type)


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


def _llm_api_key(llm_config: dict[str, Any]) -> str:
    env_name = text(llm_config.get("api_key_env"))
    return os.getenv(env_name) if env_name else os.getenv("OPENAI_API_KEY", "")


def _llm_base_url(llm_config: dict[str, Any]) -> str:
    env_name = text(llm_config.get("base_url_env"))
    if env_name and os.getenv(env_name):
        return os.getenv(env_name, "")
    return text(llm_config.get("base_url")) or os.getenv("OPENAI_BASE_URL", "")


def _llm_model(llm_config: dict[str, Any]) -> str:
    env_name = text(llm_config.get("model_env"))
    if env_name and os.getenv(env_name):
        return os.getenv(env_name, "")
    return text(llm_config.get("model")) or os.getenv("OPENAI_MODEL", "")


def _agent_output(result: Any) -> Any:
    if hasattr(result, "output"):
        return result.output
    if hasattr(result, "data"):
        return result.data
    return result