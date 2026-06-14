import os
import sqlite3
from datetime import datetime
from pathlib import Path
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
from fastapi.responses import JSONResponse
from fastapi import Depends
import importlib
import yaml as yaml_lib
from audit_workflow.bank_ledger_match import pipeline as blm_pipeline
from audit_workflow.bank_ledger_match import file_detector
from audit_workflow.llm_agent import llm_generate as audit_llm_generate
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

init_db()


def _ensure_project_dirs(customer_name: str, task_name: str):
    """确保 inputs/客户名称/任务名称 和 outputs/客户名称/任务名称 目录存在"""
    for base in ('inputs', 'outputs'):
        d = os.path.join(base, customer_name, task_name)
        os.makedirs(d, exist_ok=True)


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
    }

# ---------- Project CRUD endpoints ----------

@app.get("/projects/dirs")
def list_project_dirs():
    """返回 inputs/ 和 outputs/ 下按项目组织的目录列表"""
    return {"dirs": _list_project_dirs()}

@app.get("/projects/list")
def list_projects():
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
    conn.close()
    return {"projects": [project_row_to_dict(r) for r in rows]}

@app.post("/projects/create")
def create_project(payload: ProjectCreate):
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
    result = []
    for dirpath, dirs, files in os.walk(base):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), start=os.getcwd())
            result.append(rel.replace('\\\\', '/'))
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
    repo = find_repo(full_path)
    try:
        repo.index.add([os.path.relpath(full_path, repo.working_tree_dir)])
        repo.index.commit(f"upload {file.filename}")
    except Exception:
        pass
    return {"ok": True, "path": os.path.relpath(full_path)}


@app.post("/workflow/bank_ledger_match/match")
def workflow_match(
    config: str = Form(None),
    customer_name: str = Form(""),
    task_name: str = Form("bank_ledger_match"),
):
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
    """从环境变量构建默认 LLM 配置，供 audit_workflow.llm_agent 使用。"""
    return {
        "api_key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "base_url_env": "OPENAI_BASE_URL",
    }


@app.post("/llm/generate")
def llm_generate(prompt: str = Form(...), target_path: str = Form("outputs/clean/generated_from_llm.txt")):
    try:
        content = audit_llm_generate(_default_llm_config(), prompt)
    except RuntimeError as e:
        # LLM 未配置，回退为占位文本
        content = f"[LLM not configured] {e}\nPrompt received:\n{prompt}"

    # write content
    p = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    repo = find_repo(p)
    try:
        repo.index.add([os.path.relpath(p, repo.working_tree_dir)])
        repo.index.commit(f"llm write to {target_path}")
    except Exception:
        pass
    return {"path": os.path.relpath(p), "ok": True}


@app.post("/llm/generate_and_run")
def llm_generate_and_run(
    prompt: str = Form(...),
    target_path: str = Form("outputs/clean/generated_from_llm.py"),
    run_code: str = Form('false'),
    timeout: int = Form(5),
):
    import ast
    import sys
    # normalize run_code flag (accept 'true'/'false' from forms)
    run_flag = str(run_code).lower() in ("1", "true", "yes", "on")

    # generate content via shared LLM agent
    try:
        content = audit_llm_generate(_default_llm_config(), prompt)
    except RuntimeError as e:
        if run_flag and target_path.endswith('.py'):
            content = prompt
        else:
            content = f"[LLM not configured] {e}\nPrompt received:\n{prompt}"

    p = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # if python file and requested to run, do syntax check
    run_result = None
    if target_path.endswith('.py') and run_flag:
        try:
            ast.parse(content)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Syntax error in generated code: {e}")
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    repo = find_repo(p)
    try:
        repo.index.add([os.path.relpath(p, repo.working_tree_dir)])
        repo.index.commit(f"llm write to {target_path}")
    except Exception:
        pass

    if run_flag and target_path.endswith('.py'):
        try:
            proc = subprocess.run([sys.executable, p], capture_output=True, text=True, timeout=timeout)
            run_result = {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        except subprocess.TimeoutExpired:
            run_result = {"error": "timeout"}

    return {"path": os.path.relpath(p), "ok": True, "run_result": run_result}


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
            return yaml_lib.safe_load(f) or {}
    return {"llm": {"enabled": False}}


def _project_root() -> str:
    """返回项目根目录（desktop/api.py 的上级目录）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_llm_yml_path() -> str:
    """llm.yml 的默认路径（项目根目录下的 config.example.llm.yml）。"""
    return os.path.join(_project_root(), "config.example.llm.yml")


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
        return {
            "task_yml_path": row["task_yml_path"],
            "llm_yml_path": row["llm_yml_path"] or default_llm,
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
    """
    paths = _resolve_task_config_paths(customer_name, task_name)

    # 加载 llm.yml
    llm_cfg = {}
    if os.path.exists(paths["llm_yml_path"]):
        with open(paths["llm_yml_path"], "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}

    # 加载 task.yml
    task_cfg = {}
    if os.path.exists(paths["task_yml_path"]):
        with open(paths["task_yml_path"], "r", encoding="utf-8") as f:
            task_cfg = yaml_lib.safe_load(f) or {}

    return file_detector.merge_llm_into_full_config(task_cfg, llm_cfg)


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
                identifications = await file_detector.identify_files_with_llm_async(
                    files,
                    llm_cfg.get("llm", {}),
                    project_info,
                )
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
    if customer_name:
        cfg = _build_full_config(customer_name, task_name)
    elif config:
        cfg = json.loads(config)
    else:
        cfg = {}
    try:
        path = blm_pipeline.run_fill(cfg)
        return {"working_paper": str(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
