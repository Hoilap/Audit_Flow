"""desktop.routes_agent — Agent Loop 端点。"""

from fastapi import APIRouter, HTTPException

from .common import (
    logger,
    token_tracker,
    _default_llm_config,
    _project_root,
    _ensure_project_dirs,
    _resolve_task_dir_name,
    AgentChatPayload,
)
from audit_workflow.agent_loop import run_agent_chat, conversation_store

router = APIRouter()


@router.post("/agent/chat")
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


@router.get("/agent/conversations")
def agent_list_conversations():
    """列出所有 Agent 对话。"""
    return {"conversations": conversation_store.list_all()}


@router.get("/agent/conversations/{conversation_id}")
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


@router.post("/agent/conversations/new")
def agent_new_conversation():
    """创建新的 Agent 对话。"""
    conv_id = conversation_store.create()
    return {"conversation_id": conv_id}


@router.delete("/agent/conversations/{conversation_id}")
def agent_delete_conversation(conversation_id: str):
    """删除一个 Agent 对话。"""
    conversation_store.delete(conversation_id)
    return {"ok": True}
