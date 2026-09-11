# AuditWorkflow 打包指南（Windows x64）

本文档描述如何把「Electron 前端 + Python 后端」的 AuditWorkflow 打成一个
NSIS 安装包，使目标机器**无需预装 Python、无需联网**即可运行。

## 一、方案总览

```
安装包（约 150MB）
├─ Electron 壳（asar：main.js / preload.js / renderer）      ~60MB
├─ resources/runtime/python  内置 Python 3.12 完整运行时      ~30MB
├─ resources/wheels          全部依赖的离线 wheel 包          ~60MB
└─ resources/app             后端源码（desktop/*.py、audit_workflow/、config 模板）
```

关键决策与原因：

1. **内置 Python 运行时而非 PyInstaller 冻结**：应用运行时会用
   `sys.executable` 执行 LLM 动态生成的代码（agent\_loop.py、generated\_parsers），
   冻结方案会破坏该机制。
2. **离线 wheels 而非在线安装**：审计现场多为受限网络；且 pydantic-ai/pydantic
   有精确版本约束，离线包可保证版本不漂移。目标机首启时由 main.js 自动
   `pip install --no-index`，一次完成（写入 `.deps-installed` 标记，之后跳过）。
3. **用户数据放 %APPDATA% 而非安装目录**：inputs/outputs/projects.db/
   config/日志全部落在 `app.getPath('userData')`（通过环境变量
   `AUDIT_WORKFLOW_DATA_DIR` 传给后端），升级安装不丢数据，也不受
   Program Files 写权限限制。
4. **per-user NSIS 安装**：默认装到用户目录，无需管理员权限。

## 二、打包步骤（在开发机上执行）

前置：已装 Node.js（含 npm）、Python 3.12（项目 `.venv` 已建好）。

```powershell
# 0. 进入项目根目录
cd C:\Users\<you>\Desktop\Audit_workflow

# 1. 下载内置 Python 运行时（python-build-standalone，约 25MB）
#    GitHub 直连慢的话，可手动下载脚本中打印的 URL，放到
#    desktop\runtime\.downloads\ 下再重跑
powershell -ExecutionPolicy Bypass -File packaging/fetch-python.ps1

# 2. 收集全部依赖的离线 wheels（约 60MB，按 win64 + cp312 平台）
powershell -ExecutionPolicy Bypass -File packaging/fetch-wheels.ps1

# 3. 安装 Electron 构建依赖（首次或 package.json 变更时）
cd desktop; npm install; cd ..

# 4. 一键打包（自动校验前置，产出 dist/AuditWorkflow Setup x.y.z.exe）
powershell -ExecutionPolicy Bypass -File packaging/build.ps1
```

日常迭代只需第 4 步；`requirements.txt` 变更后需重跑第 2 步；
Python 运行时版本变更后需重跑第 1、2 步（fetch-python 加 `-Force`）。

## 三、新机器上的运行时行为

1. 用户双击安装包 → per-user 安装到
   `C:\Users\<user>\AppData\Local\Programs\AuditWorkflow\`（可改路径，无需管理员）。
2. 首次启动：

   - main.js 在 `%APPDATA%\audit-workflow-desktop\` 建数据目录，
     把随包的 `config/` 模板种子到该目录（已有则不覆盖）；

   - 用内置运行时执行 `pip install --no-index --find-links=wheels -r requirements.txt`，
     写入 `.deps-installed` 标记；

   - 以数据目录为 cwd 启动 `python -m desktop.api`（127.0.0.1:8001，
     生产模式自动关闭 uvicorn reload）。
3. 之后启动跳过依赖安装，直接拉起后端。

## 四、开发模式不受影响

`npm start`（未打包）时：仍用项目根 `.venv` 的 Python、项目根即数据目录、
uvicorn 热重载开启（`AUDIT_DEV=1`）。行为与改造前完全一致。

## 五、目录对照表

| 内容                      | 开发模式                       | 打包模式                                  |
| ----------------------- | -------------------------- | ------------------------------------- |
| Python 解释器              | `.venv/Scripts/python.exe` | `resources/runtime/python/python.exe` |
| 后端源码                    | 项目根                        | `resources/app/`（PYTHONPATH 指向它）      |
| inputs/outputs          | 项目根                        | `%APPDATA%\audit-workflow-desktop\`   |
| projects.db / 日志        | 项目根                        | 同上                                    |
| config/\*.yml           | 项目根/config                 | 数据目录/config（首启从安装包种子）                 |

## 六、常见问题

- **fetch-python 下载慢/失败**：手动到
  [python-build-standalone releases](https://github.com/astral-sh/python-build-standalone/releases)
  下载脚本中 `$FileName` 对应文件，放入 `desktop/runtime/.downloads/` 后重跑。

- **pip download 报平台不匹配**：确认在 Windows x64 上、且用项目 `.venv`
  （Python 3.12）运行 fetch-wheels。

- **首次启动卡在安装依赖**：查看 Electron 控制台（或把 main.js 的
  console 重定向到日志）确认 `--no-index` 安装是否成功；wheels 目录缺失
  会被 build.ps1 前置校验拦下。

- **改了 requirements.txt**：重跑 fetch-wheels，删掉目标机
  `resources/runtime/python/.deps-installed`（或重装应用）触发重装。

- **需要应用图标**：在 `desktop/build/` 下放 `icon.ico`（≥256x256），
  electron-builder 会自动拾取。

## 七、涉及改动的文件

| 文件                           | 改动                                                                                                                           |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `desktop/main.js`            | 打包路径解析、Python 定位、依赖 bootstrap、数据目录种子、单实例锁（`requestSingleInstanceLock`，重复打开时聚焦已有窗口）、后端 stdout/stderr 落盘到 `<数据目录>/backend.log` |
| `desktop/common.py`          | `_resolve_data_root()` 支持 `AUDIT_WORKFLOW_DATA_DIR`，db/日志/inputs/outputs 路径随之切换                                              |
| `desktop/api.py`             | reload 仅在 `AUDIT_DEV=1` 时开启                                                                                                  |
| `desktop/package.json`       | electron-builder 配置 + `dist` 脚本                                                                                              |
| `packaging/fetch-python.ps1` | 下载/解压/裁剪内置 Python 运行时                                                                                                        |
| `packaging/fetch-wheels.ps1` | 按平台收集离线 wheels                                                                                                               |
| `packaging/build.ps1`        | 前置校验 + 一键构建 + 产物报告                                                                                                           |

## 八、实例数据随包种子（2026-09-10）

安装包内附带 inputs 示例客户数据（佰腾、桂平金山、TMF2），目标机首启时
种子到数据目录 `inputs/` 下，方便现场演示与验证。

| 文件                      | 改动                                                                     |
| ----------------------- | ---------------------------------------------------------------------- |
| `desktop/package.json`  | `extraResources` 增加 `../inputs` → `app/sample_inputs`，filter 仅含佰腾/桂平金山/TMF2 三个客户 |
| `desktop/main.js`       | `seedDataDir()` 第 3 步：`sample_inputs` 下客户目录递归复制到数据目录 `inputs/`，**目录已存在则不覆盖**（幂等，升级安装不破坏用户数据） |
| `desktop/common.py`     | `init_db()` 首次建库时向 `tasks` 表写入三个示例项目（佰腾/桂平金山/TMF2，编制人 AAA/CCC/EEE、审核人 BBB/DDD/FFF 等与开发库一致），使新机器"项目管理"页即有示例项目 |

注意：inputs 根下新增客户不会自动入包，需同步更新 package.json 的 filter；
TMF2 约 146MB（FSS 收入报告 xlsx），安装包体积约翻倍，构建耗时与磁盘占用同步增加。

DB 种子仅在**首次建库**时执行（`init_db()` 开头判断 `projects.db` 是否已存在且非空）：
目标机全新安装 → 种子三个示例项目并自动创建对应 inputs/outputs 目录；
已有库（含用户自行删空项目的库）→ 不种子，用户数据不受影响、删除的项目不会复活。
`task_configs` 表无需种子：`_resolve_task_config_paths()` 无映射记录时自动回退到
`outputs/客户/任务/task.yml` 默认路径。

## 十、LLM 配置脱敏（2026-09-10）

开发库的 `config/config.llm.yml`（provider `dashscope_3.7_lzd` 含硬编码真实
API Key）和 `.env.example`（真实 `DASHSCOPE_API_KEY`）曾随安装包分发。现改为：

| 文件                      | 改动                                                                     |
| ----------------------- | ---------------------------------------------------------------------- |
| `desktop/package.json`  | `../config` → `app/config` 增加 filter，只打包 `config.blm.detect.yaml`、`config.task_definitions.yml` 两个无密钥文件 |
| `desktop/main.js`       | `seedDataDir()` 只从 `config.example.matching.yml` 生成 `config.matching.yml`；生产 LLM 配置直接使用 `config/config.llm.production.yml` |
| `packaging/build.ps1`   | 构建完成后扫描解包资源，发现 `.env` 或 `.env.*` 时立即失败 |

规则：**config/ 下新增含密钥的文件时，必须同步更新 package.json 的 filter 将其排除**，
生产包不携带 `.env` / `.env.*`，也不会在首次启动时创建 `.env`。
`config.llm.production.yml` 的 provider 使用直接 `api_key` 字段；
开发和测试统一使用 `config/config.llm.development.yml`，可继续用 `api_key_env`。

注意：`config/config.llm.yml` 目前仍被 git 跟踪（开发密钥在提交历史中），
如需彻底清除需重写历史（`git filter-repo`），另行处理。

## 九、数据目录初始化修复（2026-09-05 排查）

### 问题现象

打包安装后首次启动：前端输入/输出区为空，看起来"没有本地数据库、
没有项目管理数据"。

### 排查结论

1. `projects.db` **实际已创建**（`%APPDATA%\audit-workflow-desktop\projects.db`，
   含种子项目），后端启动链路正常 —— 数据库本身没问题。
2. 真正的缺陷是**数据目录缺少 inputs/outputs 结构**：

   - `main.js seedDataDir()` 只种子 `config/`，不创建
     `inputs/`、`outputs/` 根目录；

   - `common.py init_db()` 种子默认项目时不调用 `_ensure_project_dirs()`，
     导致 `inputs/<客户>/<任务>`、`outputs/<客户>/<任务>` 不存在，
     `/projects/dirs` 扫描结果为空。
3. 目录命名不符合数据约定：`_ensure_project_dirs()` 直接使用中文任务名
   建目录，而既有数据约定为英文 `dir_name`（见
   `config/config.task_definitions.yml`，如 `bank_ledger_match`）。
4. 健壮性隐患：`_list_project_dirs()` 用 `os.getcwd()` 扫描，依赖
   "后端 cwd == 数据目录" 这一隐式条件。

### 修改方案

| # | 文件                  | 修改                                                                              |
| - | ------------------- | ------------------------------------------------------------------------------- |
| 1 | `desktop/main.js`   | `seedDataDir()` 增加创建 `inputs/`、`outputs/` 根目录                                   |
| 2 | `desktop/common.py` | `_ensure_project_dirs()` 内部用 `_task_def_dir_name()` 把中文任务名解析为英文 `dir_name` 再建目录 |
| 3 | `desktop/common.py` | `init_db()` 种子默认项目后，逐条调用 `_ensure_project_dirs()` 创建对应输入/输出目录                   |
| 4 | `desktop/common.py` | `_list_project_dirs()` 改用 `_resolve_data_root()` 替代 `os.getcwd()`               |

以上修改对开发模式无影响（数据根目录仍是项目根，目录已存在时幂等）。

## 十、Agent 对话 HTTP 500 修复（2026-09-05 排查）

### 问题现象

打包版本中使用 Agent 对话，请求失败 HTTP 500；开发模式（.venv）正常。

### 排查结论

`pydantic-ai-slim 0.8.1` 内部引用 `opentelemetry._events`，该模块在
`opentelemetry-api 1.44` 中已被移除。离线 wheels 按最新版本收集到
1.44.0，而开发 venv 中是 1.42.1，因此只有打包环境触发
`ModuleNotFoundError: No module named 'opentelemetry._events'`。

同时发现打包后后端 stdout/stderr 只进 Electron console（GUI 下不可见），
traceback 全部丢失，导致该类错误无法从日志定位。

### 修改方案

| # | 文件                         | 修改                                                          |
| - | -------------------------- | ----------------------------------------------------------- |
| 1 | `requirements.txt`         | 锁定 `opentelemetry-api>=1.26,<1.44`，并重新收集 wheels（1.42.1）     |
| 2 | `desktop/main.js`          | 后端 stdout/stderr 追加写入 `<数据目录>/backend.log`，打包后可排查 traceback |
| 3 | `desktop/routes_common.py` | `/git/log` 在空仓库（无提交）时返回空列表，不再 500                           |

已用新安装包全链路验证：`pip install --no-index` 成功、
`import opentelemetry._events` 通过、`POST /agent/chat` 返回 200 正常对话。

## 十一、刷新 / Ctrl+R 后仍卡住修复（2026-09-05 排查）

### 问题现象

打包版本打开后界面一直加载、无响应，按刷新 / Ctrl+R 依然卡住。

### 排查结论

1. `backend.log` 显示后端启动即退出：
   `ModuleNotFoundError: No module named 'uvicorn'` —— 首启的离线依赖
   安装没有完成（`resources/runtime/python/.deps-installed` 标记不存在），
   后端根本起不来。
2. **Ctrl+R 只刷新前端页面，不会重启后端进程**——后端已死，刷新多少次
   都一样卡住。恢复的唯一方式是**完全退出应用再重新打开**。
3. 首启依赖安装要跑几分钟，而旧代码是「先阻塞装依赖、再建窗口」，
   期间用户看不到任何界面，极易误以为没启动/卡死而强杀进程，
   导致依赖装到一半进入永久坏状态。
4. 旧代码 `installDependencies()` 失败后仅 console.error（GUI 下不可见）
   仍继续启动后端，故障完全无感知。
5. 手动用内置运行时重放
   `pip install --no-index --find-links=wheels -r requirements.txt`
   成功（uvicorn 0.52.4 等全部装上），随后后端冒烟测试
   `GET /projects/list` 返回 200，确认 wheels 本身没问题。

### 修改方案（desktop/main.js）

| # | 修改                                                                                                         |
| - | ---------------------------------------------------------------------------------------------------------- |
| 1 | 新增 `bootstrapLog()`：bootstrap/安装过程追加写入 `<数据目录>/bootstrap.log`，打包后可排查                                       |
| 2 | `installDependencies()`：`spawnSync` 加 `maxBuffer: 32MB`（防 pip 输出触发 ENOBUFS 误判失败），失败时把 pip stdout/stderr 落盘 |
| 3 | `startBackend()`：依赖安装失败时弹窗报错并退出，**不再带病启动后端**                                                               |
| 4 | 后端启动 30 秒内退出（code≠0）视为启动失败，弹窗指路 `backend.log`                                                              |
| 5 | 启动顺序调整为「先建窗口、后启动后端」，首启装依赖期间用户能看到界面，避免误杀进程                                                                  |

### 用户侧恢复方法

若遇到旧包造成的坏状态：完全退出应用（托盘/任务管理器确认无
AuditWorkflow 进程），重新打开即可——依赖会重新安装直至成功；
或手动删除 `resources/runtime/python/.deps-installed` 之外的残留后用新包重装。

## 十二、Agent 对话 HTTP 500（no such table）修复（2026-09-05 排查）

### 问题现象

打包版本打开 Agent 对话页即 HTTP 500；`backend.log` 中 traceback 为
`sqlite3.OperationalError: no such table: agent_conversations`。

### 排查结论

`audit_workflow/agent_loop.py` 的 `_DB_PATH` 按
`dirname(dirname(__file__))/projects.db` 相对源码定位。
打包后 `__file__` 位于**安装目录** `resources/app/audit_workflow/`，
于是连接到 `resources/app/projects.db` —— sqlite3 对不存在的文件会
**自动创建空库**，空库没有表，查询即报 no such table。

而 `agent_conversations` / `agent_messages` 两张表实际由
`desktop/common.py init_db()` 建在**用户数据目录**的
`projects.db`（`AUDIT_WORKFLOW_DATA_DIR`）中。
开发模式下两处路径恰好都是项目根，所以只有打包后触发。

### 修改方案

| # | 文件                             | 修改                                                                                       |
| - | ------------------------------ | ---------------------------------------------------------------------------------------- |
| 1 | `audit_workflow/agent_loop.py` | `_DB_PATH` 优先读 `AUDIT_WORKFLOW_DATA_DIR`（main.js 启动后端时总会设置），回退 `__file__` 相对定位（开发模式行为不变） |
| 2 | `desktop/package.json`         | 版本号 0.1.0 → 0.1.1，便于区分是否最新包（此前新旧包同号无法区分）                                                 |

### 如何确认当前运行的是最新版

1. 版本号：0.1.1 起，安装包文件名/卸载条目均含版本号，新旧可区分；
2. `bootstrap.log`：只有含第十一节修复的 main.js 才会写
   `%APPDATA%\audit-workflow-desktop\bootstrap.log`；
3. 行为验证：Agent 对话页不再 500、`GET /agent/conversations` 返回 200。
