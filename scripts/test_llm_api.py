from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI


def main() -> None:
    load_dotenv()
    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = (
        os.getenv("DASHSCOPE_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = os.getenv("DASHSCOPE_MODEL") or os.getenv("OPENAI_MODEL") or "qwen3.7-plus"
    if not api_key:
        raise SystemExit("请先设置 DASHSCOPE_API_KEY 或 OPENAI_API_KEY。")

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
    print(f"API OK. model={model}")
    print(content.strip())


if __name__ == "__main__":
    main()
