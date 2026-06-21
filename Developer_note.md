Message（聊天区气泡） overflow-y: auto → overflow: auto + word-break: break-word
Evidence（右栏文件树）
.review-table textarea.review-ta

outbound_settlement_match/llm_agent.py — OSM 专用 LLM Agent 薄封装，用 OsmCleanerScript Pydantic 模型调用 build_agent()
outbound_settlement_match/llm_cleaner.py — 核心模块，包含 ensure_llm_settlement_cleaner() 和 ensure_llm_outbound_cleaner()。LLM 读取样本行 → 生成 def clean(...) -> list[dict] 脚本 → 语法校验+重试（最多3次）→ 按列指纹缓存到 generated_parsers/osm/
修改 5 个文件：

utils.py — 新增 strip_code_fence() 辅助函数
settlement_cleaner.py — 新增 clean_settlement_llm()，逐文件调用 LLM 生成清洗脚本，失败回退到硬编码
outbound_cleaner.py — 新增 clean_outbound_llm()，逐 sheet 调用 LLM，失败回退到硬编码
pipeline.py — run_clean_settlement() 和 run_clean_outbound() 在 llm.enabled=true 时路由到 LLM 路径
desktop/api.py — _build_osm_config() 注入 _default_llm_config() 使 pipeline 能访问 LLM 配置
desktop\renderer\src\config.js对应所有的文字编辑
api.js有所有的前端端口，供action.js调用
前端 — ui.js：新增 renderSheetTasks(result, onRetry) 函数，渲染工作表清洗详情表（文件、工作表、类型、状态徽标、脚本名、行数、错误详情折叠面板、重试按钮）。

前端 — actions.js：clean-outbound 步骤完成后自动调用 renderSheetTasks，重试按钮点击时调用新端点并实时更新行内状态。

Token 用量追踪功能已全部接通。以下是修改摘要：

数据流： LLM 调用 → _generate_with_retry 累计 token → ensure_* 返回 (Path, usage) → clean_*_llm 汇总所有文件的用量 → 写入 config["_cleaning"][step]["usage"] + ["script_dir"] → API 返回 → 前端显示。

修改的文件：

llm_cleaner.py — ensure_llm_settlement_cleaner 和 ensure_llm_outbound_cleaner 的返回类型从 Path 改为 tuple[Path, dict]，缓存命中时返回零用量。

settlement_cleaner.py — clean_settlement_llm 解包 (script_path, usage) 并累加每个文件的 token 消耗，最后写入 config["_cleaning"]["settlement"]["usage"] 和 ["script_dir"]。

outbound_cleaner.py — 同上模式，clean_outbound_llm 累计所有 sheet 的 token 用量。

api.py — clean_settlement 和 clean_outbound 端点的返回值增加 usage 和 script_dir 字段。run_all 端点因为直接返回整个 _cleaning dict，自动包含。

actions.js — clean-settlement 和 clean-outbound 步骤的消息中新增 Token 用量行（总数/输入/输出）和脚本目录路径。

## 消息通知

要实现后端主动通知前端，有三种可行方案，按复杂度从低到高排列：

方案一：Server-Sent Events (SSE) — 推荐

最适合你当前的架构。后端用 FastAPI 的 StreamingResponse 推送事件流，前端用 EventSource 监听。单向通信，改动小。

后端大致这样写：

from fastapi.responses import StreamingResponse
import asyncio, json

# 全局队列，任务完成时往里塞消息

_event_queue: asyncio.Queue = asyncio.Queue()

def notify_frontend(event: str, data: dict):
    """在任务完成后调用"""
    _event_queue.put_nowait({"event": event, "data": data})

@app.get("/events")
async def event_stream():
    async def generator():
        while True:
            msg = await _event_queue.get()
            yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
    return StreamingResponse(generator(), media_type="text/event-stream")
前端监听：

const es = new EventSource('http://127.0.0.1:8000/events')
es.addEventListener('sheet_cleaned', (e) => {
  const data = JSON.parse(e.data)
  // 刷新表格、更新 token 显示等
  renderSheetTasks(data)
})
在你的 retry endpoint 或 pipeline 完成后调用 notify_frontend('sheet_cleaned', {...}) 即可。优点是实时、轻量，且不需要改现有请求逻辑。

方案二：轮询 (Polling)

最简单但最粗糙。前端定时请求一个状态接口：

setInterval(async () => {
  const res = await request('/workflow/outbound_settlement_match/status')
  if (res.has_updates) {
    // 刷新 UI
  }
}, 3000)
好处是实现简单，坏处是有延迟且浪费请求。适合任务状态变化不频繁的场景。

方案三：WebSocket

双向通信，功能最强但改动最大。需要加 websockets 依赖，改连接管理逻辑，前端用 new WebSocket() 代替部分 fetch()。对于你目前"后端完成后通知前端刷新"这个需求来说，WebSocket 有些大材小用。
