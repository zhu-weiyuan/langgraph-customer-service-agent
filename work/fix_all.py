
with open('agent/runner.py', encoding='utf-8') as f:
    content = f.read()

# First, remove all corrupted docstring placeholders and their following content
import re
content = re.sub(
    r'# corrupted docstring - restored.*?(?=
s*def |
s*class |
s*@|


)',
    '',
    content,
    flags=re.DOTALL
)
content = re.sub(
    r'# (corrupted comment).*?(?=
s*def |
s*class |
s*@|


)',
    '',
    content,
    flags=re.DOTALL
)

# Fix the build_initial_state function - ensure proper docstring
build_func = '''def build_initial_state(
    session_id: str,
    user_message: str,
    prev_values: Optional[Dict[str, Any]] = None,
    trace_session: Any = None,
    idempotency_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble langgraph initial state.

    Parameters
    ----------
    prev_values : dict or None
        Checkpoint values from the last message (not including the latest human message).

    Returns
    -------
    State dict with messages / replies / intent / emotion / ending keys.
    """
    if prev_values and prev_values.get("messages"):
        history = list(prev_values["messages"])
    else:
        history = []
    state: Dict[str, Any] = {
        "messages": history + [HumanMessage(content=user_message)],
        "replies": [],
        "intent": "chat",
        "emotion": "neutral",
        "emotion_intensity": 1,
        "ending": False,
        "human_input": user_message,
    }
    # Note: do not put trace_session into graph state
    # checkpointer serialization will fail (TraceSession not msgpack-serializable)
    if idempotency_key is not None:
        state["_idempotency_key"] = idempotency_key
    if user_id:
        state["user_id"] = user_id
    return state

'''

# Replace the build_initial_state section
old_pattern = r'def build_initial_state(.*?return state'
content = re.sub(old_pattern, build_func.strip(), content, flags=re.DOTALL)

# Ensure import section is correct
content = re.sub(
    r'from langchain_core.messages import AIMessage, HumanMessage',
    'from langchain_core.messages import AIMessage, HumanMessage',
    content
)

with open('agent/runner.py', 'w', encoding='utf-8') as f:
    f.write(content)

import ast
try:
    ast.parse(content)
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error at line {e.lineno}: {e.msg}')
    if e.lineno:
        lines = content.split('\n')
        for i in range(max(0,e.lineno-3), min(len(lines), e.lineno+2)):
            print(f'  {i+1}: {lines[i][:80]}')
