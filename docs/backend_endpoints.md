# 后端 API 调用说明

本文档列出本项目内置本地后端（位于 `desktop/api.py`）的所有 HTTP 端点、参数与示例调用方法，方便前端或第三方工具集成。

默认后端：`http://127.0.0.1:8000`（通过 `python -m desktop.api` 或由 Electron 启动时自动运行）。

注意：后端允许跨域（CORS），多数接口接受表单（form）或 JSON 字段；某些接口会将文件写入仓库并尝试用 git 提交。

---

## 启动后端（开发）

```powershell
# 在项目根目录
cd desktop
# 启动后端
python -m desktop.api
```

Electron 前端会在 `desktop/package.json` 中运行 `npm run start`，它会自动启动后端并加载前端界面。

---

## 通用说明
- 基本 URL: `http://127.0.0.1:8000`
- 大部分 `POST` 接口接受 `application/x-www-form-urlencoded` 或 `multipart/form-data`（上传文件时）。
- 若需使用 LLM 相关接口，请先在环境或 `.env` 中设置 `OPENAI_API_KEY`。

---

## 接口一览

1. GET /files/list
   - 描述：列出指定目录下的所有文件（相对于仓库根或当前工作目录）。
   - 参数：`root`（query，可选，默认 `outputs`）
   - 示例：
     ```bash
     curl "http://127.0.0.1:8000/files/list?root=outputs"
     ```
   - 返回：{"files": ["outputs/clean/bank_transactions.csv", ...]}

2. GET /files/read
   - 描述：读取文件内容（文本文件）。
   - 参数：`path`（query，必填，文件路径）
   - 示例：
     ```bash
     curl "http://127.0.0.1:8000/files/read?path=outputs/clean/bank_transactions.csv"
     ```
   - 返回：{"content": "...file content..."}

3. POST /files/write
   - 描述：写入文本文件并尝试用 git 提交。
   - 请求体（JSON）：
     - `path` (string) — 写入的相对或绝对路径
     - `content` (string) — 文件内容
     - `commit_message` (string, 可选) — git 提交信息，默认 `update from desktop app`
   - 示例：
     ```bash
     curl -X POST "http://127.0.0.1:8000/files/write" -H "Content-Type: application/json" -d '{"path":"outputs/notes.txt","content":"hello","commit_message":"add note"}'
     ```
   - 返回：{"ok": true, "path": "outputs/notes.txt"}

4. POST /files/upload
   - 描述：上传文件到后端指定目录并写入仓库（multipart 表单）。
   - 表单字段：`file` (file), `dest` (string，可选，默认 `outputs/`)；若 `dest` 是目录则以上传文件名保存，否则 `dest` 可指定完整路径。
   - 示例：
     ```bash
     curl -F "file=@./inputs/bank.xlsm" -F "dest=outputs/bank/" http://127.0.0.1:8000/files/upload
     ```
   - 返回：{"ok": true, "path": "outputs/bank/bank.xlsm"}

5. POST /git/commit
   - 描述：对仓库当前更改执行 `git add --all` + `git commit`。
   - 参数（表单或 query）：`message`（string，可选，默认 `commit from desktop app`）
   - 示例：
     ```bash
     curl -X POST -d "message=save changes" http://127.0.0.1:8000/git/commit
     ```
   - 返回：{"ok": true}

6. POST /git/revert
   - 描述：撤销指定路径的本地修改（git checkout -- path）。
   - 参数（form/query）：`path`（string，必填）
   - 示例：
     ```bash
     curl -X POST -d "path=outputs/notes.txt" http://127.0.0.1:8000/git/revert
     ```
   - 返回：{"ok": true} 或错误信息。

7. GET /git/log
   - 描述：返回最近的 git 提交记录。
   - 参数：`limit`（query，可选，默认 20）
   - 示例：
     ```bash
     curl "http://127.0.0.1:8000/git/log?limit=10"
     ```
   - 返回：{"commits": [{"hexsha":..., "message":..., "author":..., "date":...}, ...]}

8. POST /workflow/clean
   - 描述：运行清洗流程（生成 `bank_transactions.csv` 和 `ledger_entries.csv`）。
   - 参数（form，可选）：`config`（string，JSON 序列化的配置对象；若为空则使用默认路径/配置）
   - 示例（直接传 JSON 字符串）：
     ```bash
     curl -X POST -F 'config={"output_dir":"outputs"}' http://127.0.0.1:8000/workflow/clean
     ```
   - 返回：{"bank_csv": "...", "ledger_csv": "..."}


9. POST /workflow/bank_ledger_match/match
   - 描述：运行匹配流程（bank_ledger_match），生成 `matches.csv` 与未匹配清单。
   - 参数（form，可选）：`config`（JSON 字符串）
   - 示例：
     ```bash
     curl -X POST -F 'config={"output_dir":"outputs"}' http://127.0.0.1:8000/workflow/bank_ledger_match/match
     ```
   - 返回：{"matches": "...", "unmatched_bank": "...", "unmatched_ledger": "..."}


10. POST /workflow/bank_ledger_match/approve
    - 描述：运行批准/审批相关流程（bank_ledger_match 的 `run_approve` 输出）。
    - 参数（form，可选）：`config`（JSON 字符串）
    - 示例：
      ```bash
      curl -X POST -F 'config={}' http://127.0.0.1:8000/workflow/bank_ledger_match/approve
      ```
    - 返回：{"result": ["...", ...]}


11. POST /workflow/bank_ledger_match/verify
    - 描述：检查 `bank_ledger_match` 输出目录中关键 CSV 是否存在并返回行数统计。
    - 参数（form，可选）：`config`（JSON 字符串）
    - 示例：
      ```bash
      curl -X POST -F 'config={"output_dir":"outputs"}' http://127.0.0.1:8000/workflow/bank_ledger_match/verify
      ```
    - 返回示例：
      {
        "matches": {"exists": true, "rows": 123, "path": "..."},
        "unmatched_bank": {...},
        "unmatched_ledger": {...},
        "ok": true
      }


12. POST /workflow/bank_ledger_match/fill
    - 描述：把 `bank_ledger_match` 的匹配结果填入工作底稿（生成或更新 xlsm）。
    - 参数（form，可选）：`config`（JSON 字符串）
    - 示例：
      ```bash
      curl -X POST -F 'config={}' http://127.0.0.1:8000/workflow/bank_ledger_match/fill
      ```
    - 返回：{"working_paper": "path/to/工作底稿.xlsm"}

13. POST /workflow/full
    - 描述：一次性跑完整流水：clean + match + fill 等（`run_all`）。
    - 参数（form，可选）：`config`（JSON 字符串）
    - 示例：
      ```bash
      curl -X POST -F 'config={}' http://127.0.0.1:8000/workflow/full
      ```
    - 返回：按键值返回各阶段产物路径的 JSON 对象。

14. POST /llm/generate
    - 描述：发送 prompt 到配置的 OpenAI 兼容接口，结果写入指定路径（文本）。若未配置 API key，会把 prompt 回显为占位内容。
    - 表单字段：`prompt`（string，必填），`target_path`（string，可选，默认 `outputs/clean/generated_from_llm.txt`）
    - 示例：
      ```bash
      curl -X POST -F 'prompt=请把下面文本转换为 CSV...' -F 'target_path=outputs/clean/generated.txt' http://127.0.0.1:8000/llm/generate
      ```
    - 返回：{"path": "...", "ok": true}

15. POST /llm/generate_and_run
    - 描述：生成 LLM 内容并写入文件；当 `target_path` 以 `.py` 结尾且 `run_code` 为真时，会先做语法检查并尝试在独立进程中执行（有 `timeout` 秒限制）。
    - 表单字段：
      - `prompt`（string，必填）
      - `target_path`（string，可选，默认 `outputs/clean/generated_from_llm.py`）
      - `run_code`（string/flag，可选，'true'/'false'/'1' 等皆可，被解析为布尔值，默认 'false'）
      - `timeout`（int，可选，执行超时秒数，默认 5）
    - 示例（仅生成）：
      ```bash
      curl -X POST -F 'prompt=生成一个简单的 Python 脚本' -F 'target_path=outputs/clean/foo.py' http://127.0.0.1:8000/llm/generate_and_run
      ```
    - 示例（生成并执行）：
      ```bash
      curl -X POST -F 'prompt=print("hello")' -F 'target_path=outputs/clean/runme.py' -F 'run_code=true' -F 'timeout=3' http://127.0.0.1:8000/llm/generate_and_run
      ```
    - 返回：{"path":"...","ok":true,"run_result": {"returncode":0,"stdout":"...","stderr":"..."}} 或 超时/错误信息。

---

## 使用建议
- 调用写/上传接口会尝试把文件加入 git 并提交；若不需要该行为，请在本地调用前备份或忽略 `.git`。
- 对于 `config` 字段，推荐先在本地准备好 `config.example.yml` 或 JSON，然后把其内容作为字符串发送。
- 当调用需要较长时间的后台任务（例如复杂匹配），建议在前端显示等待或使用后台任务并轮询 `workflow/verify` 以确认产物就绪。

---

如果你希望我把这份文档也添加到仓库根 README 中并放置链接，我可以继续把 `docs/backend_endpoints.md` 的链接加到 [README.md](README.md)。
