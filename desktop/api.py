import asyncio
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from git import Repo, InvalidGitRepositoryError
from typing import List, Optional
import json
import subprocess
import csv
from fastapi import BackgroundTasks
from fastapi import Form
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi import Depends
import importlib
import yaml as yaml_lib
from audit_workflow.bank_ledger_match import pipeline as blm_pipeline
from audit_workflow.bank_ledger_match import file_detector
from audit_workflow.outbound_settlement_match import pipeline as osm_pipeline
from audit_workflow.llm_agent import llm_generate as audit_llm_generate
from audit_workflow.agent_loop import run_agent_chat, conversation_store
from dotenv import load_dotenv

load_dotenv()

# ---------- Logging configuration ----------
_log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log_file = os.path.join(_log_dir, "audit_workflow.log")
_handler = RotatingFileHandler(_log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger = logging.getLogger("audit_workflow")
logger.setLevel(logging.INFO)
logger.addHandler(_handler)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    # ── task_definitions: 任务类型注册表（中文名 ↔ 英文目录名） ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            dir_name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT ''
        )
    """)
    td_count = conn.execute("SELECT COUNT(*) FROM task_definitions").fetchone()[0]
    if td_count == 0:
        task_defs = [
            ('序时账银行流水匹配', 'bank_ledger_match', 'Detect扫描→LLM识别→清洗→核查→匹配→人工复核→填入底稿'),
            ('出库结算匹配', 'outbound_settlement_match', '出库表与结算单数据匹配核查'),
            ('出库表核对', 'outbound_check', '多平台出库数据交叉核对'),
            ('资金流水专项复核', 'cash_flow_review', '资金流水专项审计复核'),
        ]
        conn.executemany(
            "INSERT INTO task_definitions (name, dir_name, description) VALUES (?,?,?)",
            task_defs
        )
        conn.commit()

    conn.close()

init_db()


def _ensure_project_dirs(customer_name: str, task_name: str):
    """确保 inputs/客户名称/任务名称 和 outputs/客户名称/任务名称 目录存在"""
    for base in ('inputs', 'outputs'):
        d = os.path.join(base, customer_name, task_name)
        os.makedirs(d, exist_ok=True)


def _resolve_task_dir_name(task_name: str) -> str:
    """将任务名解析为英文目录名。
    优先匹配 dir_name，其次匹配中文 name → dir_name，都无匹配则原样返回。
    """
    if not task_name:
        return task_name
    conn = get_db()
    try:
        # 已经是英文 dir_name → 直接返回
        row = conn.execute(
            "SELECT dir_name FROM task_definitions WHERE dir_name = ?", (task_name,)
        ).fetchone()
        if row:
            return row["dir_name"]
        # 中文 name → 翻译为英文 dir_name
        row = conn.execute(
            "SELECT dir_name FROM task_definitions WHERE name = ?", (task_name,)
        ).fetchone()
        if row:
            return row["dir_name"]
    finally:
        conn.close()
    # 自定义名称 → 原样返回
    return task_name


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

def project_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "task_name": row["task_name"],
        "customer_name": row["customer_name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "responsible_person": row["responsible_person"],
        "risk": row["risk"],
        "dir_name": row["dir_name"] if "dir_name" in row.keys() else None,
    }

# ---------- Project CRUD endpoints ----------

@app.get("/projects/dirs")
def list_project_dirs():
    """返回 inputs/ 和 outputs/ 下按项目组织的目录列表"""
    return {"dirs": _list_project_dirs()}

@app.get("/task-definitions")
def list_task_definitions():
    """返回所有任务类型定义（中文名 ↔ 英文目录名映射）"""
    conn = get_db()
    rows = conn.execute("SELECT id, name, dir_name, description FROM task_definitions ORDER BY id").fetchall()
    conn.close()
    return {"definitions": [dict(r) for r in rows]}

@app.get("/projects/list")
def list_projects():
    conn = get_db()
    rows = conn.execute("""
        SELECT p.*, td.dir_name
        FROM projects p
        LEFT JOIN task_definitions td ON p.task_name = td.name
        ORDER BY p.id DESC
    """).fetchall()
    conn.close()
    return {"projects": [project_row_to_dict(r) for r in rows]}

@app.post("/projects/create")
def create_project(payload: ProjectCreate):
    logger.info("创建项目: customer=%s, task=%s", payload.customer_name, payload.task_name)
    created_at = payload.created_at or datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO projects (task_name, customer_name, status, created_at, responsible_person, risk) VALUES (?,?,?,?,?,?)",
        (payload.task_name, payload.customer_name, payload.status, created_at, payload.responsible_person, payload.risk)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    # 自动创建项目目录结构：inputs/客户名称/任务名称 和 outputs/客户名称/任务名称
    _ensure_project_dirs(payload.customer_name, payload.task_name)
    return {"project": project_row_to_dict(row)}

@app.put("/projects/{project_id}")
def update_project(project_id: int, payload: ProjectUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="项目不存在")
    updates = {}
    for field in ("task_name", "customer_name", "status", "created_at", "responsible_person", "risk"):
        val = getattr(payload, field)
        if val is not None:
            updates[field] = val
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE projects SET {set_clause} WHERE id=?", (*updates.values(), project_id))
        conn.commit()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    conn.close()
    return {"project": project_row_to_dict(row)}

@app.delete("/projects/{project_id}")
def delete_project(project_id: int):
    logger.info("删除项目: id=%s", project_id)
    conn = get_db()
    existing = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="项目不存在")
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


def find_repo(path: str = None) -> Repo:
    root = path or os.getcwd()
    try:
        return Repo(root, search_parent_directories=True)
    except InvalidGitRepositoryError:
        # initialize repo
        repo = Repo.init(root)
        return repo


@app.get("/files/list")
def list_files(root: str = "outputs"):
    base = os.path.abspath(root)
    if not os.path.exists(base):
        return {"files": []}
    _hidden_dirs = {"agent_code", "__pycache__"}
    result = []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _hidden_dirs]
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), start=os.getcwd())
            result.append(rel.replace('\\', '/'))
    return {"files": result}


@app.get("/files/read")
def read_file(path: str):
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="File not found")
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return {"content": f.read()}


class WritePayload(BaseModel):
    path: str
    content: str
    commit_message: str = "update from desktop app"


@app.post("/files/write")
def write_file(payload: WritePayload):
    p = os.path.abspath(payload.path)
    d = os.path.dirname(p)
    os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(payload.content)
    repo = find_repo(p)
    try:
        repo.index.add([os.path.relpath(p, repo.working_tree_dir)])
        repo.index.commit(payload.commit_message)
    except Exception:
        pass
    return {"ok": True, "path": os.path.relpath(p)}


@app.delete("/files/delete")
def delete_file(path: str):
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(p)
    return {"ok": True, "path": os.path.relpath(p)}


@app.post("/git/commit")
def git_commit(message: str = "commit from desktop app"):
    repo = find_repo()
    repo.git.add(all=True)
    repo.index.commit(message)
    return {"ok": True}


@app.post("/git/revert")
def git_revert(path: str):
    repo = find_repo()
    try:
        repo.git.checkout("--", path)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/git/log")
def git_log(limit: int = 20):
    repo = find_repo()
    commits = []
    for c in list(repo.iter_commits(max_count=limit)):
        commits.append({"hexsha": c.hexsha, "message": c.message, "author": str(c.author), "date": c.committed_datetime.isoformat()})
    return {"commits": commits}



@app.post("/files/upload")
async def upload_file(file: UploadFile = File(...), dest: str = Form("outputs/")):
    dest_path = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    full_path = dest_path if os.path.splitext(dest_path)[1] else os.path.join(dest_path, file.filename)
    with open(full_path, "wb") as f:
        content = await file.read()
        f.write(content)
    logger.info("上传文件: %s -> %s", file.filename, os.path.relpath(full_path))
    repo = find_repo(full_path)
    try:
        repo.index.add([os.path.relpath(full_path, repo.working_tree_dir)])
        repo.index.commit(f"upload {file.filename}")
    except Exception:
        pass
    return {"ok": True, "path": os.path.relpath(full_path)}


@app.post("/files/copy-from-path")
async def copy_from_path(source: str = Form(...), dest: str = Form("inputs/")):
    """从系统路径复制文件或目录到项目目录中。"""
    project_root = _project_root()
    source_path = os.path.abspath(source)

    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail=f"源路径不存在: {source}")

    # 目标路径
    dest_path = os.path.join(project_root, dest)
    os.makedirs(dest_path, exist_ok=True)

    basename = os.path.basename(source_path)
    target = os.path.join(dest_path, basename)

    try:
        if os.path.isdir(source_path):
            if os.path.exists(target):
                shutil.rmtree(target)
            shutil.copytree(source_path, target)
        else:
            shutil.copy2(source_path, target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"复制失败: {e}")

    rel = os.path.relpath(target, project_root)
    logger.info("复制文件: %s -> %s", source_path, rel)

    # 尝试 git commit
    repo = find_repo(target)
    try:
        repo.index.add([os.path.relpath(target, repo.working_tree_dir)])
        repo.index.commit(f"add {basename} (drag-drop)")
    except Exception:
        pass

    return {"ok": True, "path": rel}


@app.post("/workflow/bank_ledger_match/match")
def workflow_match(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    logger.info("开始 Match: customer=%s, task=%s", customer_name, task_name)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        matches, unmatched_bank, unmatched_ledger = blm_pipeline.run_match(cfg)
        return {"matches": str(matches), "unmatched_bank": str(unmatched_bank), "unmatched_ledger": str(unmatched_ledger)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/bank_ledger_match/approve")
def workflow_approve(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        a, b, c, d = blm_pipeline.run_approve(cfg)
        return {"result": [str(x) for x in (a, b, c, d)]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/bank_ledger_match/verify")
def workflow_verify(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """Step - Verify: 将人工复核通过的项目加入 matches.csv，从 unmatched 中移除。"""
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        if not cfg:
            raise HTTPException(status_code=400, detail="缺少配置参数")
        # 调用 approver 执行实际的人工复核合并逻辑
        matches_path, unmatched_bank_path, unmatched_ledger_path, review_path = blm_pipeline.run_approve(cfg)
        # 统计行数
        def _count_rows(p):
            if not p.exists():
                return 0
            with open(p, "r", encoding="utf-8", errors="ignore", newline="") as f:
                return max(sum(1 for _ in csv.reader(f)) - 1, 0)
        result = {
            "ok": True,
            "matches": {"path": str(matches_path), "rows": _count_rows(matches_path)},
            "unmatched_bank": {"path": str(unmatched_bank_path), "rows": _count_rows(unmatched_bank_path)},
            "unmatched_ledger": {"path": str(unmatched_ledger_path), "rows": _count_rows(unmatched_ledger_path)},
            "review": {"path": str(review_path), "rows": _count_rows(review_path)},
        }
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _default_llm_config() -> dict:
    """从 config.example.llm.yml 加载 LLM 配置（支持多 provider），
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


@app.post("/llm/generate")
def llm_generate(
    prompt: str = Form(...),
    target_path: str = Form(""),
    system_prompt: str = Form(""),
    customer_name: str = Form(""),
    task_name: str = Form(""),
):
    if not target_path:
        target_path = _build_llm_target_path(customer_name, task_name, ext=".txt")
    logger.info("LLM 生成请求: target=%s, prompt_len=%d, system_prompt_len=%d",
                target_path, len(prompt), len(system_prompt))
    usage_info = {}
    try:
        content, usage_info = audit_llm_generate(
            _default_llm_config(), prompt,
            system_prompt=system_prompt or "",
        )
        token_tracker.record(usage_info)
        logger.info("LLM 生成完成: tokens=%s", usage_info.get("total_tokens", 0))
    except RuntimeError as e:
        content = f"[LLM not configured] {e}\nPrompt received:\n{prompt}"

    if target_path.endswith('.py'):
        content = _strip_code_fences(content)

    p = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": os.path.relpath(p), "ok": True, "usage": usage_info}


@app.post("/llm/generate_and_run")
def llm_generate_and_run(
    prompt: str = Form(...),
    target_path: str = Form(""),
    run_code: str = Form("false"),
    timeout: int = Form(5),
    system_prompt: str = Form(""),
    customer_name: str = Form(""),
    task_name: str = Form(""),
):
    import ast
    import sys
    if not target_path:
        target_path = _build_llm_target_path(customer_name, task_name, ext=".py")
    logger.info("LLM 生成并执行: target=%s, run=%s, timeout=%ds, system_prompt_len=%d",
                target_path, run_code, timeout, len(system_prompt))
    run_flag = str(run_code).lower() in ("1", "true", "yes", "on")
    need_check = target_path.endswith('.py') and run_flag

    max_retries = 3
    content = ""
    run_result = None
    last_error: str | None = None       # 最终错误描述（语法 or 运行）
    last_error_type: str | None = None  # "syntax" | "runtime" | None
    total_usage: dict = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    current_prompt = prompt

    for attempt in range(max_retries + 1):
        # ── 1. LLM 生成 ──
        try:
            content, usage_info = audit_llm_generate(
                _default_llm_config(), current_prompt,
                system_prompt=system_prompt or "",
            )
            token_tracker.record(usage_info)
            for k in total_usage:
                total_usage[k] += usage_info.get(k, 0)
            logger.info("LLM 生成完成: tokens=%s (attempt %d/%d)",
                        usage_info.get("total_tokens", 0), attempt + 1, max_retries + 1)
        except RuntimeError as e:
            if run_flag and target_path.endswith('.py'):
                content = prompt
            else:
                content = f"[LLM not configured] {e}\nPrompt received:\n{prompt}"
            break

        # ── 1.5 剥离 markdown 代码块标记 ──
        if target_path.endswith('.py'):
            content = _strip_code_fences(content)

        # ── 2. 保存文件（每次都保存，包括出错的版本）──
        p = os.path.abspath(target_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

        if not need_check:
            break  # 无需校验/执行，直接结束

        # ── 3. 语法检查 ──
        try:
            ast.parse(content)
        except SyntaxError as e:
            last_error = str(e)
            last_error_type = "syntax"
            logger.warning("LLM 代码语法错误 (attempt %d/%d): %s",
                           attempt + 1, max_retries + 1, e)
            if attempt < max_retries:
                logger.info("自动重试修复语法错误 (attempt %d → %d)", attempt + 1, attempt + 2)
                notify_frontend("retry", {
                    "reason": "syntax",
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "error": str(e),
                })
                current_prompt = _build_retry_prompt(
                    original=prompt,
                    generated_code=content,
                    error_type="语法错误",
                    error_detail=str(e),
                )
                continue
            break  # 重试耗尽

        # ── 4. 执行代码 ──
        run_result = None
        exec_error_msg = None
        try:
            proc = subprocess.run(
                [sys.executable, p], capture_output=True, text=True, timeout=timeout,
            )
            run_result = {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
            logger.info("LLM 代码执行完成: returncode=%d", proc.returncode)
            if proc.returncode != 0:
                # 截取关键错误信息，避免 prompt 过长
                stderr_snippet = (proc.stderr or "").strip()
                stdout_snippet = (proc.stdout or "").strip()
                parts = [f"returncode={proc.returncode}"]
                if stderr_snippet:
                    parts.append(f"stderr:\n{_tail_lines(stderr_snippet, 30)}")
                if stdout_snippet and not stderr_snippet:
                    parts.append(f"stdout:\n{_tail_lines(stdout_snippet, 30)}")
                exec_error_msg = "\n".join(parts)
        except subprocess.TimeoutExpired:
            run_result = {"error": "timeout"}
            exec_error_msg = f"执行超时（>{timeout}s）"
            logger.warning("LLM 代码执行超时: %s", target_path)

        if exec_error_msg is None:
            # 执行成功
            last_error = None
            last_error_type = None
            break
        else:
            last_error = exec_error_msg
            last_error_type = "runtime"
            logger.warning("LLM 代码运行错误 (attempt %d/%d): %s",
                           attempt + 1, max_retries + 1, exec_error_msg)
            if attempt < max_retries:
                logger.info("自动重试修复运行错误 (attempt %d → %d)", attempt + 1, attempt + 2)
                notify_frontend("retry", {
                    "reason": "runtime",
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "error": exec_error_msg[:200],
                })
                current_prompt = _build_retry_prompt(
                    original=prompt,
                    generated_code=content,
                    error_type="运行错误",
                    error_detail=exec_error_msg,
                )
                continue
            break  # 重试耗尽

    # ── 5. 最终保存（确保最后一次生成的代码落盘）──
    p = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)

    # 如果循环中已经执行过且成功，复用之前的 run_result；否则置 None
    if last_error is not None:
        run_result = None

    resp: dict = {
        "path": os.path.relpath(p),
        "ok": last_error is None,
        "run_result": run_result,
        "usage": total_usage,
        "retries": attempt,
    }
    if last_error:
        resp["error_type"] = last_error_type
        resp["error_detail"] = last_error
    return resp


def _strip_code_fences(text: str) -> str:
    """剥离 LLM 输出中的 markdown 代码块标记，提取纯代码。"""
    import re
    # 优先匹配 ```python ... ```
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 退而匹配 ``` ... ```
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _build_retry_prompt(
    original: str,
    generated_code: str,
    error_type: str,
    error_detail: str,
) -> str:
    """构造重试提示词模板，引导 LLM 修复上一次生成的代码。"""
    # 截断过长的代码，避免 token 爆炸
    code_block = generated_code
    if len(code_block) > 4000:
        code_block = code_block[:4000] + "\n... (truncated)"
    return (
        f"你上一次生成的 Python 代码存在{error_type}，请修复后重新输出完整代码。\n"
        f"直接输出纯 Python 代码，不要用 ``` 代码块包裹，不要任何额外解释。\n\n"
        f"## {error_type}\n```\n{error_detail}\n```\n\n"
        f"## 上次生成的代码\n```python\n{code_block}\n```\n\n"
        f"## 原始需求\n{original}"
    )


def _tail_lines(text: str, max_lines: int) -> str:
    """保留文本末尾最多 max_lines 行，前面用 ... 标记截断。"""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "... (truncated)\n" + "\n".join(lines[-max_lines:])


# ============================================================
# 新工作流 API：Detect → Confirm → Clean → Check → Match → Fill
# ============================================================

# 存储中间结果的全局变量（单用户场景，生产环境应改用 Redis/DB）
_workflow_state: dict = {}


def _resolve_workflow_state_key(customer_name: str, task_name: str) -> str:
    return f"{customer_name}/{task_name}"


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


def _project_root() -> str:
    """返回项目根目录（desktop/api.py 的上级目录）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_llm_yml_path() -> str:
    """llm.yml 的默认路径（config/config.llm.yml）。"""
    return os.path.join(_project_root(), "config", "config.llm.yml")


def _default_matching_yml_path() -> str:
    """matching.yml 的默认路径（config/config.matching.yml）。"""
    return os.path.join(_project_root(), "config", "config.matching.yml")


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

_migrate_config_files()


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


def _build_full_config(customer_name: str, task_name: str) -> dict:
    """构建完整的 pipeline 配置：llm.yml + task.yml。
    优先从 task_configs 表查询 yml 路径，找不到则用默认路径。

    LLM 开关由前端 Detect 步骤的 use_llm 决定，而非配置文件中的静态 enabled 字段。
    优先级：task.yml 中的 use_llm > _workflow_state 中的 llm_used > 配置文件默认值。
    """
    paths = _resolve_task_config_paths(customer_name, task_name)

    # 加载 llm.yml
    llm_cfg = {}
    if os.path.exists(paths["llm_yml_path"]):
        with open(paths["llm_yml_path"], "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}

    matching_yml_path = _default_matching_yml_path()
    if os.path.exists(matching_yml_path):
        with open(matching_yml_path, "r", encoding="utf-8") as f:
            matching_cfg = yaml_lib.safe_load(f) or {}
        llm_cfg.setdefault("llm", {})
        llm_cfg["llm"]["matching"] = matching_cfg.get("matching", {})

    # 加载 task.yml
    task_cfg = {}
    if os.path.exists(paths["task_yml_path"]):
        with open(paths["task_yml_path"], "r", encoding="utf-8") as f:
            task_cfg = yaml_lib.safe_load(f) or {}

    cfg = file_detector.merge_llm_into_full_config(task_cfg, llm_cfg)

    # ── use_llm 覆盖 matching.llm.enabled ──
    # 前端 Detect 步骤勾选了「使用 LLM」时，匹配步骤也应启用 LLM 辅助；
    # 反之则关闭。优先从 task.yml 读取（持久化），其次从内存状态读取。
    use_llm = task_cfg.get("use_llm")
    if use_llm is None:
        state_key = _resolve_workflow_state_key(customer_name, task_name)
        state = _workflow_state.get(state_key, {})
        use_llm = state.get("llm_used")

    if use_llm is not None:
        cfg.setdefault("matching", {}).setdefault("llm", {})
        cfg["matching"]["llm"]["enabled"] = bool(use_llm)

    return cfg


@app.get("/workflow/readmes")
async def get_workflow_readmes():
    """
    扫描 audit_workflow 下各任务子目录的 readme.md，返回内容列表。
    前端根据 dir_name 匹配 config.workflowTasks 的 dirName 显示任务名。
    """
    import pathlib

    audit_dir = pathlib.Path(__file__).resolve().parent.parent / "audit_workflow"
    readmes = []
    for p in sorted(audit_dir.glob("*")):
        if not p.is_dir() or p.name.startswith("_") or p.name.startswith("."):
            continue
        candidates = list(p.glob("readme.md")) + list(p.glob("README.md"))
        if not candidates:
            continue
        content = candidates[0].read_text(encoding="utf-8")
        readmes.append({
            "dir_name": p.name,
            "content": content,
        })
    return {"readmes": readmes}


@app.post("/workflow/detect")
async def workflow_detect(
    customer_name: str = Form(...),
    task_name: str = Form("bank_ledger_match"),
    use_llm: str = Form("true"),
):
    """
    Step 1 - Detect: 扫描 inputs/{customer}/{task}/ 下的文件，
                     用 LLM 自动识别文件类型、银行、时间段，生成 task.yml。
    use_llm: "true" → LLM 识别, "false" → 本地脚本/关键词识别。
    """
    logger.info("开始 Detect: customer=%s, task=%s, use_llm=%s", customer_name, task_name, use_llm)
    try:
        # 1. 扫描文件
        root_dir = _project_root()
        inputs_dir = os.path.join(root_dir, "inputs")
        files = file_detector.scan_input_files(inputs_dir, customer_name, task_name)

        if not files:
            return {
                "ok": False,
                "error": f"在 inputs/{customer_name}/{task_name}/ 下未找到任何文件。请先在数据源页面上传文件。",
                "files": [],
                "identifications": [],
            }

        # 2. 项目信息
        project_info = {
            "name": customer_name,
            "client_name": customer_name,
            "task": task_name,
            "audit_year": 2022,
        }

        # 3. 识别文件
        use_llm_flag = str(use_llm).lower() in ("1", "true", "yes", "on")
        llm_cfg = _load_llm_config()
        llm_enabled = llm_cfg.get("llm", {}).get("enabled", False) and use_llm_flag
        llm_error = None

        if llm_enabled:
            try:
                identifications, detect_usage = await file_detector.identify_files_with_llm_async(
                    files,
                    llm_cfg.get("llm", {}),
                    project_info,
                )
                token_tracker.record(detect_usage)
            except Exception as llm_exc:
                # LLM 调用失败：记录错误原因，回退到本地识别
                import traceback
                llm_error = {
                    "type": type(llm_exc).__name__,
                    "message": str(llm_exc),
                    "traceback": traceback.format_exc(),
                }
                identifications = file_detector._identify_files_local(files, project_info)
        else:
            identifications = file_detector._identify_files_local(files, project_info)

        # 4. 生成 task.yml 配置
        task_cfg = file_detector.generate_task_config(
            identifications, project_info
        )

        # 将前端 use_llm 选择持久化到 task.yml，供后续 Match 步骤读取
        task_cfg["use_llm"] = llm_enabled

        # 5. 保存到 outputs/{customer}/{task}/task.yml
        task_yml_path = os.path.join(
            root_dir, "outputs", customer_name, task_name, "task.yml"
        )
        saved_path = file_detector.save_task_config(task_cfg, task_yml_path)

        # 写入 DB 映射：公司 + 任务 → yml 路径
        _upsert_task_config(
            customer_name, task_name,
            task_yml_path=str(saved_path),
            llm_yml_path=_default_llm_yml_path(),
        )

        # 6. 保存状态
        key = _resolve_workflow_state_key(customer_name, task_name)
        _workflow_state[key] = {
            "files": files,
            "identifications": identifications,
            "task_config": task_cfg,
            "task_yml_path": str(saved_path),
            "llm_used": llm_enabled,
        }

        # 去掉 excel_preview 减少返回体积（那是给 LLM 看的 prompt 数据）
        files_light = [{k: v for k, v in f.items() if k != "excel_preview"} for f in files]

        logger.info("Detect 完成: customer=%s, task=%s, files=%d, llm_used=%s", customer_name, task_name, len(files), llm_enabled)
        return {
            "ok": True,
            "files_count": len(files),
            "files": files_light,
            "identifications": identifications,
            "task_config": task_cfg,
            "task_yml_path": str(saved_path),
            "llm_used": llm_enabled,
            "llm_error": llm_error,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/workflow/config")
def workflow_get_config(
    customer_name: str = "",
    task_name: str = "bank_ledger_match",
):
    """获取已生成的 task.yml 配置内容。"""
    paths = _resolve_task_config_paths(customer_name, task_name)
    task_yml_path = paths["task_yml_path"]
    if not os.path.exists(task_yml_path):
        return {"ok": False, "error": "尚未生成配置，请先执行 Detect 步骤。", "content": ""}

    with open(task_yml_path, "r", encoding="utf-8") as f:
        content = f.read()

    return {"ok": True, "content": content, "path": task_yml_path}


@app.post("/workflow/config/save")
def workflow_save_config(
    customer_name: str = Form(...),
    task_name: str = Form("bank_ledger_match"),
    content: str = Form(...),
):
    """
    Step 2 - Confirm: 前端用户确认/修改 task.yml 后保存。
    """
    logger.info("保存配置: customer=%s, task=%s", customer_name, task_name)
    try:
        paths = _resolve_task_config_paths(customer_name, task_name)
        task_yml_path = paths["task_yml_path"]
        os.makedirs(os.path.dirname(task_yml_path), exist_ok=True)

        # 验证 YAML 语法
        try:
            parsed = yaml_lib.safe_load(content)
        except yaml_lib.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"YAML 语法错误: {e}")

        with open(task_yml_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 同步 DB 映射
        _upsert_task_config(
            customer_name, task_name,
            task_yml_path=task_yml_path,
            llm_yml_path=paths["llm_yml_path"],
        )

        # 更新状态
        key = _resolve_workflow_state_key(customer_name, task_name)
        _workflow_state[key] = {
            **_workflow_state.get(key, {}),
            "task_config": parsed,
            "task_yml_path": task_yml_path,
            "confirmed": True,
        }

        return {
            "ok": True,
            "path": task_yml_path,
            "parsed": parsed,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/bank_ledger_match/clean")
def workflow_bank_ledger_match_clean(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """
    Step 3 - Clean: 用 task.yml 配置执行清洗。
    """
    logger.info("开始 Clean: customer=%s, task=%s", customer_name, task_name)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        bank_csv, ledger_csv = blm_pipeline.run_clean(cfg)
        return {"bank_csv": str(bank_csv), "ledger_csv": str(ledger_csv)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/bank_ledger_match/check")
def workflow_bank_ledger_match_check(
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """
    Step 4 - Check: 读取 monthly_flow_check.csv 返回数据完备性报告。
    检查银行流水和序时账按月/账号的流入流出是否一致。
    """
    logger.info("开始 Check: customer=%s, task=%s", customer_name, task_name)
    try:
        check_path = os.path.join(
            _project_root(), "outputs", customer_name, task_name,
            "matches", "monthly_flow_check.csv",
        )
        if not os.path.exists(check_path):
            return {
                "ok": False,
                "error": "monthly_flow_check.csv 不存在。请先执行 Match 步骤生成该文件，或直接运行 Clean → Match。",
                "summary": {"total_rows": 0, "ok_count": 0, "mismatch_count": 0},
                "rows": [],
            }

        with open(check_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        summary = {
            "total_rows": len(rows),
            "ok_count": sum(1 for r in rows if r.get("status", "") == "ok"),
            "mismatch_count": sum(1 for r in rows if r.get("status", "") == "mismatch"),
        }

        mismatches = [r for r in rows if r.get("status", "") == "mismatch"]

        return {
            "ok": True,
            "summary": summary,
            "all_ok": summary["mismatch_count"] == 0,
            "rows": rows[:200],  # 限制返回行数
            "mismatches": mismatches[:50],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/bank_ledger_match/fill")
def workflow_bank_ledger_match_fill(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    logger.info("开始 Fill: customer=%s, task=%s", customer_name, task_name)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        path = blm_pipeline.run_fill(cfg)
        logger.info("Fill 完成: %s", path)
        return {"working_paper": str(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/bank_ledger_match/fill_llm")
def workflow_bank_ledger_match_fill_llm(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
    """使用 LLM 生成填表代码并执行，自适应任意模板布局。"""
    logger.info("开始 Fill (LLM): customer=%s, task=%s", customer_name, task_name)
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        path, usage = blm_pipeline.run_fill_llm(cfg)
        token_tracker.record(usage)
        return {"working_paper": str(path), "usage": usage}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# Outbound Settlement Match 工作流 API
# ============================================================

def _build_osm_config(customer_name: str, task_name: str) -> dict:
    """Build configuration dict for outbound_settlement_match pipeline.

    Constructs project metadata and paths so the pipeline can locate
    inputs and outputs without requiring a task.yml file.  Also injects
    LLM configuration so the cleaning steps can use LLM-generated scripts.
    """
    root = _project_root()

    # Inject LLM config so pipeline can use LLM-generated cleaning scripts
    llm_cfg: dict = {}
    try:
        llm_cfg = _default_llm_config()
    except Exception as e:
        logger.warning("Failed to load LLM config for OSM: %s", e)

    return {
        "project": {
            "customer_name": customer_name,
            "task_name": task_name,
            "inputs_dir": os.path.join(root, "inputs", customer_name, task_name),
            "output_dir": os.path.join(root, "outputs", customer_name, task_name),
        },
        "matching": {
            "outbound_order_id_key": "order_id",
            "settlement_txn_id_key": "partner_txn_id",
        },
        "llm": llm_cfg,
        "_project_root": root,
        "_root": root,
    }


@app.post("/workflow/outbound_settlement_match/detect")
def workflow_osm_detect(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Step 1 - Detect: Scan inputs, classify settlement files and outbound sheets."""
    logger.info("OSM Detect: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        result = osm_pipeline.run_detect(cfg)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/outbound_settlement_match/clean_settlement")
def workflow_osm_clean_settlement(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Step 2 - Clean Settlement: Aggregate platform settlement CSVs."""
    logger.info("OSM Clean Settlement: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        settlement_path, summary_path = osm_pipeline.run_clean_settlement(cfg)
        token_tracker.record_from_config(cfg, "settlement")
        cleaning_info = cfg.get("_cleaning", {}).get("settlement", {})
        return {
            "ok": True,
            "settlement_csv": str(settlement_path),
            "monthly_summary": str(summary_path),
            "cleaning_mode": cleaning_info.get("mode", "hardcoded"),
            "fallback_reason": cleaning_info.get("fallback_reason"),
            "file_fallbacks": cleaning_info.get("file_fallbacks", []),
            "usage": cleaning_info.get("usage", {}),
            "script_dir": cleaning_info.get("script_dir"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/outbound_settlement_match/clean_outbound")
def workflow_osm_clean_outbound(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Step 3 - Clean Outbound: Parse Excel sheets into standardized CSVs."""
    logger.info("OSM Clean Outbound: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        paths = osm_pipeline.run_clean_outbound(cfg)
        token_tracker.record_from_config(cfg, "outbound")
        cleaning_info = cfg.get("_cleaning", {}).get("outbound", {})
        return {
            "ok": True,
            "paths": {k: str(v) if v else None for k, v in paths.items()},
            "cleaning_mode": cleaning_info.get("mode", "hardcoded"),
            "fallback_reason": cleaning_info.get("fallback_reason"),
            "file_fallbacks": cleaning_info.get("file_fallbacks", []),
            "sheet_tasks": cleaning_info.get("sheet_tasks", []),
            "usage": cleaning_info.get("usage", {}),
            "script_dir": cleaning_info.get("script_dir"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/outbound_settlement_match/clean_outbound_sheet")
def workflow_osm_clean_outbound_sheet(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
    file: str = Form(...),
    sheet: str = Form(...),
    sheet_type: str = Form(...),
    column_signature: str = Form(""),
    force_regenerate: bool = Form(False),
):
    """OSM: Retry cleaning a single outbound sheet.

    Re-generates (or reuses cached) LLM script for one sheet and runs it.
    Returns per-sheet status, script name, row count, and token usage.
    """
    logger.info(
        "OSM Clean Sheet: customer=%s, file=%s, sheet=%s, type=%s, force=%s",
        customer_name, file, sheet, sheet_type, force_regenerate,
    )
    try:
        import pandas as pd
        from audit_workflow.outbound_settlement_match.llm_cleaner import (
            ensure_llm_outbound_cleaner,
            safe_run_cleaner,
            compute_column_signature,
            patch_rename_dedup,
            _parser_dir,
        )
        from audit_workflow.outbound_settlement_match.outbound_cleaner import (
            _read_sheet_auto_header,
        )

        cfg = _build_osm_config(customer_name, task_name)
        inputs = Path(cfg["project"]["inputs_dir"])
        outbound_dir = inputs / "outbound"
        xlsx_path = outbound_dir / file

        if not xlsx_path.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在: {file}")

        # If force_regenerate, delete cached script
        if force_regenerate and column_signature:
            script_dir = _parser_dir(cfg)
            cached = script_dir / f"outbound_{sheet_type}_{column_signature}.py"
            if cached.exists():
                cached.unlink()
                logger.info("Deleted cached script: %s", cached.name)

        # Read sheet and compute column signature
        xl = pd.ExcelFile(str(xlsx_path), engine="openpyxl")
        df = _read_sheet_auto_header(xl, sheet)
        xl.close()

        if df.empty:
            return {"ok": True, "status": "failed", "error": "工作表为空", "rows": 0}

        df.columns = [str(c).strip() for c in df.columns]
        col_sig = column_signature or compute_column_signature(list(df.columns))

        # Generate / get cached script
        script_path, usage = ensure_llm_outbound_cleaner(
            cfg, xlsx_path, sheet, sheet_type, col_sig,
        )
        token_tracker.record(usage)

        # Run the script
        month = ""
        import re as _re
        m = _re.search(r"(\d{1,2})月", file)
        if m:
            month = f"2022-{int(m.group(1)):02d}"

        try:
            records = safe_run_cleaner(
                script_path, str(xlsx_path), sheet, file, month, sheet_type,
            )
        except Exception as run_err:
            err_str = str(run_err)
            # Auto-patch: if the error is "truth value of a Series is ambiguous",
            # it means the generated script has duplicate column names after rename.
            # Inject a dedup line right after the rename call and retry.
            if "truth value" in err_str.lower() or "series" in err_str.lower():
                logger.warning("Script %s has post-rename duplicate columns, auto-patching...", script_path.name)
                original_code = script_path.read_text(encoding="utf-8")
                patched_code = patch_rename_dedup(original_code)
                if patched_code != original_code:
                    script_path.write_text(patched_code, encoding="utf-8")
                    try:
                        records = safe_run_cleaner(
                            script_path, str(xlsx_path), sheet, file, month, sheet_type,
                        )
                        logger.info("Auto-patch succeeded for %s", script_path.name)
                    except Exception as patch_err:
                        logger.warning("Auto-patch also failed: %s", patch_err)
                        return {
                            "ok": True,
                            "status": "failed",
                            "script_name": script_path.name,
                            "rows": 0,
                            "error": f"原始错误: {err_str}\n修补后错误: {patch_err}",
                        }
                else:
                    return {
                        "ok": True,
                        "status": "failed",
                        "script_name": script_path.name,
                        "rows": 0,
                        "error": f"无法自动修补脚本: {err_str}",
                    }
            else:
                raise

        # Success — update persisted sheet_tasks.json and return refreshed totals
        import json as _json
        from audit_workflow.outbound_settlement_match.config import output_dir as _output_dir
        clean_dir = _output_dir(cfg) / "clean"
        st_path = clean_dir / "sheet_tasks.json"
        total_usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}

        # Update the matching entry in persisted sheet_tasks
        if st_path.exists():
            try:
                persisted = _json.loads(st_path.read_text(encoding="utf-8"))
                for task in persisted.get("sheet_tasks", []):
                    if task["file"] == file and task["sheet"] == sheet:
                        task["status"] = "llm_success"
                        task["script_name"] = script_path.name
                        task["column_signature"] = col_sig
                        task["rows"] = len(records)
                        task["error"] = None
                    # Accumulate usage from all tasks (approximate: cached=0 for old entries)
                # Add the new retry's usage to persisted totals
                old_usage = persisted.get("usage", {})
                for k in total_usage:
                    total_usage[k] = old_usage.get(k, 0) + usage.get(k, 0)
                persisted["usage"] = total_usage
                st_path.write_text(
                    _json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as persist_err:
                logger.warning("Failed to update sheet_tasks.json: %s", persist_err)
        else:
            # No persisted file — just accumulate this retry's usage
            total_usage = dict(usage)

        return {
            "ok": True,
            "status": "llm_success",
            "script_name": script_path.name,
            "column_signature": col_sig,
            "rows": len(records),
            "usage": usage,
            "total_usage": total_usage,
            "error": None,
        }
    except Exception as e:
        logger.warning("Clean sheet failed: %s", e)
        return {
            "ok": True,
            "status": "failed",
            "script_name": None,
            "rows": 0,
            "error": str(e),
        }


@app.post("/workflow/outbound_settlement_match/match")
def workflow_osm_match(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Step 4 - Match: Filter net outbound and match against settlement by ID."""
    logger.info("OSM Match: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        result = osm_pipeline.run_match(cfg)

        def _count_rows(p):
            if not p or not Path(p).exists():
                return 0
            with open(p, "r", encoding="utf-8", errors="ignore", newline="") as f:
                return max(sum(1 for _ in csv.reader(f)) - 1, 0)

        return {
            "ok": True,
            "summary": result["summary"],
            "net_outbound": {"path": str(result["net_outbound"]), "rows": _count_rows(result["net_outbound"])},
            "matched": {"path": str(result["matched"]), "rows": _count_rows(result["matched"])},
            "unmatched_outbound": {"path": str(result["unmatched_outbound"]), "rows": _count_rows(result["unmatched_outbound"])},
            "unmatched_settlement": {"path": str(result["unmatched_settlement"]), "rows": _count_rows(result["unmatched_settlement"])},
            "monthly_summary": {"path": str(result["monthly_summary"]), "rows": _count_rows(result["monthly_summary"])},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/outbound_settlement_match/run_all")
def workflow_osm_run_all(
    customer_name: str = Form(...),
    task_name: str = Form("outbound_settlement_match"),
):
    """OSM Full Pipeline: Detect → Clean → Match (one-shot)."""
    logger.info("OSM Run All: customer=%s, task=%s", customer_name, task_name)
    try:
        cfg = _build_osm_config(customer_name, task_name)
        result = osm_pipeline.run_all(cfg)
        token_tracker.record_from_config(cfg, "settlement", "outbound")
        cleaning = cfg.get("_cleaning", {})
        return {
            "ok": True,
            "summary": result.get("match", {}).get("summary", {}),
            "detect": {
                "settlement_files": len(result.get("detect", {}).get("settlement_files", [])),
                "outbound_files": len(result.get("detect", {}).get("outbound_files", [])),
            },
            "cleaning_settlement": cleaning.get("settlement", {}),
            "cleaning_outbound": cleaning.get("outbound", {}),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/llm/config")
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


@app.post("/llm/config")
def update_llm_config(
    default_provider: str = Form(""),
    enabled: str = Form(""),
):
    """更新 llm.yml 中的默认 provider 或启用状态。

    新格式（providers）下，通过 default_provider 指定默认 provider 名称。
    旧格式（扁平）下，不再支持通过此接口修改 model/base_url/api_key，
    请直接编辑 YAML 文件。
    """
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


@app.get("/llm/config/provider/{provider_name}/key")
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


@app.put("/llm/config/providers")
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


@app.post("/llm/config/providers")
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


@app.delete("/llm/config/providers/{provider_name}")
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


@app.get("/llm/tokens")
def get_llm_tokens():
    """获取当前会话累计 token 消耗。"""
    snap = token_tracker.snapshot()
    return {
        "ok": True,
        "total_tokens": snap["total_tokens"],
        "prompt_tokens": snap["prompt_tokens"],
        "completion_tokens": snap["completion_tokens"],
    }


@app.get("/events")
async def event_stream():
    """SSE 端点：前端通过 EventSource 连接此接口，实时接收后端事件。"""
    async def generator():
        while True:
            msg = await _event_queue.get()
            yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/logs")
def get_logs(lines: int = 100):
    """返回最近的日志行，供前端展示。"""
    if not os.path.exists(_log_file):
        return {"ok": True, "lines": [], "total": 0}
    with open(_log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    limit = min(max(lines, 1), 500)
    return {"ok": True, "lines": [l.rstrip("\n") for l in all_lines[-limit:]], "total": len(all_lines)}


# ============================================================
# Agent Loop API
# ============================================================

class AgentChatPayload(BaseModel):
    message: str
    conversation_id: str = ""
    customer_name: str = ""
    task_name: str = ""


@app.post("/agent/chat")
async def agent_chat(payload: AgentChatPayload):
    """向 Agent 发送消息，运行 pydantic-ai agent loop（带工具），
    流式推送中间事件到 SSE，返回最终响应。"""
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    conv_id = payload.conversation_id or conversation_store.create()

    # 将任务名解析为英文目录名（数据库驱动）
    task_dir = _resolve_task_dir_name(payload.task_name)

    # 若指定了客户名和任务名，确保输出目录存在
    if payload.customer_name and task_dir:
        _ensure_project_dirs(payload.customer_name, task_dir)

    try:
        result = await run_agent_chat(
            message=payload.message,
            conversation_id=conv_id,
            llm_config=_default_llm_config(),
            project_root=_project_root(),
            token_tracker=token_tracker,
            customer_name=payload.customer_name,
            task_name=task_dir,
        )
        return result
    except Exception as e:
        logger.error("Agent chat error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agent/conversations")
def agent_list_conversations():
    """列出所有 Agent 对话。"""
    return {"conversations": conversation_store.list_all()}


@app.get("/agent/conversations/{conversation_id}")
def agent_get_conversation(conversation_id: str):
    """获取对话的完整消息历史。"""
    conv = conversation_store.get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "role": m.role, "type": m.type, "content": m.content,
                "timestamp": m.timestamp, "metadata": m.metadata,
            }
            for m in conv["messages"]
        ],
    }


@app.post("/agent/conversations/new")
def agent_new_conversation():
    """创建新的 Agent 对话。"""
    conv_id = conversation_store.create()
    return {"conversation_id": conv_id}


@app.delete("/agent/conversations/{conversation_id}")
def agent_delete_conversation(conversation_id: str):
    """删除一个 Agent 对话。"""
    conversation_store.delete(conversation_id)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
