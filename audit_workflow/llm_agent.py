"""通用 LLM Agent 模块 —— 可被 bank_ledger_match、desktop/api.py 等所有模块复用。"""

from __future__ import annotations

import json
import os
from typing import Any, NamedTuple


class ProviderConfig(NamedTuple):
    """解析后的 LLM provider 连接参数。"""
    api_key: str
    base_url: str
    model_name: str


def resolve_provider(
    llm_config: dict[str, Any],
    task_config: dict[str, Any] | None = None,
) -> ProviderConfig:
    """从配置中解析出最终使用的 API 连接参数。

    支持两种配置格式:

    1. **providers 格式（新）**::

        llm:
          providers:
            dashscope:
              api_key_env: DASHSCOPE_API_KEY
              base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
              model: qwen3.7-plus
            openai:
              api_key_env: OPENAI_API_KEY
              base_url: https://api.openai.com/v1
              model: gpt-4o
          default: dashscope

        task_config 中可通过 ``provider: openai`` 指定使用哪个。

    2. **扁平格式（旧，向后兼容）**::

        llm:
          api_key: sk-xxx
          base_url: https://...
          model: qwen3.7-plus

    Parameters
    ----------
    llm_config : dict
        顶层 ``config["llm"]`` 配置段。
    task_config : dict, optional
        任务级配置段（如 ``config["matching"]["llm"]``），其中的
        ``provider`` 字段用于选择 providers 中的某个。

    Returns
    -------
    ProviderConfig
        包含 api_key、base_url、model_name 的命名元组。
    """
    providers = llm_config.get("providers")

    if providers and isinstance(providers, dict):
        # --- providers 格式 ---
        # 确定 provider 名称：task_config["provider"] > llm_config["default"] > providers 中第一个
        provider_name = None
        if task_config:
            provider_name = _text(task_config.get("provider"))
        if not provider_name:
            provider_name = _text(llm_config.get("default"))
        if not provider_name:
            provider_name = next(iter(providers))

        if provider_name not in providers:
            available = ", ".join(providers.keys())
            raise RuntimeError(
                f"LLM provider '{provider_name}' 未在 providers 中定义。可用: {available}"
            )

        provider_cfg = {**providers[provider_name]}
        # task_config 中的 model 可覆盖 provider 的 model（方便按任务换模型）
        if task_config and _text(task_config.get("model")):
            provider_cfg["model"] = task_config["model"]

        api_key = _llm_api_key(provider_cfg)
        base_url = _llm_base_url(provider_cfg)
        model_name = _llm_model(provider_cfg)
    else:
        # --- 扁平格式（向后兼容）---
        # task_config 中的 model 也可覆盖
        merged = {**llm_config}
        if task_config:
            if _text(task_config.get("model")):
                merged["model"] = task_config["model"]
            # 旧格式下 task_config 也可能自带 api_key / base_url（如 matching.llm 单独配了一组）
            for key in ("api_key", "api_key_env", "base_url", "base_url_env", "model_env"):
                if _text(task_config.get(key)):
                    merged[key] = task_config[key]

        api_key = _llm_api_key(merged)
        base_url = _llm_base_url(merged)
        model_name = _llm_model(merged)

    return ProviderConfig(api_key=api_key, base_url=base_url, model_name=model_name)


def build_agent(
    llm_config: dict[str, Any],
    system_prompt: str,
    output_type: Any = None,
    task_config: dict[str, Any] | None = None,
):
    """构建一个 PydanticAI Agent，返回 Agent 实例。

    参数:
        llm_config: 包含 providers（新格式）或 api_key/base_url/model（旧格式）的配置字典
        system_prompt: 系统提示词
        output_type: 可选，结构化输出类型（Pydantic model）
        task_config: 可选，任务级配置（如 matching.llm），用于选择 provider 或覆盖 model
    """
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as exc:
        raise RuntimeError("请先安装 PydanticAI：pip install pydantic-ai") from exc

    provider_cfg = resolve_provider(llm_config, task_config)
    api_key = provider_cfg.api_key
    base_url = provider_cfg.base_url
    model_name = provider_cfg.model_name

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


def llm_generate(
    llm_config: dict[str, Any],
    prompt: str,
    system_prompt: str = "",
    task_config: dict[str, Any] | None = None,
) -> str:
    """通用文本生成：给定 LLM 配置和 prompt，返回生成的文本。

    参数:
        llm_config: LLM 配置字典
        prompt: 用户提示词
        system_prompt: 可选系统提示词
        task_config: 可选，任务级配置（用于选择 provider）

    返回:
        str: 当 return_usage=False 时返回纯文本
        dict: 当 return_usage=True 时返回 {"text": str, "usage": {...}}
    """
    import asyncio
    GENERAL_SYSTEM_PROMPT = "你是一个审计人员代码助手。请直接输出纯 Python 代码，不要用 ``` 代码块包裹，不要任何多余的解释。"
    agent = build_agent(llm_config, system_prompt or GENERAL_SYSTEM_PROMPT, task_config=task_config)
    result = agent.run_sync(prompt)
    text = _agent_output_text(result)
    usage = _extract_token_usage(result)
    return text, usage


async def llm_generate_async(
    llm_config: dict[str, Any],
    prompt: str,
    system_prompt: str = "",
    task_config: dict[str, Any] | None = None,
) -> str:
    """异步版本：通用文本生成。
    返回: (text, usage) 元组
    """
    agent = build_agent(llm_config, system_prompt or "You are a helpful assistant.", task_config=task_config)
    result = await agent.run(prompt)
    text = _agent_output_text(result)
    usage = _extract_token_usage(result)
    return text, usage


def llm_generate_structured(
    llm_config: dict[str, Any],
    prompt: str,
    output_type: Any,
    system_prompt: str = "",
    task_config: dict[str, Any] | None = None,
) -> Any:
    """通用结构化生成：给定 LLM 配置、prompt 和 Pydantic 输出类型，返回结构化结果。

    返回: (output, usage) 元组
    """
    agent = build_agent(llm_config, system_prompt, output_type, task_config=task_config)
    result = agent.run_sync(prompt)
    output = _agent_output(result)
    usage = _extract_token_usage(result)
    return output, usage


async def llm_generate_structured_async(
    llm_config: dict[str, Any],
    prompt: str,
    output_type: Any,
    system_prompt: str = "",
    task_config: dict[str, Any] | None = None,
) -> Any:
    """异步版本：通用结构化生成。
    返回: (output, usage) 元组
    """
    agent = build_agent(llm_config, system_prompt, output_type, task_config=task_config)
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
    import logging
    _log = logging.getLogger(__name__)

    usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    try:
        # pydantic-ai 0.8.x: usage() 方法返回 RunUsage 对象
        # 属性名: total_tokens, request_tokens, response_tokens
        if hasattr(result, "usage") and callable(result.usage):
            u = result.usage()
            usage["total_tokens"] = getattr(u, "total_tokens", 0) or 0
            usage["prompt_tokens"] = getattr(u, "request_tokens", 0) or getattr(u, "prompt_tokens", 0) or 0
            usage["completion_tokens"] = getattr(u, "response_tokens", 0) or getattr(u, "completion_tokens", 0) or 0
            return usage
        if hasattr(result, "_usage"):
            u = result._usage
            usage["total_tokens"] = getattr(u, "total_tokens", 0) or 0
            usage["prompt_tokens"] = getattr(u, "prompt_tokens", 0) or getattr(u, "request_tokens", 0) or 0
            usage["completion_tokens"] = getattr(u, "completion_tokens", 0) or getattr(u, "response_tokens", 0) or 0
            return usage
        _log.warning("_extract_token_usage: result(%s) 无 usage 属性，token 计数为 0",
                      type(result).__name__)
    except Exception as e:
        _log.warning("_extract_token_usage 异常: %s", e)
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