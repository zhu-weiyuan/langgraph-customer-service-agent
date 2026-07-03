"""
State 定义 - LangGraph 智能客服 Agent

LangGraph 的核心概念：
- State: 定义了整个图中所有节点可以访问和修改的数据结构
- 使用 TypedDict 来定义状态 schema，确保类型安全
- 每个节点接收当前状态，返回要更新的状态字段
"""

from typing import TypedDict, Annotated, List, Optional
from langgraph.graph.message import add_messages


# 客服 Agent 的状态定义
# 这是整个图的数据总线，所有节点都通过它来传递信息
class CustomerServiceState(TypedDict):
    # 消息历史 - 使用 add_messages 作为 reducer，自动合并新旧消息
    # 这是 LangGraph 的标准模式，支持多轮对话的上下文管理
    messages: Annotated[List, add_messages]

    # 意图识别结果：'consult'(咨询) / 'complaint'(投诉) / 'chat'(闲聊)
    intent: Optional[str]

    # 机器人回复内容
    bot_reply: Optional[str]

    # 用户满意度：True/False/None
    satisfaction: Optional[bool]

    # 重试次数（用于控制满意度检查的重试逻辑）
    retry_count: int

    # 是否需要转人工
    escalate: bool

    # 会话 ID（用于 checkpointer 恢复）
    session_id: Optional[str]

    # 用户情绪：'neutral'/'angry'/'sad'/'anxious'/'happy'
    emotion: Optional[str]

    # 情绪强度：1-5
    emotion_intensity: int

    # Agentic RAG: 检索结果缓存
    rag_results: Optional[List]

    # Agentic RAG: 当前检索轮次
    rag_round: int

    # Observability: trace session (for recording request lifecycle)
    trace_session: Optional[object]
