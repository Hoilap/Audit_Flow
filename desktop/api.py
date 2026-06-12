import os
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from git import Repo, InvalidGitRepositoryError
from typing import List
import json
import subprocess
import csv
from fastapi import BackgroundTasks
from fastapi import Form
from fastapi.responses import JSONResponse
from fastapi import Depends
import importlib
from audit_workflow.bank_ledger_match import pipeline as blm_pipeline
from dotenv import load_dotenv
import openai

load_dotenv()
OPENAI_KEY = os.getenv('OPENAI_API_KEY')
if OPENAI_KEY:
    openai.api_key = OPENAI_KEY

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.post("/workflow/clean")
def workflow_clean(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        bank_csv, ledger_csv = blm_pipeline.run_clean(cfg)
        return {"bank_csv": str(bank_csv), "ledger_csv": str(ledger_csv)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/match")
def workflow_match(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        matches, unmatched_bank, unmatched_ledger = blm_pipeline.run_match(cfg)
        return {"matches": str(matches), "unmatched_bank": str(unmatched_bank), "unmatched_ledger": str(unmatched_ledger)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/approve")
def workflow_approve(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        a, b, c, d = blm_pipeline.run_approve(cfg)
        return {"result": [str(x) for x in (a, b, c, d)]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/verify")
def workflow_verify(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        out = blm_pipeline.output_dir(cfg)
        files = {
            "matches": out / "matches" / "matches.csv",
            "unmatched_bank": out / "matches" / "unmatched_bank.csv",
            "unmatched_ledger": out / "matches" / "unmatched_ledger.csv",
        }
        result = {}
        for name, path in files.items():
            if not path.exists():
                result[name] = {"exists": False, "rows": 0, "path": str(path)}
                continue
            with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
                rows = max(sum(1 for _ in csv.reader(f)) - 1, 0)
            result[name] = {"exists": True, "rows": rows, "path": str(path)}
        result["ok"] = all(item["exists"] for item in result.values() if isinstance(item, dict))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/fill")
def workflow_fill(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        path = blm_pipeline.run_fill(cfg)
        return {"working_paper": str(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workflow/full")
def workflow_full(config: str = Form(None)):
    cfg = {}
    if config:
        cfg = json.loads(config)
    try:
        res = blm_pipeline.run_all(cfg)
        return {k: str(v) for k, v in res.items()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/llm/generate")
def llm_generate(prompt: str = Form(...), target_path: str = Form("outputs/clean/generated_from_llm.txt")):
    # If OPENAI_API_KEY not set, return a placeholder
    if not OPENAI_KEY:
        # echo prompt back
        content = f"[LLM not configured] Prompt received:\n{prompt}"
    else:
        try:
            resp = openai.ChatCompletion.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], max_tokens=800)
            content = resp.choices[0].message.content
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
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

    # generate content
    if not OPENAI_KEY:
        # if user requested execution of python code, assume prompt is the code
        if run_flag and target_path.endswith('.py'):
            content = prompt
        else:
            content = f"[LLM not configured] Prompt received:\n{prompt}"
    else:
        try:
            resp = openai.ChatCompletion.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], max_tokens=1500)
            content = resp.choices[0].message.content
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    # no debug prints in production

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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
