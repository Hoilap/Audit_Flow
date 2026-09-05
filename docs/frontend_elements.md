# 前端要素名称与术语说明

> 适用范围：`desktop/renderer/`（Electron 渲染进程）
> 入口：`index.html` → `src/main.js`，样式：`styles.css`
> 更新日期：2026-08-08

---

## 一、核心术语：什么是 Block？什么是 Message？

这两个词在本项目中是**不同层级的概念**，先厘清定义再看后面的要素清单。

### Block（区块）

**Block 是页面中一个视觉上独立的矩形功能区域**，通常具有边框、背景色和圆角（CSS 中由统一的卡片样式定义）。Block 是一个布局/概念单位，不是某个固定的 CSS 类名。

共享同一套"卡片外观"的区块样式（见 `styles.css`）：

```css
.model-card, .card, .message, .timeline, .file-row,
.finding-row, .task-card, .step-card {
  background: var(--surface-raised);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
}
```

例如 **Agent 工作流页面**从上到下由 5 个 block 组成（当前顺序）：

| 顺序 | Block 名称 | DOM 容器 ID / 类名 | 说明 |
|---|---|---|---|
| 1 | 项目 block | `#workflow-task-list`（`.project-bar`） | 选择已有项目，或勾选"自定义"手动填客户名+任务名 |
| 2 | 步骤 block | `.card` 内的 `#workflow-step-list` | 横向排列的步骤卡片（`.step-card`） |
| 3 | 解析器 block | `#detect-method-bar`（`.project-bar`） | radio 单选切换当前步骤可用的解析器 |
| 4 | 额外需求 block | `#step-requirement-bar` | 仅 LLM 类步骤显示的文本框，内容传给 LLM |
| 5 | 消息 block | `#chat`（`.conversation`） | 按时间顺序堆叠的执行结果消息（见下） |

### Message（消息）

**Message 是消息 block（聊天区 `#chat`）内部的一条条执行记录卡片**，CSS 类名为 `.message`。它比 block 低一个层级：一个消息 block 里包含多条 message。

- 由 `ui.js` 的 `addMessage({ role, title, body, result, failed })` 创建并追加到 `#chat` 尾部；
- 每条 message 的结构：
  - `.message-head` — 头部：角色（Agent）、标题、时间、Token 用量、删除按钮（✕）、折叠按钮（▼）；点击头部可折叠/展开；
  - `.message-body` — 正文：文字段落（`body` 参数）+ 结果 JSON（`result` 参数渲染为 `pre.log-block`）。
- 特殊类型的 message（不是 addMessage 生成，而是专用渲染函数直接构造 `.message` 元素）：
  - Detect 识别结果表 → `renderDetectResult()`
  - 工作表清洗详情表 → `renderSheetTasks()`（id=`sheet-tasks-panel`）
  - 数据完备性检查报告 → `renderCheckResult()`

### 两者关系一句话

> **页面（page）由若干 block 组成；消息 block 里装的是 message。**

另注意区分：**Agent 对话页**的聊天气泡不叫 message，叫 **agent-msg**（`.agent-msg`），是另一套独立组件，见第三节。

---

## 二、全局布局要素

`index.html` 的骨架结构：

| 要素 | 选择器 | 说明 |
|---|---|---|
| 应用外壳 | `.app-shell` | 44px 顶栏 + 主体两行网格 |
| 顶部状态栏 | `.status-bar` | Agent 状态、进度、停止、计时、Token、模型 |
| 主体布局 | `.layout` | 三列网格：左栏 260px / 中间自适应 / 右栏 380px |
| 左栏（侧边栏） | `.sidebar` | 品牌区 + 导航 + 底部模型卡片 |
| 中间主区 | `.main` | 承载所有 page，同一时刻只显示一个 |
| 右栏（证据面板） | `.evidence` | 仅在 Agent 工作流页显示 |

### 2.1 状态栏（status-bar）

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| Agent 状态灯 | `#agent-dot`（`.status-dot`） | 颜色随状态变：running/validating/completed/failed |
| Agent 状态文字 | `#agent-status` | 如 "Agent Running" |
| 进度条 | `.progress-track` > `#agent-progress` | 宽度百分比由 setAgentStatus() 控制 |
| 停止按钮 | `#stop-task` | 仅运行中显示 |
| 计时器 | `#elapsed` | mm:ss |
| Token 计数 | `#tokens` | 累计 token 用量（SSE `token_updated` 实时刷新） |
| 当前模型 | `#status-model` | 顶栏显示的模型名（与左栏下拉联动） |
| 折叠左栏按钮 | `#toggle-sidebar` | ◀/▶ |
| 深色模式按钮 | `#theme-toggle` | 切换 `body.dark` |
| 刷新按钮 | `#refresh-all` | 并行刷新文件/Git日志/LLM配置/Token |

### 2.2 左栏（sidebar）

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 品牌区 | `.brand`（`.brand-title` / `.brand-subtitle`） | "AuditFlow / 智能审计工作流桌面端" |
| 导航列表 | `#nav`（`.nav`） | 由 `config.js` 的 `navItems` 数组渲染，每项为 `button[data-page]` |
| 底部容器 | `.sidebar-footer` | 模型卡片 + 提交按钮 |
| 当前模型卡片 | `.model-card` | 含"当前模型"标签 + `#model-select` 下拉框 |
| 模型下拉框 | `#model-select` | 选项格式 `模型名 (provider名)`，切换即调后端保存默认 provider |
| 提交证据链按钮 | `#commit-all` | Git 提交当前证据链 |

导航当前顺序：Agent 工作流 → Agent 对话 → 审计程序 → 工作底稿 → Dashboard → 项目管理 → 数据源 → 分析报告 → 设置（`config.js` 的 `navItems`，调整数组即可改顺序）。

### 2.3 右栏证据面板（evidence）

由 `config.js` 的 `evidencePanelSections` 五元组 `[id, 标题, 是否可见, 是否有刷新按钮, 是否可折叠]` 驱动渲染，每个区块是 `.panel-section`（头部 `.panel-section-header` + 主体 `.panel-section-body`）：

| Section ID | 标题 | 内容容器 |
|---|---|---|
| `review-editor` | 人工复核 | `#review-file-list`（复核文件列表，"打开复核"→ 复核编辑器 Modal） |
| `config-editor` | 配置确认 | `#config-editor-content`（task.yml 编辑器，Confirm 步骤时填充） |
| `project-file-tree` | 项目文件 | `#project-filetree`（当前项目 inputs/outputs 文件树，支持拖拽导入） |
| `git-log` | Git 历史 | `#git-log`（`.timeline`，默认折叠） |

文件树通用要素：`.tree-root`（根节点）、`.tree-folder`（文件夹，可折叠）、`.tree-file`（文件，可点击预览）、`.tree-icon`、`.tree-name`、`.tree-copy-btn`（复制路径）、`.tree-delete-btn`（删除，仅数据源页）。

---

## 三、各页面要素

一个 **page（页面）** 是 `<section class="page" id="page-{id}">`，与左栏导航一一对应；加 `.active` 类的才可见。默认页为 Agent 工作流。

### 3.1 Agent 工作流页（`#page-agent`）

页面头：标题 "Agent 工作流"、任务描述 `#active-task-desc`、按钮 `#run-next-step`（执行下一步）/ `#run-all-steps`（执行全部）。

五个 block 见第一节。各 block 内部要素：

**① 项目 block**（`renderProjectSelector()` 动态填充）

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 项目下拉 | `#project-select`（`.project-bar-select`） | 列出 projects 表全部项目 |
| 自定义勾选 | `#project-custom-check` | 勾选后切换手动输入模式 |
| 客户名输入 | `#project-customer-input`（`.project-bar-input`） | 自定义模式下的客户名 |
| 任务名下拉 | `#project-task-select` | 预定义工作流 + 自定义任务名 |

**② 步骤 block**

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 任务标题 | `#active-task-title` | 当前工作流名称 |
| 步骤列表 | `#workflow-step-list`（`.step-list`） | 横向滚动 |
| 步骤卡片 | `.step-card`（`data-step-index`） | 状态类：`.active` / `.done` / `.failed` |
| 步骤运行按钮 | `.run-step-btn`（`data-run-step`） | ▶ 独立运行该步骤 |
| 状态徽标 | `.badge` | Ready / Running / Done / Failed |

**③ 解析器 block**（`renderDetectMethodSelector()` 动态填充）

| 要素 | 说明 |
|---|---|
| 标签 "解析器" | `.project-bar-label` |
| 解析器 radio 组 | `name="detect-method"`（一般步骤）或 `name="match-method"`（match 步骤）——同一栏 radio 切换，选项来自 `task_definitions.yml` 的 `allow_llm` / `allow_scripts_list` |

**④ 额外需求 block**

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 容器 | `#step-requirement-bar`（`.step-requirement-bar`） | 仅 detect/clean-*/fill/match 等 LLM 步骤显示 |
| 文本框 | `#step-requirement` | 内容按 `taskRunKey`（任务:步骤）存入 `state.stepRequirements`，运行时传给 LLM |

**⑤ 消息 block**

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 聊天容器 | `#chat`（`.conversation`） | 纵向滚动，最大高度 480px |
| 消息卡片 | `.message` | 结构见第一节；由 `addMessage()` / 专用渲染函数追加 |

### 3.2 Agent 对话页（`#page-agent-loop`）

页面头工具栏：`#agent-new-conv`（新建对话）、`#agent-conv-select`（历史对话下拉）、`#agent-delete-conv`（删除当前对话）。

两列布局 `.agent-loop-container`：

**左列：聊天区 `.agent-loop-chat`**

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 消息流 | `#agent-messages`（`.agent-loop-messages`） | 初始显示欢迎语 `.agent-welcome` |
| 输入框 | `#agent-prompt` | Enter 发送，Shift+Enter 换行 |
| 状态文字 | `#agent-loop-status` | 如 "Agent 思考中..." |
| 发送按钮 | `#agent-send` | |

**聊天气泡 `.agent-msg`**（注意：不是第一节的 `.message`）——按 `type` 加类名 `agent-msg-{type}`：

| type | 类名 | 含义 |
|---|---|---|
| `text` | `.agent-msg-text` | 用户输入或 Agent 文本回答 |
| `thinking` | `.agent-msg-thinking` | Agent 思考过程（斜体、左竖线） |
| `tool_call` | `.agent-msg-tool` | 工具调用（可折叠 details：scan_files/read_file/execute_code） |
| `tool_result` | `.agent-msg-result` | 工具结果（`.success` / `.failure` 左竖线着色） |
| `code_output` | `.agent-msg-code` | 代码执行的 stdout/stderr |
| `error` | `.agent-msg-error` | 错误（红字） |

气泡头部 `.agent-msg-head`：角色 + `.agent-msg-time`；正文 `.agent-msg-body`（最高 500px 内滚）。流式输出中的气泡带 `data-streaming="true"`（蓝色描边）。

**右列：文件树面板 `.agent-loop-filetree`（`#agent-filetree-panel`）**

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 头部 | `.agent-filetree-header` | "项目文件" + 刷新按钮 `#agent-refresh-filetree` |
| 树主体 | `#agent-filetree`（`.agent-filetree-body`） | 支持拖拽导入（`.drag-over` / `.drop-hint`） |
| 输出路径选择器 | `#agent-output-selector`（`.agent-filetree-dropdowns`） | 客户行 `#agent-customer-input`（datalist `#agent-customer-list`）、任务行 `#agent-task-input`（datalist `#agent-task-list`）、路径回显 `#agent-output-path` |

### 3.3 审计程序页（`#page-programs`）

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 列表容器 | `#programs-list`（`.programs-list`） | 由 `renderProgramsList()` 渲染 |
| 程序卡片 | `.readme-card` | 每个工作流一张，头部 `.readme-card-head`（标题 + 风险徽标 + 来源，点击折叠）、正文 `.readme-content`（readme.md 的 Markdown 渲染，Mermaid 图动态渲染） |

### 3.4 工作底稿页（`#page-workpapers`）

`.tabs` 页签（审计目标/审计程序/测试结果/审计结论）+ 底稿预览区 `#workpaper-preview`。

### 3.5 Dashboard（`#page-dashboard`）

指标卡片网格 `.grid.metrics`（今日任务数 / 待审核项目 / 运行中的 Agent `#running-agents` / 风险预警数量）+ 最近审计项目表 + 风险分布。

### 3.6 项目管理页（`#page-projects`）

| 要素 | ID | 说明 |
|---|---|---|
| 新建项目按钮 | `#new-project-btn` | 弹出项目表单 Modal |
| 搜索框 | `#project-search` | 按任务/客户/负责人过滤 |
| 状态过滤 | `#project-filter-status` | Planning/Running/Reviewing/Completed |
| 风险过滤 | `#project-filter-risk` | Low/Medium/High/Critical |
| 项目表格 | `#project-table-body` | 行内 ✎ 编辑 / ✕ 删除按钮 |
| 项目表单 Modal | `#project-form` | 任务名下拉 `#form-task-name`（选"自定义输入..."出现 `#form-task-name-custom`）、客户 `#form-customer`、状态 `#form-status`、创建时间 `#form-created`、负责人 `#form-person`、风险 `#form-risk` |

### 3.7 数据源页（`#page-data`）

| 要素 | ID | 说明 |
|---|---|---|
| 项目定位下拉 | `#data-project-select` | 选中后自动把上传目标设为 `inputs/{客户}/{任务}/` |
| 文件选择 | `#upload-file` | |
| 上传目标路径 | `#upload-dest` | 默认 `inputs/` |
| 上传按钮 | `#upload-btn` | |
| 文件树 | `#file-tree`（`.file-tree`） | inputs + outputs 全量树，点击文件出数据画像 |
| 数据画像卡片 | `#data-profile-card` > `#data-profile-content` | `.profile-stats` 统计条、金额/日期字段识别、列信息 `.profile-cols`、前 5 行预览 |

### 3.8 分析报告页（`#page-reports`）

两张占位卡片：风险分析报告、管理建议书（`.report-list`）。

### 3.9 设置页（`#page-settings`）

| 要素 | ID / 类名 | 说明 |
|---|---|---|
| 配置路径显示 | `#settings-config-path` | 当前 config.llm.yml 路径 |
| 保存按钮 | `#settings-save-providers` | 批量保存全部 provider |
| 状态提示 | `#settings-provider-status` | |
| Provider 列表 | `#settings-provider-list` | 每项 `.settings-provider-card`：API Key（`#`眼睛切换明文 `.settings-eye-toggle`）、Base URL、Model、删除按钮 `.settings-delete-provider`，默认 provider 带"默认"徽标 |
| 添加按钮 | `#settings-add-provider` | 追加空白 provider 卡片 |

---

## 四、通用组件（跨页面）

| 组件 | 入口 | 要素 |
|---|---|---|
| Modal 通用组件 | `modal.js` `createModal()` | `.modal-overlay`（遮罩）> `.modal-container`（`.modal-header` 标题 `.modal-title` + 关闭 `.modal-close`；`.modal-body`）。ESC / 点遮罩关闭 |
| CSV/文本预览 | `previewModal.js` | 基于 Modal；CSV 渲染为表格（最多 1000 行），其他文本以 `pre` 显示 |
| 人工复核编辑器 | `reviewEditor.js` | `.review-toolbar`（`#review-save` 保存到 CSV / `#review-refresh` 重新加载）、`.review-table`（候选行：批准按钮 `.approve-btn` ✓通过/✗拒绝、可编辑文本域 `.review-ta`：bank_summary / ledger_summary / llm_reason / manual_note） |

---

## 五、前端文件职责速查

| 文件 | 职责 |
|---|---|
| `index.html` | 骨架：状态栏、三栏布局、模型卡片 |
| `styles.css` | 全部样式（含深色模式变量） |
| `src/main.js` | 初始化、事件绑定（导航/步骤/设置页/Agent 对话/模型下拉） |
| `src/config.js` | 导航顺序、工作流与步骤定义、右栏区块配置 |
| `src/state.js` | 全局状态对象与派生函数（activeTask/taskRunKey 等） |
| `src/ui.js` | 全部页面/block 的 HTML 渲染与消息渲染 |
| `src/actions.js` | 业务动作：步骤执行、项目 CRUD、LLM 配置同步、SSE 监听 |
| `src/agentActions.js` | Agent 对话：发送、SSE agent_step 处理、气泡渲染、会话管理 |
| `src/api.js` | 全部后端 HTTP 端点封装 |
| `src/reviewEditor.js` | 人工复核编辑器 Modal |
| `src/previewModal.js` / `src/modal.js` | 文件预览弹窗 / 通用 Modal |
| `src/csv.js` / `src/dom.js` / `src/logger.js` | CSV 解析 / DOM 工具 / 日志 |
