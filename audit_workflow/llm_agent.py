"""通用 LLM Agent 模块 —— 可被 bank_ledger_match、desktop/api.py 等所有模块复用。"""

from __future__ import annotations

import json
import os
from typing import Any


def build_agent(llm_config: dict[str, Any], system_prompt: str, output_type: Any = None):
    """构建一个 PydanticAI Agent，返回 Agent 实例。

    参数:
        llm_config: 包含 api_key_env / model_env / base_url_env / model / base_url 等键的字典
        system_prompt: 系统提示词
        output_type: 可选，结构化输出类型（Pydantic model）
    """
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

    # pydantic-ai 0.8.x: Agent 使用 output_type 参数
    if output_type is not None:
        # 禁用 thinking mode，qwen-thinking 模型默认开启 thinking 与 tool_choice 冲突
        return Agent(
            model,
            system_prompt=system_prompt,
            output_type=output_type,
            model_settings={"extra_body": {"enable_thinking": False}},
        )
    else:
        return Agent(model, system_prompt=system_prompt)


def llm_generate(llm_config: dict[str, Any], prompt: str, system_prompt: str = "") -> str:
    """通用文本生成：给定 LLM 配置和 prompt，返回生成的文本。

    参数:
        llm_config: LLM 配置字典
        prompt: 用户提示词
        system_prompt: 可选系统提示词

    返回:
        str: 当 return_usage=False 时返回纯文本
        dict: 当 return_usage=True 时返回 {"text": str, "usage": {...}}
    """
    import asyncio
    agent = build_agent(llm_config, system_prompt or "You are a helpful assistant.")
    result = agent.run_sync(prompt)
    text = _agent_output_text(result)
    usage = _extract_token_usage(result)
    return text, usage


async def llm_generate_async(llm_config: dict[str, Any], prompt: str, system_prompt: str = "") -> str:
    """异步版本：通用文本生成。
    返回: (text, usage) 元组
    """
    agent = build_agent(llm_config, system_prompt or "You are a helpful assistant.")
    result = await agent.run(prompt)
    text = _agent_output_text(result)
    usage = _extract_token_usage(result)
    return text, usage


def llm_generate_structured(
    llm_config: dict[str, Any],
    prompt: str,
    output_type: Any,
    system_prompt: str = "",
) -> Any:
    """通用结构化生成：给定 LLM 配置、prompt 和 Pydantic 输出类型，返回结构化结果。

    返回: (output, usage) 元组
    """
    agent = build_agent(llm_config, system_prompt, output_type)
    result = agent.run_sync(prompt)
    output = _agent_output(result)
    usage = _extract_token_usage(result)
    return output, usage


async def llm_generate_structured_async(
    llm_config: dict[str, Any],
    prompt: str,
    output_type: Any,
    system_prompt: str = "",
) -> Any:
    """异步版本：通用结构化生成。
    返回: (output, usage) 元组
    """
    agent = build_agent(llm_config, system_prompt, output_type)
    result = await agent.run(prompt)
    output = _agent_output(result)
    usage = _extract_token_usage(result)
    return output, usage


def _agent_output(result: Any) -> Any:
    if hasattr(result, "output"):
        return result.output
    if hasattr(result, "data"):
        return result.data
    return result


def _agent_output_text(result: Any) -> str:
    output = _agent_output(result)
    if isinstance(output, str):
        return output
    if hasattr(output, "data") and isinstance(output.data, str):
        return output.data
    if hasattr(output, "content") and isinstance(output.content, str):
        return output.content
    return str(output)


def _extract_token_usage(result: Any) -> dict:
    """从 pydantic-ai result 中提取 token 用量信息。
    返回 {"total_tokens": int, "prompt_tokens": int, "completion_tokens": int}
    """
    usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    try:
        # pydantic-ai 0.8.x: usage() 方法返回 Usage 对象
        if hasattr(result, "usage") and callable(result.usage):
            u = result.usage()
            usage["total_tokens"] = getattr(u, "total_tokens", 0) or 0
            usage["prompt_tokens"] = getattr(u, "request_tokens", 0) or getattr(u, "prompt_tokens", 0) or 0
            usage["completion_tokens"] = getattr(u, "response_tokens", 0) or getattr(u, "completion_tokens", 0) or 0
            return usage
        # 尝试从 _usage 属性获取
        if hasattr(result, "_usage"):
            u = result._usage
            usage["total_tokens"] = getattr(u, "total_tokens", 0) or 0
            usage["prompt_tokens"] = getattr(u, "prompt_tokens", 0) or getattr(u, "request_tokens", 0) or 0
            usage["completion_tokens"] = getattr(u, "completion_tokens", 0) or getattr(u, "response_tokens", 0) or 0
            return usage
    except Exception:
        pass
    return usage


def _llm_api_key(llm_config: dict[str, Any]) -> str:
    # 优先从 api_key 直接字段读取
    direct = _text(llm_config.get("api_key"))
    if direct:
        return direct
    # 其次从 api_key_env 环境变量读取
    env_name = _text(llm_config.get("api_key_env"))
    if env_name:
        val = os.getenv(env_name)
        if val:
            return val
        # 如果 env_name 不像是环境变量名（比如以 sk- 开头），则当字面量使用
        if not env_name.startswith(("$", "ENV_", "env_")) and len(env_name) > 20:
            return env_name
    return os.getenv("OPENAI_API_KEY", "")


def _llm_base_url(llm_config: dict[str, Any]) -> str:
    # 优先从 base_url 直接字段读取
    direct = _text(llm_config.get("base_url"))
    if direct:
        return direct
    # 其次从 base_url_env 环境变量读取
    env_name = _text(llm_config.get("base_url_env"))
    if env_name:
        val = os.getenv(env_name)
        if val:
            return val
        # 如果 env_name 看起来像 URL，当字面量使用
        if env_name.startswith("http"):
            return env_name
    return os.getenv("OPENAI_BASE_URL", "")


def _llm_model(llm_config: dict[str, Any]) -> str:
    # 优先从 model 直接字段读取
    direct = _text(llm_config.get("model"))
    if direct:
        return direct
    # 其次从 model_env 环境变量读取
    env_name = _text(llm_config.get("model_env"))
    if env_name:
        val = os.getenv(env_name)
        if val:
            return val
        # 如果 env_name 不像是环境变量名（比如包含数字/点号），当字面量使用
        if not env_name.isupper() or not env_name.startswith("$"):
            return env_name
    return os.getenv("OPENAI_MODEL", "")


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()