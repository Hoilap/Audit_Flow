"""desktop.routes_common — 项目/文件/Git/SSE/日志/工作流通用端点。"""

import json
import os
import shutil
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from .common import (
    logger,
    _event_queue,
    get_db,
    _ensure_project_dirs,
    _resolve_task_dir_name,
    _list_project_dirs,
    _load_task_definitions,
    _task_def_by_name,
    find_repo,
    _project_root,
    get_log_file_path,
    ProjectCreate,
    ProjectUpdate,
    WritePayload,
    project_row_to_dict,
)

router = APIRouter()


# ---------- Project CRUD ----------


@router.get("/projects/dirs")
def list_project_dirs():
    """返回 inputs/ 和 outputs/ 下按项目组织的目录列表"""
    return {"dirs": _list_project_dirs()}

@router.get("/task-definitions")
def list_task_definitions():
    """返回所有任务类型定义（从 config/config.task_definitions.yml 加载）"""
    return {"definitions": _load_task_definitions()}

# tasks 与 audit_procedures 关联查询（project_name 来自审计程序主表）
_PROJECT_SELECT = """
    SELECT t.*, ap.project_name AS project_name
    FROM tasks t
    LEFT JOIN audit_procedures ap ON ap.project_code = t.project_code
"""

_TASK_FIELDS = (
    "customer_name", "customer_short_name", "first_engagement", "start_date",
    "project_code", "group_audit", "end_date", "currency", "exchange_rate",
    "prepared_by", "prepared_date", "prepared_completed", "reviewed_by",
    "reviewed_date", "reviewed_completed",
)


@router.get("/dashboard/stats")
def dashboard_stats():
    """返回 Dashboard 统计概览：项目总数、已完成编制数、已完成审核数等。"""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    prepared = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE prepared_completed = 1"
    ).fetchone()["c"]
    reviewed = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE reviewed_completed = 1"
    ).fetchone()["c"]
    conn.close()
    return {
        "total_projects": total,
        "prepared_completed": prepared,
        "reviewed_completed": reviewed,
        "pending_prepare": max(total - prepared, 0),
        "pending_review": max(prepared - reviewed, 0),
    }


@router.get("/projects/procedures")
def list_audit_procedures():
    """返回审计程序主表（项目代码/项目名称），供任务表单选择"""
    conn = get_db()
    rows = conn.execute(
        "SELECT project_code, project_name FROM audit_procedures ORDER BY project_code"
    ).fetchall()
    conn.close()
    return {"procedures": [dict(r) for r in rows]}


@router.get("/projects/list")
def list_projects():
    conn = get_db()
    rows = conn.execute(_PROJECT_SELECT + " ORDER BY t.id DESC").fetchall()
    conn.close()
    return {"projects": [project_row_to_dict(r) for r in rows]}

@router.post("/projects/create")
def create_project(payload: ProjectCreate):
    logger.info("创建项目: customer=%s(%s), procedure=%s",
                payload.customer_name, payload.customer_short_name, payload.project_code)
    conn = get_db()
    proc = conn.execute(
        "SELECT project_code FROM audit_procedures WHERE project_code=?", (payload.project_code,)
    ).fetchone()
    if not proc:
        conn.close()
        raise HTTPException(status_code=400, detail=f"审计程序不存在: {payload.project_code}")
    cur = conn.execute(
        """INSERT INTO tasks (customer_name, customer_short_name, first_engagement, start_date,
           project_code, group_audit, end_date, currency, exchange_rate,
           prepared_by, prepared_date, prepared_completed, reviewed_by, reviewed_date, reviewed_completed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (payload.customer_name, payload.customer_short_name, payload.first_engagement,
         payload.start_date, payload.project_code, payload.group_audit, payload.end_date,
         payload.currency, payload.exchange_rate, payload.prepared_by, payload.prepared_date,
         int(payload.prepared_completed), payload.reviewed_by, payload.reviewed_date,
         int(payload.reviewed_completed))
    )
    conn.commit()
    row = conn.execute(_PROJECT_SELECT + " WHERE t.id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    # 自动创建项目目录结构：inputs/客户简称/项目代码 和 outputs/客户简称/项目代码
    _ensure_project_dirs(payload.customer_short_name, payload.project_code)
    return {"project": project_row_to_dict(row)}

@router.put("/projects/{project_id}")
def update_project(project_id: int, payload: ProjectUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (project_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="项目不存在")
    updates = {}
    for field in _TASK_FIELDS:
        val = getattr(payload, field)
        if val is not None:
            updates[field] = val
    if "project_code" in updates:
        proc = conn.execute(
            "SELECT project_code FROM audit_procedures WHERE project_code=?", (updates["project_code"],)
        ).fetchone()
        if not proc:
            conn.close()
            raise HTTPException(status_code=400, detail=f"审计程序不存在: {updates['project_code']}")
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", (*updates.values(), project_id))
        conn.commit()
    row = conn.execute(_PROJECT_SELECT + " WHERE t.id=?", (project_id,)).fetchone()
    conn.close()
    # 客户简称或项目代码变更时，确保新目录存在
    _ensure_project_dirs(row["customer_short_name"], row["project_code"])
    return {"project": project_row_to_dict(row)}

@router.delete("/projects/{project_id}")
def delete_project(project_id: int):
    logger.info("删除项目: id=%s", project_id)
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (project_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="项目不存在")
    conn.execute("DELETE FROM tasks WHERE id=?", (project_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- File operations ----------


@router.get("/files/list")
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


@router.get("/files/mtime")
def file_mtime(path: str):
    """Return file modification time as ISO string, or empty if not found."""
    p = os.path.abspath(path)
    if not os.path.exists(p):
        return {"mtime": "", "exists": False}
    mtime = os.path.getmtime(p)
    return {"mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"), "exists": True}


@router.get("/files/read")
def read_file(path: str):
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="File not found")
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return {"content": f.read()}


@router.post("/files/write")
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


@router.delete("/files/delete")
def delete_file(path: str):
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(p)
    return {"ok": True, "path": os.path.relpath(p)}


# ---------- Git operations ----------


@router.post("/git/commit")
def git_commit(message: str = "commit from desktop app"):
    repo = find_repo()
    repo.git.add(all=True)
    repo.index.commit(message)
    return {"ok": True}


@router.post("/git/revert")
def git_revert(path: str):
    repo = find_repo()
    try:
        repo.git.checkout("--", path)
        return {"ok": True}
    except Exception as e:
        logger.error("Git Revert 失败: path=%s, error=%s\n%s", path, e, traceback.format_exc())
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/git/log")
def git_log(limit: int = 20):
    repo = find_repo()
    commits = []
    try:
        commit_iter = list(repo.iter_commits(max_count=limit))
    except ValueError:
        # 数据目录的 git 仓库刚初始化、尚无提交记录（新装用户的正常状态）
        return {"commits": []}
    for c in commit_iter:
        commits.append({"hexsha": c.hexsha, "message": c.message, "author": str(c.author), "date": c.committed_datetime.isoformat()})
    return {"commits": commits}


# ---------- Upload / Copy ----------


@router.post("/files/upload")
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


@router.post("/files/copy-from-path")
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
        logger.error("复制文件失败: source=%s, dest=%s, error=%s\n%s", source, dest, e, traceback.format_exc())
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


@router.get("/files/validate-inputs")
def validate_inputs():
    """扫描 inputs/ 目录结构，校验是否符合「客户简称/项目代码」规范并与数据库任务一致。

    返回 issues 列表，每项含 level(error/warning)、path、message。
    """
    issues = []
    root = _project_root()
    inputs_dir = os.path.join(root, "inputs")
    if not os.path.isdir(inputs_dir):
        return {"ok": True, "issues": []}

    conn = get_db()
    rows = conn.execute("SELECT customer_short_name, project_code FROM tasks").fetchall()
    conn.close()

    # 客户简称 -> 该客户在数据库中的项目代码（即任务英文目录名）集合
    expected: dict[str, set] = {}
    for r in rows:
        expected.setdefault(r["customer_short_name"], set()).add(_resolve_task_dir_name(r["project_code"]))

    for entry in sorted(os.listdir(inputs_dir)):
        if entry.startswith("."):
            continue
        entry_path = os.path.join(inputs_dir, entry)
        rel = f"inputs/{entry}"
        if os.path.isfile(entry_path):
            issues.append({
                "level": "error", "path": rel,
                "message": "文件直接放在 inputs/ 根目录，应放入 inputs/{客户名称}/{任务英文目录名}/ 下",
            })
            continue
        if entry not in expected:
            known = "、".join(sorted(expected)) or "（无，请先在项目管理中创建项目）"
            issues.append({
                "level": "error", "path": rel,
                "message": f"客户目录「{entry}」与数据库中任何项目的客户名称不一致（数据库客户：{known}）",
            })
            continue
        # 二级：任务英文目录名
        for sub in sorted(os.listdir(entry_path)):
            if sub.startswith("."):
                continue
            sub_path = os.path.join(entry_path, sub)
            sub_rel = f"{rel}/{sub}"
            if os.path.isfile(sub_path):
                issues.append({
                    "level": "error", "path": sub_rel,
                    "message": f"文件应放入任务目录下：inputs/{entry}/{{任务英文目录名}}/{sub}",
                })
                continue
            if sub not in expected[entry]:
                td = _task_def_by_name(sub)
                hint = f"。是否想命名为「{td['dir_name']}」？" if td and td.get("dir_name") != sub else ""
                known_tasks = "、".join(sorted(expected[entry]))
                issues.append({
                    "level": "error", "path": sub_rel,
                    "message": f"任务目录「{sub}」与客户「{entry}」在数据库中的项目代码不一致（应为：{known_tasks}）{hint}",
                })

    has_error = any(i["level"] == "error" for i in issues)
    logger.info("校验 inputs/ 目录: %d 个问题", len(issues))
    return {"ok": not has_error, "issues": issues}


@router.post("/files/open-in-explorer")
def open_in_explorer(path: str = Form(...)):
    """在系统文件资源管理器中打开数据根目录内的指定路径（如 inputs/ 或 outputs/）。"""
    root = os.path.abspath(_project_root())
    target = os.path.abspath(os.path.join(root, path))
    # 只允许打开数据根目录内的路径，防止越界
    if os.path.commonpath([root, target]) != root:
        raise HTTPException(status_code=400, detail="只允许打开数据目录内的路径")
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")
    if hasattr(os, "startfile"):  # Windows
        os.startfile(target)
    else:
        import subprocess
        subprocess.Popen(["xdg-open", target])
    logger.info("打开文件资源管理器: %s", target)
    return {"ok": True, "path": target}


# ---------- Workflow readmes ----------


@router.get("/workflow/readmes")
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


# ---------- SSE / Logs ----------


@router.get("/events")
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


@router.get("/logs")
def get_logs(lines: int = 100):
    """返回最近的日志行，供前端展示。"""
    log_file = get_log_file_path()
    if not os.path.exists(log_file):
        return {"ok": True, "lines": [], "total": 0}
    with open(log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    limit = min(max(lines, 1), 500)
    return {"ok": True, "lines": [l.rstrip("\n") for l in all_lines[-limit:]], "total": len(all_lines)}
