"""
Agent Loop — 多轮对话式审计 Agent，支持工具调用与自动纠错。

核心组件：
- ConversationStore: 内存会话存储
- 3 个 Tool 函数: scan_files / read_file / execute_code
- build_agent_loop: 构建 pydantic-ai Agent
- run_agent_chat: 异步运行入口

关键设计：
  pydantic-ai 0.8.1 的 run_stream() 会在模型首次输出文本时停止 agent 循环
  （视为 final result），导致工具调用无法执行。
  因此改用 agent.run() + event_stream_handler 实现完整 agent 循环 + SSE 流式推送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle
import sqlite3
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterable

from audit_workflow.process_utils import run_safe

logger = logging.getLogger(__name__)

# ── 延迟导入 notify_frontend，避免循环引用 ──

_notify_fn = None


def _get_notify():
    global _notify_fn
    if _notify_fn is None:
        from desktop.common import notify_frontend
        _notify_fn = notify_frontend
    return _notify_fn


# ================================================================
# 数据结构
# ================================================================

@dataclass
class ConversationMessage:
    """对话中的单条消息。"""
    role: str           # 'user' | 'assistant' | 'system'
    type: str           # 'text' | 'thinking' | 'tool_call' | 'tool_result' | 'code_output' | 'error'
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)


# ================================================================
# 会话存储（SQLite 持久化）
# ================================================================

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "projects.db",
)


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class ConversationStore:
    """SQLite 持久化的会话存储。"""

    def create(self) -> str:
        conv_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO agent_conversations (id, title, created_at) VALUES (?, ?, ?)",
                (conv_id, "", now),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("创建新 Agent 会话: %s", conv_id[:8])
        return conv_id

    def get(self, conversation_id: str) -> dict | None:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id, title, created_at FROM agent_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None

            msg_rows = conn.execute(
                "SELECT role, type, content, timestamp, metadata_json "
                "FROM agent_messages WHERE conversation_id=? ORDER BY id",
                (conversation_id,),
            ).fetchall()

            messages = [
                ConversationMessage(
                    role=r["role"],
                    type=r["type"],
                    content=r["content"],
                    timestamp=r["timestamp"],
                    metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                )
                for r in msg_rows
            ]

            pydantic_messages = self.get_pydantic_messages(conversation_id)

            return {
                "messages": messages,
                "pydantic_messages": pydantic_messages,
                "title": row["title"],
                "created_at": row["created_at"],
            }
        finally:
            conn.close()

    def list_all(self) -> list[dict]:
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT c.id, c.title, c.created_at, COUNT(m.id) AS message_count "
                "FROM agent_conversations c "
                "LEFT JOIN agent_messages m ON m.conversation_id = c.id "
                "GROUP BY c.id "
                "ORDER BY c.created_at DESC",
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "title": r["title"] or f"对话 {r['id'][:8]}",
                    "created_at": r["created_at"],
                    "message_count": r["message_count"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def add_message(self, conversation_id: str, msg: ConversationMessage):
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO agent_messages (conversation_id, role, type, content, timestamp, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    msg.role,
                    msg.type,
                    msg.content,
                    msg.timestamp,
                    json.dumps(msg.metadata, ensure_ascii=False),
                ),
            )
            # 自动设置标题：第一条用户文本消息的前30个字符
            if msg.role == "user" and msg.type == "text":
                row = conn.execute(
                    "SELECT title FROM agent_conversations WHERE id=?",
                    (conversation_id,),
                ).fetchone()
                if row and not row["title"]:
                    conn.execute(
                        "UPDATE agent_conversations SET title=? WHERE id=?",
                        (msg.content[:30].strip(), conversation_id),
                    )
            conn.commit()
        finally:
            conn.close()

    def set_pydantic_messages(self, conversation_id: str, messages: list):
        blob = pickle.dumps(messages)
        conn = _get_db()
        try:
            conn.execute(
                "UPDATE agent_conversations SET pydantic_blob=? WHERE id=?",
                (blob, conversation_id),
            )
            conn.commit()
            logger.debug(
                "保存 pydantic 消息历史: conv=%s, messages=%d",
                conversation_id[:8], len(messages),
            )
        finally:
            conn.close()

    def get_pydantic_messages(self, conversation_id: str) -> list:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT pydantic_blob FROM agent_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if not row or not row["pydantic_blob"]:
                return []
            try:
                return pickle.loads(row["pydantic_blob"])
            except Exception:
                logger.warning("反序列化 pydantic 消息失败，返回空列表")
                return []
        finally:
            conn.close()

    def delete(self, conversation_id: str):
        conn = _get_db()
        try:
            conn.execute(
                "DELETE FROM agent_conversations WHERE id=?",
                (conversation_id,),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("删除 Agent 会话: %s", conversation_id[:8])


conversation_store = ConversationStore()


# ================================================================
# Tool 定义
# ================================================================

AGENT_SYSTEM_PROMPT = """\
你是一个审计工作流智能助手（AuditFlow Agent）。你可以扫描文件、读取数据、编写并执行 Python 代码。

## 强制规则
1. **必须使用工具**：收到用户请求后，第一步必须调用 scan_files 扫描目录，不要只说"让我扫描一下"而不调用工具。
2. **禁止扫描根目录**：永远不要调用 scan_files(".") 或 scan_files("")，这会导致文件数过多、上下文溢出。始终扫描具体的子目录，如 inputs/{客户名}/{任务名}/ 或 outputs/{客户名}/{任务名}/。
3. 用 read_file 查看文件内容和数据结构，再编写处理代码。
4. 用 execute_code 执行 Python 代码。代码执行出错时，分析错误并修正后重试。
5. 生成的输出文件必须保存到 outputs/ 目录下。

## 项目目录结构
- inputs/{客户名}/{任务名}/ — 输入数据文件（Excel、CSV 等）
- outputs/{客户名}/{任务名}/ — 输出结果文件
- config/ — 配置文件
- desktop/ — 前端代码

## 代码编写规范
- 使用 pandas 处理表格数据，Excel 用 openpyxl 引擎
- CSV 尝试 utf-8、gbk、gb18030 编码
- 输出文件保存到 outputs/ 目录下
- 代码注释使用中文

## 回复规范
- 用中文回复
- 先调用工具获取信息，再给出分析结论
- 不要在文本中描述你"将要"调用工具，直接调用即可
"""


# scan_files 排除的目录名（防止扫描 .venv 等超大噪音目录）
_SCAN_EXCLUDED_DIR_NAMES = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".git",
    "generated_parsers", ".qoderworkcn",
})
_SCAN_MAX_FILES = 500  # 单次扫描返回的最大文件数


def tool_scan_files(directory: str = "inputs") -> str:
    """扫描指定目录下的所有文件，返回 JSON 格式的文件列表。
    包含文件名、路径、大小、扩展名和修改时间。
    自动排除 .venv/node_modules/__pycache__/.git 等目录。
    单次最多返回 500 个文件，超出部分截断。

    Args:
        directory: 要扫描的子目录，相对于项目根目录。默认为 'inputs'。
    """
    logger.info("[tool] scan_files: directory=%s", directory)

    project_root = _current_project_root
    target = os.path.join(project_root, directory)

    # 硬保护：禁止扫描项目根目录本身（防止上下文溢出）
    resolved = os.path.normpath(target)
    root_resolved = os.path.normpath(project_root)
    if resolved == root_resolved:
        logger.warning("[tool] scan_files: 拒绝扫描项目根目录")
        return json.dumps({
            "error": "禁止扫描项目根目录，文件数过多会导致上下文溢出。请指定具体子目录，如 inputs/{客户名}/{任务名}/",
            "files": [],
        }, ensure_ascii=False)

    if not os.path.isdir(target):
        logger.warning("[tool] scan_files: 目录不存在: %s", target)
        return json.dumps(
            {"error": f"目录不存在: {directory}", "files": []},
            ensure_ascii=False,
        )

    files = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(target):
        # 原地修改 dirnames 以跳过排除目录（os.walk 支持此用法）
        dirnames[:] = [d for d in dirnames if d not in _SCAN_EXCLUDED_DIR_NAMES]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, project_root).replace("\\", "/")
            try:
                stat = os.stat(fpath)
                files.append({
                    "path": rel,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "extension": os.path.splitext(fname)[1],
                })
            except OSError:
                files.append({"path": rel, "size": -1, "error": "stat failed"})
            if len(files) >= _SCAN_MAX_FILES:
                truncated = True
                break
        if truncated:
            break

    result: dict[str, Any] = {
        "directory": directory,
        "file_count": len(files),
        "files": files,
    }
    if truncated:
        result["warning"] = (
            f"文件数超过上限 {_SCAN_MAX_FILES}，结果已截断。"
            f"请缩小扫描范围（指定更具体的子目录）。"
        )

    logger.info("[tool] scan_files: 找到 %d 个文件 (目录: %s, 截断: %s)",
                len(files), directory, truncated)
    return json.dumps(result, ensure_ascii=False)


def tool_read_file(file_path: str, max_lines: int = 100) -> str:
    """读取指定文件的前 N 行内容。

    Args:
        file_path: 文件路径，相对于项目根目录。
        max_lines: 最多读取的行数，默认 100。
    """
    logger.info("[tool] read_file: path=%s, max_lines=%d", file_path, max_lines)

    project_root = _current_project_root
    abs_path = os.path.join(project_root, file_path)

    if not os.path.isfile(abs_path):
        logger.warning("[tool] read_file: 文件不存在: %s", abs_path)
        return f"错误: 文件不存在: {file_path}"

    content = None
    for encoding in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(abs_path, "r", encoding=encoding) as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append(f"\n... (已截断，显示前 {max_lines} 行)")
                        break
                    lines.append(line)
                content = "".join(lines)
            logger.info("[tool] read_file: 成功 (%s编码, %d行)", encoding, len(content.splitlines()))
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    if content is None:
        logger.warning("[tool] read_file: 所有编码均失败")
        content = "错误: 无法以任何支持的编码读取文件"

    return content


def tool_execute_code(code: str, timeout: int = 30) -> str:
    """在子进程中执行 Python 代码，返回 stdout 和 stderr。
    代码运行环境与后端相同，工作目录为项目根目录。

    Args:
        code: 要执行的 Python 源代码。
        timeout: 最大执行时间（秒），默认 30。
    """
    logger.info("[tool] execute_code: code_length=%d, timeout=%d", len(code), timeout)
    logger.debug("[tool] execute_code: code_preview=\n%s", code[:500])

    project_root = _current_project_root

    # 保存代码到文件（方便调试和追溯）
    code_dir = os.path.join(project_root, "outputs", "agent_code")
    os.makedirs(code_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    code_file = os.path.join(code_dir, f"agent_{ts}.py")
    try:
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(f"# Agent 自动生成 — {datetime.now().isoformat()}\n")
            f.write(code)
        logger.info("[tool] execute_code: 代码已保存到 %s", code_file)
    except Exception as e:
        logger.warning("[tool] execute_code: 保存代码失败: %s", e)

    try:
        proc = run_safe(
            [sys.executable, "-c", code],
            timeout=timeout,
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # 截断过长输出，防止 token 爆炸
        max_output = 4000
        stdout_full_len = len(stdout)
        stderr_full_len = len(stderr)
        if len(stdout) > max_output:
            stdout = stdout[:max_output] + f"\n... (已截断，共 {stdout_full_len} 字符)"
        if len(stderr) > max_output:
            stderr = stderr[:max_output] + f"\n... (已截断，共 {stderr_full_len} 字符)"

        if proc.returncode == 0:
            logger.info("[tool] execute_code: 成功 (stdout=%d字符)", stdout_full_len)
        else:
            logger.warning(
                "[tool] execute_code: 失败 returncode=%d stderr_preview=%s",
                proc.returncode, stderr[:200],
            )

        result_parts = [f"returncode: {proc.returncode}"]
        if stdout:
            result_parts.append(f"stdout:\n{stdout}")
        if stderr:
            result_parts.append(f"stderr:\n{stderr}")
        return "\n".join(result_parts)

    except subprocess.TimeoutExpired:
        logger.warning("[tool] execute_code: 超时 (%d秒) — 进程树已被 run_safe 清理", timeout)
        return f"错误: 执行超时（{timeout}秒）"


# 模块级变量，由 run_agent_chat 在每次调用前设置
_current_project_root: str = ""


# ================================================================
# Agent 构建
# ================================================================

def build_agent_loop(llm_config: dict, project_root: str,
                     customer_name: str = "", task_name: str = ""):
    """构建带工具的 pydantic-ai Agent。"""
    global _current_project_root
    _current_project_root = project_root

    from pydantic_ai import Agent, Tool
    from pydantic_ai.models.openai import OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from audit_workflow.llm_agent import resolve_provider

    provider_cfg = resolve_provider(llm_config)
    api_key = provider_cfg.api_key
    base_url = provider_cfg.base_url
    model_name = provider_cfg.model_name

    if not api_key:
        raise RuntimeError("未设置 API Key，请检查 LLM 配置。")
    if not model_name:
        raise RuntimeError("未设置模型名，请检查 LLM 配置。")

    provider = OpenAIProvider(api_key=api_key, base_url=base_url or None)
    model = OpenAIModel(model_name, provider=provider)

    # 动态拼接系统提示：若指定了客户名和任务名，注入当前项目路径
    # 注：task_name 已由 api._resolve_task_dir_name() 通过 config.task_definitions.yml 解析为英文 dir_name
    system_prompt = AGENT_SYSTEM_PROMPT
    if customer_name and task_name:
        system_prompt += (
            f"\n\n## 当前项目\n"
            f"- 客户名称: {customer_name}\n"
            f"- 任务名称: {task_name}\n"
            f"- 输入目录: inputs/{customer_name}/{task_name}/\n"
            f"- **输出目录: outputs/{customer_name}/{task_name}/** — 所有生成的文件必须保存到此目录下\n"
            f"- 请优先扫描 inputs/{customer_name}/{task_name}/ 目录下的输入文件\n"
        )

    agent = Agent(
        model,
        system_prompt=system_prompt,
        tools=[
            Tool(tool_scan_files),
            Tool(tool_read_file),
            Tool(tool_execute_code),
        ],
        # 禁用 thinking mode（qwen-thinking 模型默认开启，与 tool_choice 冲突）
        model_settings={"extra_body": {"enable_thinking": False}},
    )
    logger.info("Agent 构建成功: model=%s, tools=3", model_name)
    return agent


# ================================================================
# 异步运行入口
# ================================================================

def _extract_usage_from_run_result(result) -> dict:
    """从 pydantic-ai AgentRunResult 中提取 token 用量。"""
    usage = {"total_tokens": 0, "request_tokens": 0, "response_tokens": 0}
    try:
        u = result.usage()
        usage["total_tokens"] = getattr(u, "total_tokens", 0) or 0
        usage["request_tokens"] = getattr(u, "request_tokens", 0) or 0
        usage["response_tokens"] = getattr(u, "response_tokens", 0) or 0
    except Exception:
        pass
    return usage


async def run_agent_chat(
    message: str,
    conversation_id: str,
    llm_config: dict,
    project_root: str,
    token_tracker,
    customer_name: str = "",
    task_name: str = "",
) -> dict:
    """运行一轮 Agent 对话。

    使用 agent.run() + event_stream_handler 实现完整 agent 循环：
    - event_stream_handler 在每次模型请求时接收流式事件（文本 delta、工具调用等）
    - agent.run() 内部自动处理工具调用 → 工具结果 → 下一轮模型请求的循环
    - 循环直到模型产生最终文本输出

    注意：不能使用 run_stream()，因为它在模型首次输出文本时就停止循环。

    返回: {"conversation_id", "ok", "response", "usage", "messages"}
    """
    global _current_project_root
    _current_project_root = project_root

    notify = _get_notify()

    logger.info(
        "=== Agent 对话开始 === conv=%s, message_length=%d, project_root=%s",
        conversation_id[:8], len(message), project_root,
    )

    # 确保会话存在
    conv = conversation_store.get(conversation_id)
    if not conv:
        conversation_id = conversation_store.create()
        conv = conversation_store.get(conversation_id)

    # 记录用户消息
    user_msg = ConversationMessage(role="user", type="text", content=message)
    conversation_store.add_message(conversation_id, user_msg)
    logger.info("用户消息已记录: conv=%s", conversation_id[:8])

    # 构建 Agent
    try:
        agent = build_agent_loop(llm_config, project_root, customer_name, task_name)
    except Exception as e:
        logger.error("Agent 构建失败: %s", e, exc_info=True)
        error_msg = ConversationMessage(
            role="assistant", type="error",
            content=f"Agent 构建失败: {e}",
            metadata={"error_type": "llm"},
        )
        conversation_store.add_message(conversation_id, error_msg)
        return {"conversation_id": conversation_id, "ok": False, "error": str(e)}

    # 获取 pydantic-ai 多轮历史
    history = conversation_store.get_pydantic_messages(conversation_id)
    logger.info(
        "加载历史: conv=%s, history_messages=%d",
        conversation_id[:8], len(history) if history else 0,
    )

    # 推送开始事件
    notify("agent_step", {
        "step_type": "thinking",
        "conversation_id": conversation_id,
        "content": "正在分析您的请求...",
    })

    # ── 构建 event_stream_handler ──
    # 用于在 agent.run() 执行过程中实时推送 SSE 事件
    # 收集所有文本 delta，同时推送工具调用/结果事件

    from pydantic_ai import messages as pai_messages

    # 用于在 handler 闭包中累积数据
    _handler_state = {
        "text_chunks": [],       # 所有文本片段
        "model_turn": 0,         # 当前模型请求轮次
    }

    async def event_handler(run_ctx, event_stream: AsyncIterable):
        """pydantic-ai event_stream_handler: 处理每次模型请求的流式事件。"""
        _handler_state["model_turn"] += 1
        turn = _handler_state["model_turn"]
        logger.debug("event_handler: 模型请求轮次 #%d", turn)

        async for event in event_stream:
            ek = getattr(event, "event_kind", None)

            if ek == "part_start":
                part = getattr(event, "part", None)
                if part is None:
                    continue
                pk = getattr(part, "part_kind", None)

                if pk == "text":
                    # 文本开始 — 推送首个文本 delta
                    text = getattr(part, "content", "")
                    if text:
                        _handler_state["text_chunks"].append(text)
                        notify("agent_step", {
                            "step_type": "text_delta",
                            "conversation_id": conversation_id,
                            "delta": text,
                        })
                        logger.debug("text_delta (start): %d chars", len(text))

                elif pk == "tool_call":
                    # 工具调用开始（仅记录日志，SSE 由 function_tool_call 推送，避免重复）
                    tool_name = getattr(part, "tool_name", "?")
                    logger.debug("[event] part_start tool_call: %s (turn #%d)", tool_name, turn)

            elif ek == "part_delta":
                delta = getattr(event, "delta", None)
                if delta is None:
                    continue
                dk = getattr(delta, "part_delta_kind", None)

                if dk == "text":
                    # 文本增量
                    chunk = getattr(delta, "content_delta", "")
                    if chunk:
                        _handler_state["text_chunks"].append(chunk)
                        notify("agent_step", {
                            "step_type": "text_delta",
                            "conversation_id": conversation_id,
                            "delta": chunk,
                        })

                elif dk == "tool_call":
                    # 工具调用参数增量（通常不需要处理）
                    pass

            elif ek == "function_tool_call":
                # agent 正在执行工具 — 先终结当前流式文本气泡，再推送工具调用
                part = getattr(event, "part", None)
                tool_name = getattr(part, "tool_name", "?") if part else "?"
                logger.info("[event] function_tool_call: %s", tool_name)

                # 终结流式文本（防止工具调用后的文本与之前的文本混在一个气泡里）
                notify("agent_step", {
                    "step_type": "finalize_stream",
                    "conversation_id": conversation_id,
                })

                notify("agent_step", {
                    "step_type": "tool_call",
                    "tool_name": tool_name,
                    "arguments": _safe_parse_args(
                        getattr(part, "args", "") if part else ""
                    ),
                    "code_preview": _extract_code_preview(
                        getattr(part, "args", "") if part else ""
                    ),
                })

            elif ek == "function_tool_result":
                # 工具执行完成
                result_part = getattr(event, "result", None)
                tool_call_id = getattr(event, "tool_call_id", "")

                # 从 ToolReturnPart / RetryPromptPart 中提取信息
                result_str = ""
                result_tool_name = "tool"
                if isinstance(result_part, str):
                    result_str = result_part
                elif hasattr(result_part, "content"):
                    result_str = str(result_part.content)
                    result_tool_name = getattr(result_part, "tool_name", "tool") or "tool"
                else:
                    result_str = str(result_part) if result_part else ""

                # 判断是否成功
                success = "returncode: 0" in result_str or (
                    not result_str.startswith("错误") and "returncode: 1" not in result_str
                )

                logger.info(
                    "[event] function_tool_result: tool=%s, success=%s, result_length=%d",
                    result_tool_name, success, len(result_str),
                )

                # 提取 stdout/stderr 预览
                stdout_preview = ""
                stderr_preview = ""
                if "stdout:" in result_str:
                    parts = result_str.split("stdout:")
                    if len(parts) > 1:
                        stdout_preview = parts[1].split("\nstderr:")[0][:300].strip()
                if "stderr:" in result_str:
                    stderr_preview = result_str.split("stderr:")[-1][:300].strip()

                notify("agent_step", {
                    "step_type": "tool_result",
                    "tool_name": result_tool_name,
                    "success": success,
                    "result_preview": result_str[:200],
                    "stdout_preview": stdout_preview,
                    "stderr_preview": stderr_preview,
                })

                # 如果是代码执行，推送专门的 code_output 事件
                if "returncode:" in result_str:
                    _push_code_output(notify, result_str)

    # ── 运行 Agent ──
    usage_info = {"total_tokens": 0, "request_tokens": 0, "response_tokens": 0}
    response_text = ""

    try:
        logger.info("开始 agent.run()...")
        result = await agent.run(
            message,
            message_history=history if history else None,
            event_stream_handler=event_handler,
        )
        logger.info("agent.run() 完成")

        # 获取最终文本输出
        response_text = result.output if hasattr(result, "output") else ""
        if not isinstance(response_text, str):
            response_text = str(response_text) if response_text else ""

        # 提取 token 用量
        usage_info = _extract_usage_from_run_result(result)

        # 保存 pydantic-ai 消息历史
        all_msgs = result.all_messages()
        conversation_store.set_pydantic_messages(conversation_id, all_msgs)
        logger.info(
            "Agent 循环完成: text_length=%d, usage=%s, messages=%d",
            len(response_text), usage_info, len(all_msgs),
        )

    except Exception as e:
        logger.error("Agent chat error: %s", e, exc_info=True)
        error_msg = ConversationMessage(
            role="assistant", type="error",
            content=f"Agent 执行失败: {e}",
            metadata={"error_type": "llm", "detail": str(e)},
        )
        conversation_store.add_message(conversation_id, error_msg)
        notify("agent_step", {
            "step_type": "error",
            "conversation_id": conversation_id,
            "error_type": "llm",
            "content": str(e),
        })
        return {"conversation_id": conversation_id, "ok": False, "error": str(e)}

    # 如果 event_handler 收集到了文本但 result.output 为空，使用 handler 的文本
    if not response_text and _handler_state["text_chunks"]:
        response_text = "".join(_handler_state["text_chunks"])
        logger.info("使用 handler 收集的文本: length=%d", len(response_text))

    # 保存助手回复
    assistant_msg = ConversationMessage(
        role="assistant", type="text", content=response_text,
    )
    conversation_store.add_message(conversation_id, assistant_msg)

    # 记录 token 用量
    token_tracker.record(usage_info)

    # 推送完成事件
    notify("agent_step", {
        "step_type": "done",
        "conversation_id": conversation_id,
        "usage": usage_info,
    })

    logger.info("=== Agent 对话完成 === conv=%s", conversation_id[:8])

    # 重新获取最新的对话数据（包含刚刚保存的用户和助手消息）
    conv = conversation_store.get(conversation_id)

    return {
        "conversation_id": conversation_id,
        "ok": True,
        "response": response_text,
        "usage": usage_info,
        "messages": [
            {
                "role": m.role, "type": m.type, "content": m.content,
                "timestamp": m.timestamp, "metadata": m.metadata,
            }
            for m in (conv["messages"] if conv else [])
        ],
    }


# ================================================================
# 辅助函数
# ================================================================

def _safe_parse_args(args) -> dict:
    """安全地解析工具参数（可能是 JSON 字符串或已经是 dict）。"""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args)
        except (json.JSONDecodeError, ValueError):
            return {"raw": args[:200]}
    return {"raw": str(args)[:200]}


def _extract_code_preview(args) -> str:
    """从工具参数中提取代码预览。"""
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            return ""
    elif isinstance(args, dict):
        parsed = args
    else:
        return ""

    code = parsed.get("code", "")
    if code:
        return code[:500] + ("..." if len(code) > 500 else "")
    return ""


def _push_code_output(notify, result_str: str):
    """从 execute_code 的结果字符串中提取 stdout/stderr，推送 code_output 事件。"""
    returncode = -1
    stdout = ""
    stderr = ""

    # 解析 returncode
    for line in result_str.split("\n"):
        if line.startswith("returncode:"):
            try:
                returncode = int(line.split(":")[1].strip())
            except (ValueError, IndexError):
                pass

    # 提取 stdout / stderr
    if "stdout:" in result_str:
        after_stdout = result_str.split("stdout:", 1)[1]
        if "stderr:" in after_stdout:
            stdout = after_stdout.split("stderr:", 1)[0].strip()
        else:
            stdout = after_stdout.strip()

    if "stderr:" in result_str:
        stderr = result_str.split("stderr:", 1)[1].strip()

    notify("agent_step", {
        "step_type": "code_output",
        "returncode": returncode,
        "stdout": stdout[:2000],
        "stderr": stderr[:2000],
    })
