# 后端 API 调用说明

本文档列出本项目内置本地后端（位于 `desktop/api.py`）的所有 HTTP 端点、参数与示例调用方法，方便前端或第三方工具集成。

默认后端：`http://127.0.0.1:8000`（通过 `python -m desktop.api` 或由 Electron 启动时自动运行）。

---

## 启动后端

```powershell
cd desktop
python -m desktop.api
# 或带热重载
python -m uvicorn desktop.api:app --host 127.0.0.1 --port 8000 --reload
```

## 通用说明
- 基本 URL: `http://127.0.0.1:8000`
- `POST` 接口接受 `application/x-www-form-urlencoded` 或 `multipart/form-data`（上传文件时）。
- 部分接口接受 `application/json`（详见各接口说明）。
- 写/上传接口会尝试把文件加入 git 并提交。

---

## 配置架构

配置已拆分为两层，通过 `task_configs` 数据库表关联：

```
config.example.llm.yml          ← LLM 模型/API 配置（全局）
outputs/{客户}/{任务}/task.yml  ← 任务文件相关配置（由 Detect 生成）
```

运行时由 `_build_full_config(customer_name, task_name)` 合并两者。

---

## 接口一览

### 项目管理

#### 1. GET /projects/dirs
扫描 `inputs/` 和 `outputs/` 下实际存在的项目目录。
- 返回: `{"dirs": [{"customer_name": "...", "task_name": "..."}, ...]}`

#### 2. GET /projects/list
列出所有项目（从 projects.db）。
- 返回: `{"projects": [{id, task_name, customer_name, status, created_at, responsible_person, risk}, ...]}`

#### 3. POST /projects/create
创建新项目，同时自动创建 `inputs/` 和 `outputs/` 子目录。
- Body (JSON):
  - `task_name` (string, 必填)
  - `customer_name` (string)
  - `status` (string, 默认 "Planning")
  - `responsible_person` (string)
  - `risk` (string, 默认 "Medium")
- 返回: `{"project": {...}}`

#### 4. PUT /projects/{project_id}
更新项目信息。
- Body (JSON): 任意可更新字段 (`task_name, customer_name, status, created_at, responsible_person, risk`)
- 返回: `{"project": {...}}`

#### 5. DELETE /projects/{project_id}
删除项目。
- 返回: `{"ok": true}`

---

### 文件管理

#### 6. GET /files/list
列出指定目录下的所有文件。
- 参数: `root` (query, 可选, 默认 `outputs`)
- 示例:
  ```bash
  curl "http://127.0.0.1:8000/files/list?root=outputs"
  ```
- 返回: `{"files": ["outputs/clean/bank_transactions.csv", ...]}`

#### 7. GET /files/read
读取文本文件内容。
- 参数: `path` (query, 必填)
- 示例:
  ```bash
  curl "http://127.0.0.1:8000/files/read?path=outputs/clean/bank_transactions.csv"
  ```
- 返回: `{"content": "..."}`

#### 8. POST /files/write
写入文本文件并 git 提交。
- Body (JSON):
  - `path` (string, 必填)
  - `content` (string, 必填)
  - `commit_message` (string, 可选)
- 示例:
  ```bash
  curl -X POST "http://127.0.0.1:8000/files/write" \
    -H "Content-Type: application/json" \
    -d '{"path":"outputs/notes.txt","content":"hello"}'
  ```
- 返回: `{"ok": true, "path": "outputs/notes.txt"}`

#### 9. DELETE /files/delete
删除指定文件。
- 参数: `path` (query, 必填)
- 返回: `{"ok": true, "path": "..."}`

#### 10. POST /files/upload
上传文件（multipart 表单）。
- 表单字段:
  - `file` (file, 必填)
  - `dest` (string, 可选, 默认 `outputs/`)
- 示例:
  ```bash
  curl -F "file=@./bank.xlsm" -F "dest=inputs/桂平金山/bank_ledger_match/" \
    http://127.0.0.1:8000/files/upload
  ```
- 返回: `{"ok": true, "path": "inputs/桂平金山/bank_ledger_match/bank.xlsm"}`

---

### Git 版本管理

#### 11. POST /git/commit
`git add --all` + `git commit`。
- 参数: `message` (string, 可选)
- 返回: `{"ok": true}`

#### 12. POST /git/revert
撤销指定路径的本地修改 (`git checkout -- path`)。
- 参数: `path` (string, 必填)
- 返回: `{"ok": true}`

#### 13. GET /git/log
返回最近 git 提交记录。
- 参数: `limit` (query, 可选, 默认 20)
- 返回: `{"commits": [{"hexsha":..., "message":..., "author":..., "date":...}, ...]}`

---

### 新工作流 API（6 步流水线）

工作流: **Detect → Confirm → Clean → Check → Match → Fill**

#### 14. POST /workflow/detect（Step 1）
扫描 `inputs/{customer}/{task}/` 下的 Excel 文件，用 LLM 自动识别文件类型（银行流水/序时账）、银行名称、时间段，生成 `task.yml` 并写入 `task_configs` 表。
- 表单字段:
  - `customer_name` (string, 必填) — 客户名称
  - `task_name` (string, 可选, 默认 `bank_ledger_match`)
  - `use_llm` (string, 可选, 默认 `true`) — 是否启用 LLM 识别
- 示例:
  ```bash
  curl -X POST -F "customer_name=桂平金山" -F "task_name=bank_ledger_match" \
    http://127.0.0.1:8000/workflow/detect
  ```
- 返回:
  ```json
  {
    "ok": true,
    "files_count": 2,
    "files": [{"path": "...", "name": "...", ...}],
    "identifications": [{"id": "icbc_bank_2022", "type": "bank_statement", "bank_name": "...", ...}],
    "task_config": {...},
    "task_yml_path": "outputs/桂平金山/bank_ledger_match/task.yml",
    "llm_used": true
  }
  ```

#### 15. GET /workflow/config
获取已生成的 `task.yml` 内容（从 `task_configs` 表查路径）。
- 参数:
  - `customer_name` (query, 必填)
  - `task_name` (query, 可选, 默认 `bank_ledger_match`)
- 示例:
  ```bash
  curl "http://127.0.0.1:8000/workflow/config?customer_name=桂平金山&task_name=bank_ledger_match"
  ```
- 返回: `{"ok": true, "content": "...YAML...", "path": "..."}`

#### 16. POST /workflow/config/save（Step 2）
保存用户确认/修改后的 `task.yml`，同步更新 `task_configs` 表。
- 表单字段:
  - `customer_name` (string, 必填)
  - `task_name` (string, 可选, 默认 `bank_ledger_match`)
  - `content` (string, 必填) — YAML 文本内容
- 示例:
  ```bash
  curl -X POST -F "customer_name=桂平金山" -F "task_name=bank_ledger_match" \
    -F "content=$(cat task.yml)" http://127.0.0.1:8000/workflow/config/save
  ```
- 返回: `{"ok": true, "path": "...", "parsed": {...}}`
- 错误: 400 (YAML 语法错误)

#### 17. POST /workflow/bank_ledger_match/clean（Step 3）
按 task.yml 配置清洗原始数据，生成 `clean/bank_transactions.csv` 和 `clean/ledger_entries.csv`。
- 表单字段:
  - `customer_name` (string) — 推荐，自动从 DB 加载配置
  - `task_name` (string, 可选, 默认 `bank_ledger_match`)
  - `config` (string, 可选) — 直接传 JSON 配置（兼容旧版）
- 示例:
  ```bash
  curl -X POST -F "customer_name=桂平金山" \
    http://127.0.0.1:8000/workflow/bank_ledger_match/clean
  ```
- 返回: `{"bank_csv": "...", "ledger_csv": "..."}`

#### 18. POST /workflow/bank_ledger_match/check（Step 4）
读取 `monthly_flow_check.csv`，返回数据完备性报告（按月/账号检查流入流出是否一致）。
- 表单字段:
  - `customer_name` (string, 必填)
  - `task_name` (string, 可选, 默认 `bank_ledger_match`)
- 示例:
  ```bash
  curl -X POST -F "customer_name=桂平金山" \
    http://127.0.0.1:8000/workflow/bank_ledger_match/check
  ```
- 返回:
  ```json
  {
    "ok": true,
    "all_ok": false,
    "summary": {"total_rows": 12, "ok_count": 10, "mismatch_count": 2},
    "rows": [...],
    "mismatches": [...]
  }
  ```
- 注意: `monthly_flow_check.csv` 由 Match 步骤生成，Check 需在 Match 之后执行。

#### 19. POST /workflow/bank_ledger_match/match（Step 5）
执行银行流水 ↔ 序时账自动匹配，生成 `matches/matches.csv`、`unmatched_bank.csv`、`unmatched_ledger.csv`、`monthly_flow_check.csv`。
- 表单字段:
  - `customer_name` (string) — 推荐
  - `task_name` (string, 可选)
  - `config` (string, 可选) — 兼容旧版
- 示例:
  ```bash
  curl -X POST -F "customer_name=桂平金山" \
    http://127.0.0.1:8000/workflow/bank_ledger_match/match
  ```
- 返回: `{"matches": "...", "unmatched_bank": "...", "unmatched_ledger": "..."}`

#### 20. POST /workflow/bank_ledger_match/fill（Step 6）
将匹配结果填入 Excel 工作底稿模板。
- 表单字段:
  - `customer_name` (string) — 推荐
  - `task_name` (string, 可选)
  - `config` (string, 可选) — 兼容旧版
- 示例:
  ```bash
  curl -X POST -F "customer_name=桂平金山" \
    http://127.0.0.1:8000/workflow/bank_ledger_match/fill
  ```
- 返回: `{"working_paper": "outputs/.../working_paper/资金流水专项核查工作底稿-自动填报.xlsm"}`

---

### 其他工作流辅助

#### 21. POST /workflow/bank_ledger_match/approve
运行批准/复核流程。
- 表单字段: `config` (string, 可选, JSON)
- 返回: `{"result": ["...", ...]}`

#### 22. POST /workflow/bank_ledger_match/verify
检查关键产物 CSV 是否存在并返回行数统计。
- 表单字段:
  - `customer_name` (string, 可选)
  - `task_name` (string, 可选)
  - `config` (string, 可选, JSON)
- 返回:
  ```json
  {
    "matches": {"exists": true, "rows": 123, "path": "..."},
    "unmatched_bank": {"exists": true, "rows": 45, "path": "..."},
    "unmatched_ledger": {"exists": true, "rows": 30, "path": "..."},
    "ok": true
  }
  ```

---

### 出库表-平台结算流水匹配 (Outbound Settlement Match)

工作流: **Detect → Clean Settlement → Clean Outbound → Match**

模块代码: `audit_workflow/outbound_settlement_match/`
测试脚本: `scripts/test_osm_pipeline.py`

输入目录结构:
```
inputs/{客户}/outbound_settlement_match/
  settlement/          ← 平台结算流水 CSV（按月分子目录，如 01-22/, 02-22/）
  outbound/            ← 月度收入报告 Excel（含 Sellout/Refund/Return/Transfer 工作表）
```

输出目录结构:
```
outputs/{客户}/outbound_settlement_match/
  clean/
    settlement_all.csv              ← 统一结算流水（全年度）
    settlement_monthly_summary.csv  ← 月度结算汇总
    settlement_file_index.csv       ← 结算文件索引
    sellout.csv                     ← 清洗后的出库数据
    refund.csv                      ← 清洗后的仅退款数据
    return.csv                      ← 清洗后的退货数据
    transfer.csv                    ← 清洗后的退仓数据
    outbound_sheet_index.csv        ← 工作表分类索引
  matches/
    net_outbound.csv                ← 净出库（过滤退款/退货/退仓后）
    matched.csv                     ← 匹配成功的记录
    unmatched_outbound.csv          ← 未匹配的出库记录
    unmatched_settlement.csv        ← 未匹配的结算记录
    monthly_match_summary.csv       ← 月度匹配汇总
```

#### 25. POST /workflow/outbound_settlement_match/detect（OSM Step 1）

扫描 `inputs/{customer}/{task}/` 下的结算 CSV 和出库 Excel 文件，自动分类工作表类型。

工作表分类规则（基于工作表名称关键词）:
- **sellout** (出库): 名称含 "sellout" / "出库"
- **refund** (仅退款): 名称含 "refund only" / "仅退款" / "资损"
- **return** (货损): 名称含 "return wh" / "rongqing return" / "货损"
- **transfer** (退仓): 名称含 "return to bonded" / "退回保税仓"
- **skip**: recap 汇总、sellout filter、collect、通用 Sheet 等预处理/辅助工作表

- 表单字段:
  - `customer_name` (string, 必填) — 客户名称
  - `task_name` (string, 可选, 默认 `outbound_settlement_match`)
- 示例:
  ```bash
  curl -X POST -F "customer_name=ABC" \
    http://127.0.0.1:8000/workflow/outbound_settlement_match/detect
  ```
- 返回:
  ```json
  {
    "ok": true,
    "settlement_dir": "inputs/ABC/outbound_settlement_match/settlement",
    "settlement_files": [
      {"path": "settlement/01-22/xxx.csv", "name": "xxx.csv", "size": 12345}
    ],
    "outbound_dir": "inputs/ABC/outbound_settlement_match/outbound",
    "outbound_files": [
      {
        "path": "outbound/xxx.xlsx",
        "name": "xxx.xlsx",
        "size": 67890,
        "sheets": [
          {"name": "Recap", "type": "skip"},
          {"name": "Sellout-出库", "type": "sellout"},
          {"name": "refund only系统仅退款", "type": "refund"},
          {"name": "Rongqing Return WH", "type": "return"},
          {"name": "return to bonded", "type": "transfer"}
        ]
      }
    ]
  }
  ```

#### 26. POST /workflow/outbound_settlement_match/clean_settlement（OSM Step 2）

清洗平台结算流水 CSV 文件。自动检测文件类型（逐笔交易 vs 批次汇总），统一字段名为英文，按月汇总。

处理逻辑:
- 递归扫描 `settlement/` 下所有 CSV 文件
- 自动检测编码（UTF-8 / GBK / GB18030）
- 统一 17 个标准字段: `partner_txn_id`, `amount`, `rmb_amount`, `fee`, `settlement`, `rmb_settlement`, `currency`, `rate`, `payment_time`, `settlement_time`, `type`, `status`, `remarks`, `month`, `source_file` 等
- 按月份分组聚合，生成交易笔数/金额/费用/结算额统计

- 表单字段:
  - `customer_name` (string, 必填)
  - `task_name` (string, 可选, 默认 `outbound_settlement_match`)
- 示例:
  ```bash
  curl -X POST -F "customer_name=ABC" \
    http://127.0.0.1:8000/workflow/outbound_settlement_match/clean_settlement
  ```
- 返回:
  ```json
  {
    "ok": true,
    "settlement_csv": "outputs/ABC/outbound_settlement_match/clean/settlement_all.csv",
    "monthly_summary": "outputs/ABC/outbound_settlement_match/clean/settlement_monthly_summary.csv"
  }
  ```
- 错误: 500 (结算目录不存在 / 无 CSV 文件)

#### 27. POST /workflow/outbound_settlement_match/clean_outbound（OSM Step 3）

清洗月度收入报告 Excel 文件。自动识别工作表类型，提取 sellout/refund/return/transfer 数据为标准化 CSV。

处理逻辑:
- 扫描 `outbound/` 下所有 `.xlsx` 文件
- 自动检测表头行位置（处理有汇总行的工作表）
- 按工作表名称分类（见 Step 1 分类规则）
- 跳过预处理工作表（sellout filter / sellout filtered 等）
- 统一字段名映射（支持 TMF 平台 41 列和 WMS 平台 47 列两种出库表格式）
- 按类型分别输出 CSV

- 表单字段:
  - `customer_name` (string, 必填)
  - `task_name` (string, 可选, 默认 `outbound_settlement_match`)
- 示例:
  ```bash
  curl -X POST -F "customer_name=ABC" \
    http://127.0.0.1:8000/workflow/outbound_settlement_match/clean_outbound
  ```
- 返回:
  ```json
  {
    "ok": true,
    "paths": {
      "sellout": "outputs/ABC/.../clean/sellout.csv",
      "refund": "outputs/ABC/.../clean/refund.csv",
      "return": "outputs/ABC/.../clean/return.csv",
      "transfer": "outputs/ABC/.../clean/transfer.csv"
    }
  }
  ```
  注: 若某类型无数据，对应值为 `null`。
- 错误: 500 (出库目录不存在 / 无 Excel 文件)

#### 28. POST /workflow/outbound_settlement_match/match（OSM Step 4）

净出库过滤 + 平台结算流水 ID 匹配。

处理逻辑:
1. **净出库过滤**: 以 `order_id` 为唯一标识，从 sellout 中剔除出现在 refund/return/transfer 中的订单
2. **ID 匹配**: 净出库 `order_id` ←→ 结算流水 `partner_txn_id`
3. **金额比对**: 对匹配成功的记录计算出库金额与结算金额的差异
4. **月度汇总**: 按月统计匹配/未匹配数量和金额

- 表单字段:
  - `customer_name` (string, 必填)
  - `task_name` (string, 可选, 默认 `outbound_settlement_match`)
- 前置条件: 需先执行 Step 2 (clean_settlement) 和 Step 3 (clean_outbound)
- 示例:
  ```bash
  curl -X POST -F "customer_name=ABC" \
    http://127.0.0.1:8000/workflow/outbound_settlement_match/match
  ```
- 返回:
  ```json
  {
    "ok": true,
    "summary": {
      "sellout_total": 47661,
      "refund_count": 767,
      "return_count": 78,
      "transfer_count": 658,
      "filtered_count": 863,
      "net_outbound_count": 46798,
      "matched_count": 43456,
      "matched_outbound_orders": 41031,
      "unmatched_outbound_count": 3427,
      "unmatched_settlement_count": 540286,
      "match_rate": 92.64,
      "matched_outbound_amount": 10832970.80,
      "matched_settlement_amount": 8271636.47,
      "unmatched_outbound_amount": 847055.36,
      "unmatched_settlement_amount": 105289797.54,
      "outbound_key": "order_id",
      "settlement_key": "partner_txn_id"
    },
    "net_outbound": {"path": "...", "rows": 46798},
    "matched": {"path": "...", "rows": 43456},
    "unmatched_outbound": {"path": "...", "rows": 3427},
    "unmatched_settlement": {"path": "...", "rows": 540286},
    "monthly_summary": {"path": "...", "rows": 13}
  }
  ```
- 错误: 500 (缺少清洗产物 / 匹配字段不存在)

#### 29. POST /workflow/outbound_settlement_match/run_all

一键执行完整流水线: Detect → Clean Settlement → Clean Outbound → Match。

- 表单字段:
  - `customer_name` (string, 必填)
  - `task_name` (string, 可选, 默认 `outbound_settlement_match`)
- 示例:
  ```bash
  curl -X POST -F "customer_name=ABC" \
    http://127.0.0.1:8000/workflow/outbound_settlement_match/run_all
  ```
- 返回:
  ```json
  {
    "ok": true,
    "summary": {
      "matched_count": 43456,
      "match_rate": 92.64,
      "...": "..."
    },
    "detect": {
      "settlement_files": 287,
      "outbound_files": 9
    }
  }
  ```

---

### LLM 代码生成

#### 23. POST /llm/generate
发送 prompt 到 LLM，结果写入指定路径。
- 表单字段:
  - `prompt` (string, 必填)
  - `target_path` (string, 可选, 默认 `outputs/clean/generated_from_llm.txt`)
- 返回: `{"path": "...", "ok": true}`

#### 24. POST /llm/generate_and_run
LLM 生成代码 + 可选执行（`.py` 文件）。
- 表单字段:
  - `prompt` (string, 必填)
  - `target_path` (string, 可选, 默认 `outputs/clean/generated_from_llm.py`)
  - `run_code` (string, 可选, `true`/`false`, 默认 `false`)
  - `timeout` (int, 可选, 默认 5 秒)
- 示例:
  ```bash
  curl -X POST -F 'prompt=print("hello")' \
    -F 'target_path=outputs/clean/runme.py' \
    -F 'run_code=true' -F 'timeout=3' \
    http://127.0.0.1:8000/llm/generate_and_run
  ```
- 返回: `{"path":"...","ok":true,"run_result":{"returncode":0,"stdout":"...","stderr":"..."}}`

---

## 典型使用流程

```bash
# 1. 创建项目
curl -X POST http://127.0.0.1:8000/projects/create \
  -H "Content-Type: application/json" \
  -d '{"task_name":"序时账银行流水匹配","customer_name":"桂平金山"}'

# 2. 上传文件
curl -F "file=@./bank.xlsm" \
  http://127.0.0.1:8000/files/upload?dest=inputs/桂平金山/bank_ledger_match/

# 3. Step 1: 扫描 + LLM 识别 → 生成 task.yml
curl -X POST -F "customer_name=桂平金山" \
  http://127.0.0.1:8000/workflow/detect

# 4. Step 2: 查看并确认配置（前端 YAML 编辑器）
curl "http://127.0.0.1:8000/workflow/config?customer_name=桂平金山"

# 5. Step 3: 清洗
curl -X POST -F "customer_name=桂平金山" \
  http://127.0.0.1:8000/workflow/bank_ledger_match/clean

# 6. Step 5: 匹配（会生成 monthly_flow_check.csv）
curl -X POST -F "customer_name=桂平金山" \
  http://127.0.0.1:8000/workflow/bank_ledger_match/match

# 7. Step 4: 核查月度流量
curl -X POST -F "customer_name=桂平金山" \
  http://127.0.0.1:8000/workflow/bank_ledger_match/check

# 8. Step 6: 填入底稿
curl -X POST -F "customer_name=桂平金山" \
  http://127.0.0.1:8000/workflow/bank_ledger_match/fill
```

### 出库表-平台结算匹配 (OSM) 典型流程

```bash
# 方式一: 一键全流程
curl -X POST -F "customer_name=ABC" \
  http://127.0.0.1:8000/workflow/outbound_settlement_match/run_all

# 方式二: 逐步执行（推荐，便于检查结果）

# Step 1: 扫描文件 + 分类工作表
curl -X POST -F "customer_name=ABC" \
  http://127.0.0.1:8000/workflow/outbound_settlement_match/detect

# Step 2: 清洗结算流水
curl -X POST -F "customer_name=ABC" \
  http://127.0.0.1:8000/workflow/outbound_settlement_match/clean_settlement

# Step 3: 清洗出库报告
curl -X POST -F "customer_name=ABC" \
  http://127.0.0.1:8000/workflow/outbound_settlement_match/clean_outbound

# Step 4: 净出库过滤 + ID 匹配
curl -X POST -F "customer_name=ABC" \
  http://127.0.0.1:8000/workflow/outbound_settlement_match/match
```

也可使用测试脚本一键测试:
```bash
python scripts/test_osm_pipeline.py --customer ABC
python scripts/test_osm_pipeline.py --customer ABC match     # 只测匹配步骤
```

## 数据库表结构

### projects 表
| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER | 主键 |
| task_name | TEXT | 任务名称 |
| customer_name | TEXT | 客户名称 |
| status | TEXT | 状态 (Planning/Reviewing/Completed) |
| created_at | TEXT | 创建日期 |
| responsible_person | TEXT | 负责人 |
| risk | TEXT | 风险等级 |

### task_configs 表（新增）
| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER | 主键 |
| customer_name | TEXT | 客户名称 |
| task_name | TEXT | 任务名称 |
| task_yml_path | TEXT | task.yml 文件路径 |
| llm_yml_path | TEXT | llm.yml 文件路径 |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

UNIQUE(customer_name, task_name) — 每个公司+任务对应唯一一条 yml 路径映射。
