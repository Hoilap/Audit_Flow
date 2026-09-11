"""测试 LLM API 连通性。

用法:
    python scripts/test_llm_api.py              # 测试默认 provider
    python scripts/test_llm_api.py openai       # 测试名为 openai 的 provider
    python scripts/test_llm_api.py dashscope    # 测试名为 dashscope 的 provider
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from dotenv import load_dotenv
from openai import OpenAI


def _load_llm_config(provider_name: str | None = None) -> tuple[str, str, str]:
    """从开发环境 LLM 配置加载 provider。

    返回 (api_key, base_url, model) 元组。
    """
    from audit_workflow.llm_agent import resolve_provider

    # 尝试加载配置文件
    root = Path(__file__).resolve().parents[1]
    for name in ("config.llm.development.yml", "config.llm.yml"):
        cfg_path = root / "config" / name
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            break
    else:
        raise SystemExit("找不到 config/config.llm.development.yml 或 config/config.llm.yml")

    llm_config = raw.get("llm", {})
    task_config = {"provider": provider_name} if provider_name else None
    pc = resolve_provider(llm_config, task_config)
    return pc.api_key, pc.base_url, pc.model_name


def main() -> None:
    load_dotenv()

    provider_name = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        api_key, base_url, model = _load_llm_config(provider_name)
    except Exception as e:
        # 配置文件加载失败时，回退到环境变量
        import os
        api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = (
            os.getenv("DASHSCOPE_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        model = os.getenv("DASHSCOPE_MODEL") or os.getenv("OPENAI_MODEL") or "qwen3.7-plus"
        print(f"[fallback] 配置文件加载失败 ({e})，使用环境变量")

    if not api_key:
        raise SystemExit("没有可用的 API Key。请在 .env 或配置文件中设置。")

    label = f"provider={provider_name}" if provider_name else "default provider"
    print(f"测试 {label}: model={model}, base_url={base_url}")

    client = OpenAI(api_key=api_key, base_url=base_url)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "用一句中文回答，确认接口可用。"},
            {"role": "user", "content": "你是谁？"},
        ],
        temperature=0,
    )
    content = completion.choices[0].message.content or ""
    print(f"API OK. {label}")
    print(content.strip())


if __name__ == "__main__":
    main()
