"""验证 resolve_provider 在新旧两种配置格式下都能正确工作。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os

# 模拟环境变量
os.environ["DASHSCOPE_API_KEY"] = "test-dashscope-key"
os.environ["DASHSCOPE_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
os.environ["DASHSCOPE_MODEL"] = "qwen3.7-plus"
os.environ["OPENAI_API_KEY"] = "test-openai-key"
os.environ["OPENAI_BASE_URL"] = "https://api.openai.com/v1"
os.environ["OPENAI_MODEL"] = "gpt-4o"

from audit_workflow.llm_agent import resolve_provider

errors = []

# ── 测试 1: 新格式 — 默认 provider ──
print("=" * 50)
print("测试 1: 新格式，默认 provider")
llm_config_new = {
    "enabled": True,
    "default": "dashscope",
    "providers": {
        "dashscope": {
            "api_key_env": "DASHSCOPE_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.7-plus",
        },
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
        },
    },
}
pc = resolve_provider(llm_config_new)
print(f"  api_key={pc.api_key}")
print(f"  base_url={pc.base_url}")
print(f"  model_name={pc.model_name}")
assert pc.api_key == "test-dashscope-key", f"Expected test-dashscope-key, got {pc.api_key}"
assert pc.model_name == "qwen3.7-plus", f"Expected qwen3.7-plus, got {pc.model_name}"
print("  PASS")

# ── 测试 2: 新格式 — task_config 指定 openai ──
print("=" * 50)
print("测试 2: 新格式，task_config 指定 openai")
task_config = {"provider": "openai"}
pc = resolve_provider(llm_config_new, task_config)
print(f"  api_key={pc.api_key}")
print(f"  base_url={pc.base_url}")
print(f"  model_name={pc.model_name}")
assert pc.api_key == "test-openai-key", f"Expected test-openai-key, got {pc.api_key}"
assert pc.model_name == "gpt-4o", f"Expected gpt-4o, got {pc.model_name}"
print("  PASS")

# ── 测试 3: 新格式 — task_config 覆盖 model ──
print("=" * 50)
print("测试 3: 新格式，task_config 覆盖 model")
task_config = {"provider": "dashscope", "model": "qwen-max"}
pc = resolve_provider(llm_config_new, task_config)
print(f"  api_key={pc.api_key}")
print(f"  model_name={pc.model_name}")
assert pc.model_name == "qwen-max", f"Expected qwen-max, got {pc.model_name}"
print("  PASS")

# ── 测试 4: 新格式 — 无效 provider 名称 ──
print("=" * 50)
print("测试 4: 新格式，无效 provider 名称")
task_config = {"provider": "nonexistent"}
try:
    pc = resolve_provider(llm_config_new, task_config)
    print("  FAIL: 应该抛出异常但没有")
    errors.append("测试 4 失败")
except RuntimeError as e:
    print(f"  捕获预期异常: {e}")
    print("  PASS")

# ── 测试 5: 旧格式（扁平） ──
print("=" * 50)
print("测试 5: 旧格式（扁平配置）")
llm_config_old = {
    "api_key_env": "DASHSCOPE_API_KEY",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3.7-plus",
}
pc = resolve_provider(llm_config_old)
print(f"  api_key={pc.api_key}")
print(f"  base_url={pc.base_url}")
print(f"  model_name={pc.model_name}")
assert pc.api_key == "test-dashscope-key", f"Expected test-dashscope-key, got {pc.api_key}"
assert pc.model_name == "qwen3.7-plus", f"Expected qwen3.7-plus, got {pc.model_name}"
print("  PASS")

# ── 测试 6: 旧格式 + task_config 覆盖（模拟 matching.llm 独立配了一组） ──
print("=" * 50)
print("测试 6: 旧格式 + task_config 独立配置")
llm_config_old = {
    "api_key_env": "DASHSCOPE_API_KEY",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3.7-plus",
}
task_config = {
    "api_key_env": "OPENAI_API_KEY",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o",
}
pc = resolve_provider(llm_config_old, task_config)
print(f"  api_key={pc.api_key}")
print(f"  base_url={pc.base_url}")
print(f"  model_name={pc.model_name}")
assert pc.api_key == "test-openai-key", f"Expected test-openai-key, got {pc.api_key}"
assert pc.model_name == "gpt-4o", f"Expected gpt-4o, got {pc.model_name}"
print("  PASS")

# ── 测试 7: 新格式 — 无 default，取第一个 ──
print("=" * 50)
print("测试 7: 新格式，无 default 字段，自动取第一个 provider")
llm_config_no_default = {
    "providers": {
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
        },
    },
}
pc = resolve_provider(llm_config_no_default)
print(f"  api_key={pc.api_key}")
print(f"  model_name={pc.model_name}")
assert pc.api_key == "test-openai-key", f"Expected test-openai-key, got {pc.api_key}"
print("  PASS")

print("=" * 50)
if errors:
    print(f"有 {len(errors)} 个测试失败: {errors}")
    sys.exit(1)
else:
    print("全部 7 个测试通过!")
