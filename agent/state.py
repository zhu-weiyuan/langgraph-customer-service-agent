"""State 定义 - LangGraph 智能客服 Agent（唯一的 State 定义）

P1-A 重构要点：
- 本文件是全项目唯一的 ``CustomerServiceState`` 定义。graph.py 内联的重复
  定义（其 reducer ``lambda x, y: x + [y]`` 会把整批消息包成嵌套列表）已删除，
  统一从这里 import。
- ``messages`` 使用 langgraph 官方的 ``add_messages`` reducer（自动合并、去重、
  支持追加单条或多条消息）。
- langgraph import 带 try/except 守卫：在没有安装 langgraph 的环境（如纯
  stdlib 测试容器）中，退化为一个语义等价的扁平拼接 reducer，保证本模块
  永远可 import / 可编译。
"""

from typing import TypedDict, Annotated, List, Optional

try:  # 三方依赖守卫：langgraph 不可用时用扁平拼接 reducer 兜底
    from langgraph.graph.message import add_messages
except Exception:  # pragma: no cover - exercised only without langgraph
    def add_messages(left, right):  # type: ignore[misc]
        """Fallback reducer: flat-concat（绝不产生嵌套列表）。"""
        left = list(left or [])
        if right is None:
            return left
        if not isinstance(right, list):
            right = [right]
        return left + list(right)


class CustomerServiceState(TypedDict, total=False):
    """整个图的数据总线 — 所有节点读写的字段并集（state.py ∪ 旧 graph.py）。"""

    # 消息历史 - add_messages reducer 自动合并新旧消息（支持多轮对话）
    messages: Annotated[List, add_messages]

    # 意图识别结果：'consult'(咨询) / 'complaint'(投诉) / 'chat'(闲聊) / 'ending'
    intent: Optional[str]

    # 用户是否表达了结束对话的意图（意图识别节点写入，路由函数读取）
    ending: bool

    # 机器人回复内容（app 层读取用于展示/流式兜底）
    bot_reply: Optional[str]

    # 用户满意度：True/False/None
    satisfaction: Optional[bool]

    # 连续不满意次数（用于重试与升级人工的判断）
    retry_count: int

    # 是否需要转人工
    escalate: bool

    # 会话 ID（用于 checkpointer 恢复 / 本次对话 / trace）—— 一次对话的标识
    session_id: Optional[str]

    # 用户身份 ID —— **长期记忆的主键**（跨会话连续）。
    # 从认证态派生（JWT sub / X-User-Id / 匿名 IP）；未知时下游回退 session_id。
    user_id: Optional[str]

    # 用户情绪：'neutral'/'angry'/'sad'/'anxious'/'happy'
    emotion: Optional[str]

    # 情绪强度：1-5
    emotion_intensity: int

    # Agentic RAG: 检索结果缓存
    rag_results: Optional[List]

    # Agentic RAG: 当前检索轮次
    rag_round: int

    # Observability: 请求关联 ID(字符串,可序列化)。
    # 警告:不要把 TraceSession 等自定义对象放进 State ——
    # checkpoint 的 msgpack 序列化会失败并让整个请求 500。
    request_id: Optional[str]
