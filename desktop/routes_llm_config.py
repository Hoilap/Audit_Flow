"""desktop.routes_llm_config — LLM 配置管理 CRUD 端点 + token 查询。"""

import os
import traceback

import yaml as yaml_lib
from fastapi import APIRouter, HTTPException, Form

from .common import (
    logger,
    token_tracker,
    _default_llm_yml_path,
    _resolve_masked_key,
    _resolve_plain_key,
)

router = APIRouter()


@router.get("/llm/config")
def get_llm_config():
    """读取 llm.yml 中的 LLM 配置，返回 providers 列表和当前默认 provider。"""
    llm_yml_path = _default_llm_yml_path()
    if not os.path.exists(llm_yml_path):
        return {"ok": False, "error": "llm.yml 不存在", "config": {}}
    with open(llm_yml_path, "r", encoding="utf-8") as f:
        llm_cfg = yaml_lib.safe_load(f) or {}
    llm_section = llm_cfg.get("llm", {})

    providers = llm_section.get("providers")
    if providers and isinstance(providers, dict):
        # providers 格式：返回所有 provider 的名称和 model，以及当前 default
        default_name = llm_section.get("default", next(iter(providers)))
        provider_list = []
        for name, pcfg in providers.items():
            masked, key_set, source = _resolve_masked_key(pcfg)
            provider_list.append({
                "name": name,
                "model": pcfg.get("model", name),
                "base_url": pcfg.get("base_url", ""),
                "api_key_masked": masked,
                "api_key_set": key_set,
                "api_key_env": pcfg.get("api_key_env", ""),
                "api_key_source": source,
            })
        return {
            "ok": True,
            "config": {
                "enabled": llm_section.get("enabled", False),
                "default": default_name,
                "providers": provider_list,
            },
            "path": llm_yml_path,
        }
    else:
        # 旧扁平格式：包装成单个 provider
        model = llm_section.get("model", "")
        masked, key_set, source = _resolve_masked_key(llm_section)
        return {
            "ok": True,
            "config": {
                "enabled": llm_section.get("enabled", False),
                "default": "_default",
                "providers": [{
                    "name": "_default",
                    "model": model or "unknown",
                    "base_url": llm_section.get("base_url", ""),
                    "api_key_masked": masked,
                    "api_key_set": key_set,
                    "api_key_env": llm_section.get("api_key_env", ""),
                    "api_key_source": source,
                }],
            },
            "path": llm_yml_path,
        }


@router.post("/llm/config")
def update_llm_config(
    default_provider: str = Form(""),
    enabled: str = Form(""),
):
    """更新 llm.yml 中的默认 provider 或启用状态。"""
    llm_yml_path = _default_llm_yml_path()
    if os.path.exists(llm_yml_path):
        with open(llm_yml_path, "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}
    else:
        llm_cfg = {}

    if "llm" not in llm_cfg:
        llm_cfg["llm"] = {}

    if default_provider:
        providers = llm_cfg["llm"].get("providers", {})
        if default_provider in providers:
            llm_cfg["llm"]["default"] = default_provider
        else:
            # 旧格式兼容：直接把 default_provider 当 model 名写入
            llm_cfg["llm"]["model"] = default_provider

    if enabled in ("true", "false"):
        llm_cfg["llm"]["enabled"] = enabled == "true"

    os.makedirs(os.path.dirname(llm_yml_path), exist_ok=True)
    with open(llm_yml_path, "w", encoding="utf-8") as f:
        yaml_lib.dump(llm_cfg, f, allow_unicode=True, default_flow_style=False)

    return {"ok": True, "path": llm_yml_path}


@router.get("/llm/config/provider/{provider_name}/key")
def get_provider_key(provider_name: str):
    """返回单个 provider 的明文 API Key（用于眼睛切换显示）。"""
    llm_yml_path = _default_llm_yml_path()
    if not os.path.exists(llm_yml_path):
        return {"ok": False, "error": "llm.yml 不存在"}
    with open(llm_yml_path, "r", encoding="utf-8") as f:
        llm_cfg = yaml_lib.safe_load(f) or {}
    providers = llm_cfg.get("llm", {}).get("providers", {})
    if provider_name not in providers:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    return {"ok": True, "api_key": _resolve_plain_key(providers[provider_name])}


@router.put("/llm/config/providers")
def update_llm_providers(payload: dict):
    """批量更新 providers 的 api_key / base_url / model。
    api_key 为空字符串时表示用户未修改，不覆盖原值。
    """
    llm_yml_path = _default_llm_yml_path()
    if os.path.exists(llm_yml_path):
        with open(llm_yml_path, "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}
    else:
        llm_cfg = {"llm": {"providers": {}}}

    if "llm" not in llm_cfg:
        llm_cfg["llm"] = {}
    providers = llm_cfg["llm"].setdefault("providers", {})

    for upd in payload.get("providers", []):
        name = upd.get("name", "")
        if name not in providers:
            continue
        pcfg = providers[name]

        # API Key: 仅当用户输入了新值时才覆盖
        new_key = (upd.get("api_key") or "").strip()
        if new_key:
            pcfg["api_key"] = new_key

        # Base URL: 始终更新（允许清空）
        if "base_url" in upd:
            pcfg["base_url"] = upd["base_url"]

        # Model: 仅当非空时更新
        new_model = (upd.get("model") or "").strip()
        if new_model:
            pcfg["model"] = new_model

    os.makedirs(os.path.dirname(llm_yml_path), exist_ok=True)
    with open(llm_yml_path, "w", encoding="utf-8") as f:
        yaml_lib.dump(llm_cfg, f, allow_unicode=True, default_flow_style=False)

    return {"ok": True, "path": llm_yml_path}


@router.post("/llm/config/providers")
def create_llm_provider(payload: dict):
    """添加新的 LLM provider。"""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Provider 名称不能为空")

    llm_yml_path = _default_llm_yml_path()
    if os.path.exists(llm_yml_path):
        with open(llm_yml_path, "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}
    else:
        llm_cfg = {"llm": {"providers": {}}}

    if "llm" not in llm_cfg:
        llm_cfg["llm"] = {}
    providers = llm_cfg["llm"].setdefault("providers", {})

    if name in providers:
        raise HTTPException(status_code=409, detail=f"Provider '{name}' 已存在")

    new_pcfg = {}
    if payload.get("api_key"):
        new_pcfg["api_key"] = payload["api_key"]
    elif payload.get("api_key_env"):
        new_pcfg["api_key_env"] = payload["api_key_env"]
    if payload.get("base_url"):
        new_pcfg["base_url"] = payload["base_url"]
    if payload.get("model"):
        new_pcfg["model"] = payload["model"]

    providers[name] = new_pcfg

    os.makedirs(os.path.dirname(llm_yml_path), exist_ok=True)
    with open(llm_yml_path, "w", encoding="utf-8") as f:
        yaml_lib.dump(llm_cfg, f, allow_unicode=True, default_flow_style=False)

    return {"ok": True, "path": llm_yml_path, "name": name}


@router.delete("/llm/config/providers/{provider_name}")
def delete_llm_provider(provider_name: str):
    """删除指定的 LLM provider。"""
    llm_yml_path = _default_llm_yml_path()
    if not os.path.exists(llm_yml_path):
        raise HTTPException(status_code=404, detail="llm.yml 不存在")
    with open(llm_yml_path, "r", encoding="utf-8") as f:
        llm_cfg = yaml_lib.safe_load(f) or {}

    providers = llm_cfg.get("llm", {}).get("providers", {})
    if provider_name not in providers:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' 不存在")
    if len(providers) <= 1:
        raise HTTPException(status_code=400, detail="不能删除最后一个 provider")

    del providers[provider_name]

    # 若删除的是当前 default，自动切换到第一个剩余 provider
    new_default = None
    if llm_cfg["llm"].get("default") == provider_name:
        new_default = next(iter(providers))
        llm_cfg["llm"]["default"] = new_default

    with open(llm_yml_path, "w", encoding="utf-8") as f:
        yaml_lib.dump(llm_cfg, f, allow_unicode=True, default_flow_style=False)

    return {"ok": True, "new_default": new_default}


@router.get("/llm/tokens")
def get_llm_tokens():
    """获取当前会话累计 token 消耗。"""
    snap = token_tracker.snapshot()
    return {
        "ok": True,
        "total_tokens": snap["total_tokens"],
        "prompt_tokens": snap["prompt_tokens"],
        "completion_tokens": snap["completion_tokens"],
    }
