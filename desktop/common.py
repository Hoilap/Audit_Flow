"""desktop.common — 共享状态、工具函数和 Pydantic 模型。

所有 routes_*.py 模块都从此处导入公共依赖，避免循环引用。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

import yaml as yaml_lib
from pydantic import BaseModel
from git import Repo, InvalidGitRepositoryError
from dotenv import load_dotenv

load_dotenv()

# ---------- Logging configuration ----------
_log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log_file = os.path.join(_log_dir, "audit_workflow.log")
_handler = RotatingFileHandler(_log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger = logging.getLogger("audit_workflow")
logger.setLevel(logging.INFO)
_handler_ref = _handler  # keep reference for routes that need log path
logger.addHandler(_handler)


# ── 全局 token 追踪器 ──


class TokenTracker:
    """会话级 token 用量追踪器。

    所有 LLM 调用完成后都应通过此类记录 usage，确保前端顶部 token 栏
    能正确反映当前会话的累计消耗。
    """

    def __init__(self):
        self._usage: dict[str, int] = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    # ── 核心记录方法 ──

    def record(self, usage: dict | None) -> None:
        """记录一次 LLM 调用的 usage dict。None / 空 dict 安全。"""
        if not usage:
            return
        for k in self._usage:
            self._usage[k] += usage.get(k, 0)
        # 实时推送最新 token 累计到前端
        notify_frontend("token_updated", self.snapshot())

    def record_from_config(self, cfg: dict, *step_names: str) -> None:
        """从 ``cfg["_cleaning"][step]["usage"]`` 提取并记录。

        用于 OSM pipeline 调用后——pipeline 会把 usage 写进
        ``cfg["_cleaning"]`` 子字典中。

        Args:
            cfg: pipeline 执行后的 config dict。
            *step_names: 要提取的 step 名称，如 ``"settlement"``,
                ``"outbound"``。
        """
        cleaning = cfg.get("_cleaning", {})
        for step in step_names:
            info = cleaning.get(step, {})
            self.record(info.get("usage"))

    # ── 查询方法 ──

    def snapshot(self) -> dict:
        """返回当前累计的副本。"""
        return dict(self._usage)

    def get(self, key: str, default: int = 0) -> int:
        """兼容旧 ``_token_usage.get()`` 调用。"""
        return self._usage.get(key, default)


token_tracker = TokenTracker()


# ── SSE 事件队列 ──

_event_queue: asyncio.Queue = asyncio.Queue(maxsize=256)


def notify_frontend(event: str, data: dict) -> None:
    """非阻塞地将事件塞入 SSE 队列，供前端 EventSource 消费。"""
    try:
        _event_queue.put_nowait({"event": event, "data": data})
    except asyncio.QueueFull:
        logger.warning("SSE queue full, dropping event: %s", event)


# ---------- SQLite project database ----------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'projects.db')

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT NOT NULL,
            customer_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Planning',
            created_at TEXT NOT NULL DEFAULT '',
            responsible_person TEXT NOT NULL DEFAULT '',
            risk TEXT NOT NULL DEFAULT 'Medium'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            task_name TEXT NOT NULL,
            task_yml_path TEXT NOT NULL DEFAULT '',
            llm_yml_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(customer_name, task_name)
        )
    """)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            pydantic_blob BLOB
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (conversation_id) REFERENCES agent_conversations(id) ON DELETE CASCADE
        )
    """)
    # seed default projects if empty
    count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    if count == 0:
        defaults = [
            ('序时账银行流水匹配', '桂平金山', 'Reviewing', '2026-06-08', '审计一组', 'High'),
            ('出库表核对', 'A 公司', 'Planning', '2026-06-13', 'CPA', 'Medium'),
        ]
        conn.executemany(
            "INSERT INTO projects (task_name, customer_name, status, created_at, responsible_person, risk) VALUES (?,?,?,?,?,?)",
            defaults
        )
        conn.commit()

    conn.close()


# ---------- Pydantic models ----------


class ProjectCreate(BaseModel):
    task_name: str
    customer_name: str = ''
    status: str = 'Planning'
    created_at: str = ''
    responsible_person: str = ''
    risk: str = 'Medium'

class ProjectUpdate(BaseModel):
    task_name: Optional[str] = None
    customer_name: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    responsible_person: Optional[str] = None
    risk: Optional[str] = None


class WritePayload(BaseModel):
    path: str
    content: str
    commit_message: str = "update from desktop app"


class AgentChatPayload(BaseModel):
    message: str
    conversation_id: str = ""
    customer_name: str = ""
    task_name: str = ""


def project_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "task_name": row["task_name"],
        "customer_name": row["customer_name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "responsible_person": row["responsible_person"],
        "risk": row["risk"],
    }


# ---------- Helper functions ----------


def _ensure_project_dirs(customer_name: str, task_name: str):
    """确保 inputs/客户名称/任务名称 和 outputs/客户名称/任务名称 目录存在"""
    for base in ('inputs', 'outputs'):
        d = os.path.join(base, customer_name, task_name)
        os.makedirs(d, exist_ok=True)


def _project_root() -> str:
    """返回项目根目录（desktop/ 的上级目录）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_llm_yml_path() -> str:
    """llm.yml 的默认路径（config/config.llm.yml）。"""
    return os.path.join(_project_root(), "config", "config.llm.yml")


def _default_matching_yml_path() -> str:
    """matching.yml 的默认路径（config/config.matching.yml）。"""
    return os.path.join(_project_root(), "config", "config.matching.yml")


def _load_llm_config() -> dict:
    """加载 llm.yml 配置。"""
    llm_yml_path = _default_llm_yml_path()
    if os.path.exists(llm_yml_path):
        with open(llm_yml_path, "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}
    else:
        llm_cfg = {"llm": {"enabled": False}}

    matching_yml_path = _default_matching_yml_path()
    if os.path.exists(matching_yml_path):
        with open(matching_yml_path, "r", encoding="utf-8") as f:
            matching_cfg = yaml_lib.safe_load(f) or {}
        llm_cfg.setdefault("llm", {})
        llm_cfg["llm"]["matching"] = matching_cfg.get("matching", {})

    return llm_cfg


def _default_llm_config() -> dict:
    """从 config.llm.yml 加载 LLM 配置（支持多 provider），
    供 /llm/generate 等端点使用。若加载失败则回退到环境变量。"""
    try:
        full_cfg = _load_llm_config()
        llm_cfg = full_cfg.get("llm", {})
        if llm_cfg.get("providers"):
            return llm_cfg
    except Exception:
        pass
    # 回退：从环境变量构建 flat 配置
    return {
        "api_key_env": "DASHSCOPE_API_KEY",
        "model_env": "DASHSCOPE_MODEL",
        "base_url_env": "DASHSCOPE_BASE_URL",
    }


def _build_llm_target_path(customer_name: str, task_name: str, ext: str = ".py") -> str:
    """构造 LLM 生成代码的目标路径：outputs/{客户}/{任务}/llm_code/generated_{时间戳}{ext}"""
    if not customer_name:
        customer_name = "_default"
    if not task_name:
        task_name = "_default"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("outputs", customer_name, task_name, "llm_code", f"generated_{ts}{ext}")


def _resolve_task_dir_name(task_name: str) -> str:
    """将任务名解析为英文目录名。
    从 config/config.task_definitions.yml 查找：
    优先匹配 dir_name，其次匹配中文 name → dir_name，都无匹配则原样返回。
    """
    return _task_def_dir_name(task_name)


def _list_project_dirs() -> list:
    """扫描 inputs/ 和 outputs/ 下实际存在的项目目录，返回去重后的 (customer_name, task_name) 列表"""
    result = []
    seen = set()
    for base in ('inputs', 'outputs'):
        base_path = os.path.join(os.getcwd(), base)
        if not os.path.isdir(base_path):
            continue
        for customer in os.listdir(base_path):
            customer_path = os.path.join(base_path, customer)
            if not os.path.isdir(customer_path):
                continue
            for task in os.listdir(customer_path):
                task_path = os.path.join(customer_path, task)
                if os.path.isdir(task_path):
                    key = (customer, task)
                    if key not in seen:
                        seen.add(key)
                        result.append({'customer_name': customer, 'task_name': task})
    return result


def find_repo(path: str = None) -> Repo:
    root = path or os.getcwd()
    try:
        return Repo(root, search_parent_directories=True)
    except InvalidGitRepositoryError:
        # initialize repo
        repo = Repo.init(root)
        return repo


# ── 任务定义（只读配置，替代原 DB task_definitions 表）──

_TASK_DEFS_CACHE: list[dict] | None = None

def _load_task_definitions() -> list[dict]:
    """从 config/config.task_definitions.yml 加载任务定义。"""
    global _TASK_DEFS_CACHE
    if _TASK_DEFS_CACHE is not None:
        return _TASK_DEFS_CACHE
    yml_path = os.path.join(_project_root(), "config", "config.task_definitions.yml")
    if os.path.exists(yml_path):
        with open(yml_path, "r", encoding="utf-8") as f:
            data = yaml_lib.safe_load(f) or {}
        _TASK_DEFS_CACHE = data.get("tasks", [])
    else:
        _TASK_DEFS_CACHE = []
    return _TASK_DEFS_CACHE


def _task_def_by_name(name: str) -> dict | None:
    """按中文名或 dir_name 查找任务定义。"""
    for td in _load_task_definitions():
        if td.get("name") == name or td.get("dir_name") == name:
            return td
    return None


def _task_def_dir_name(task_name: str) -> str:
    """将任务名解析为英文目录名（替代原 DB _resolve_task_dir_name）。"""
    if not task_name:
        return task_name
    td = _task_def_by_name(task_name)
    if td:
        return td["dir_name"]
    return task_name


def _migrate_config_files():
    """一次性迁移：将 config.example.*.yml 复制到 config/config.*.yml（幂等）。"""
    root = _project_root()
    config_dir = os.path.join(root, "config")
    migrations = [
        ("config.example.llm.yml", "config.llm.yml"),
        ("config.example.matching.yml", "config.matching.yml"),
    ]
    for old_name, new_name in migrations:
        old_path = os.path.join(root, old_name)
        new_path = os.path.join(config_dir, new_name)
        if os.path.exists(old_path) and not os.path.exists(new_path):
            os.makedirs(config_dir, exist_ok=True)
            shutil.copy2(old_path, new_path)
            logger.info("Migrated config: %s -> %s", old_name, os.path.join("config", new_name))


def _resolve_masked_key(pcfg: dict) -> tuple[str, bool, str]:
    """解析并遮罩 provider 的 API Key。
    Returns: (masked_key, is_set, source_label)
    """
    direct = (pcfg.get("api_key") or "").strip()
    if direct:
        masked = "****" + direct[-4:] if len(direct) > 4 else "****"
        return masked, True, "direct"

    env_name = (pcfg.get("api_key_env") or "").strip()
    if env_name:
        val = os.getenv(env_name, "")
        if val:
            masked = "****" + val[-4:] if len(val) > 4 else "****"
            return masked, True, "env"
        # 向后兼容：api_key_env 字段存储了字面量 key
        if not env_name.startswith(("$", "ENV_", "env_")) and len(env_name) > 20:
            masked = "****" + env_name[-4:] if len(env_name) > 4 else "****"
            return masked, True, "literal"

    fallback = os.getenv("OPENAI_API_KEY", "")
    if fallback:
        masked = "****" + fallback[-4:] if len(fallback) > 4 else "****"
        return masked, True, "fallback"

    return "", False, "none"


def _resolve_plain_key(pcfg: dict) -> str:
    """解析 provider 的明文 API Key（用于眼睛切换显示）。"""
    direct = (pcfg.get("api_key") or "").strip()
    if direct:
        return direct
    env_name = (pcfg.get("api_key_env") or "").strip()
    if env_name:
        val = os.getenv(env_name, "")
        if val:
            return val
        if not env_name.startswith(("$", "ENV_", "env_")) and len(env_name) > 20:
            return env_name
    return os.getenv("OPENAI_API_KEY", "")


def _resolve_task_config_paths(customer_name: str, task_name: str) -> dict:
    """从 DB 查询 (customer_name, task_name) 对应的 yml 路径映射。
    若 DB 无记录则返回默认路径。
    Returns: {"task_yml_path": str, "llm_yml_path": str}
    """
    conn = get_db()
    row = conn.execute(
        "SELECT task_yml_path, llm_yml_path FROM task_configs WHERE customer_name=? AND task_name=?",
        (customer_name, task_name),
    ).fetchone()
    conn.close()

    default_llm = _default_llm_yml_path()

    if row and row["task_yml_path"]:
        llm_path = row["llm_yml_path"] or default_llm
        # 兼容旧路径：DB 存储的旧文件不存在但新默认路径存在时，使用新路径
        if llm_path and not os.path.exists(llm_path) and os.path.exists(default_llm):
            llm_path = default_llm
        return {
            "task_yml_path": row["task_yml_path"],
            "llm_yml_path": llm_path,
        }

    # fallback 默认路径
    return {
        "task_yml_path": os.path.join(_project_root(), "outputs", customer_name, task_name, "task.yml"),
        "llm_yml_path": default_llm,
    }


def _upsert_task_config(customer_name: str, task_name: str, task_yml_path: str = "", llm_yml_path: str = ""):
    """写入或更新 task_configs 映射表。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute(
        """INSERT INTO task_configs (customer_name, task_name, task_yml_path, llm_yml_path, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(customer_name, task_name) DO UPDATE SET
               task_yml_path=excluded.task_yml_path,
               llm_yml_path=excluded.llm_yml_path,
               updated_at=excluded.updated_at""",
        (customer_name, task_name, task_yml_path, llm_yml_path, now, now),
    )
    conn.commit()
    conn.close()


# ── 工作流共享状态 ──

_workflow_state: dict = {}


def _resolve_workflow_state_key(customer_name: str, task_name: str) -> str:
    return f"{customer_name}/{task_name}"


# ── 日志文件路径（供 /logs 端点使用）──

def get_log_file_path() -> str:
    return _log_file
