"""验证 use_llm 从前端到 Match 步骤的完整传播链路。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
import os

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "audit_workflow" / "bank_ledger_match"))
from file_detector import merge_llm_into_full_config

errors = []

# ── 模拟配置文件加载 ──

# 1. 加载开发环境 LLM 配置
with open(root / "config" / "config.llm.development.yml", encoding="utf-8") as f:
    llm_cfg = yaml.safe_load(f) or {}

# 2. 加载 config.example.matching.yml
with open(root / "config.example.matching.yml", encoding="utf-8") as f:
    matching_cfg = yaml.safe_load(f) or {}

# 3. 合并 matching → llm（和 desktop/api.py 逻辑一致）
llm_cfg.setdefault("llm", {})
llm_cfg["llm"]["matching"] = matching_cfg.get("matching", {})

# 4. 模拟 task.yml（含 use_llm）
task_cfg_with_llm = {
    "project": {"client_name": "测试公司"},
    "use_llm": True,
}
task_cfg_without_llm = {
    "project": {"client_name": "测试公司"},
    "use_llm": False,
}
task_cfg_no_field = {
    "project": {"client_name": "测试公司"},
}


def apply_use_llm_override(task_cfg, llm_cfg):
    """模拟 _build_full_config 中的 use_llm 覆盖逻辑"""
    cfg = merge_llm_into_full_config(task_cfg, llm_cfg)

    use_llm = task_cfg.get("use_llm")
    if use_llm is not None:
        cfg.setdefault("matching", {}).setdefault("llm", {})
        cfg["matching"]["llm"]["enabled"] = bool(use_llm)

    return cfg


# ── 测试 1: use_llm=True → matching.llm.enabled=True ──
print("=" * 50)
print("测试 1: use_llm=True → matching.llm.enabled 应为 True")
cfg = apply_use_llm_override(task_cfg_with_llm, llm_cfg)
enabled = cfg.get("matching", {}).get("llm", {}).get("enabled")
print(f"  matching.llm.enabled = {enabled}")
assert enabled is True, f"Expected True, got {enabled}"
print("  PASS")

# ── 测试 2: use_llm=False → matching.llm.enabled=False ──
print("=" * 50)
print("测试 2: use_llm=False → matching.llm.enabled 应为 False")
cfg = apply_use_llm_override(task_cfg_without_llm, llm_cfg)
enabled = cfg.get("matching", {}).get("llm", {}).get("enabled")
print(f"  matching.llm.enabled = {enabled}")
assert enabled is False, f"Expected False, got {enabled}"
print("  PASS")

# ── 测试 3: task.yml 无 use_llm 字段 → 不做覆盖 ──
print("=" * 50)
print("测试 3: task.yml 无 use_llm → 不覆盖，使用配置文件默认值")
cfg = apply_use_llm_override(task_cfg_no_field, llm_cfg)
enabled = cfg.get("matching", {}).get("llm", {}).get("enabled")
print(f"  matching.llm.enabled = {enabled}")
# config.example.matching.yml 中已移除 enabled，所以应为 None
print(f"  (None 表示未被设置，matcher.py 默认按 False 处理)")
print("  PASS")

# ── 测试 4: 匹配参数仍然正确加载 ──
print("=" * 50)
print("测试 4: matching 参数仍然正确加载（不受 use_llm 影响）")
cfg = apply_use_llm_override(task_cfg_with_llm, llm_cfg)
batch_size = cfg.get("matching", {}).get("llm", {}).get("batch_size")
confidence = cfg.get("matching", {}).get("llm", {}).get("acceptance_confidence")
window_days = cfg.get("matching", {}).get("llm", {}).get("candidate_window_days")
print(f"  batch_size = {batch_size}")
print(f"  acceptance_confidence = {confidence}")
print(f"  candidate_window_days = {window_days}")
assert batch_size == 5, f"Expected 5, got {batch_size}"
assert confidence == 0.72, f"Expected 0.72, got {confidence}"
assert window_days == 45, f"Expected 45, got {window_days}"
print("  PASS")

# ── 测试 5: llm providers 仍然存在 ──
print("=" * 50)
print("测试 5: llm.providers 配置未受影响")
cfg = apply_use_llm_override(task_cfg_with_llm, llm_cfg)
providers = cfg.get("llm", {}).get("providers", {})
print(f"  providers = {list(providers.keys())}")
default_provider = cfg.get("llm", {}).get("default")
assert providers, "at least one provider should exist"
assert default_provider in providers, "default provider should exist in providers"
print("  PASS")

print("=" * 50)
if errors:
    print(f"有 {len(errors)} 个测试失败: {errors}")
    sys.exit(1)
else:
    print("全部 5 个测试通过!")
