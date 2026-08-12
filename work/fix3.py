with open('agent/runner.py', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
skip = False
for line in lines:
    if '# corrupted docstring - restored' in line or '# (corrupted comment)' in line:
        skip = True
        continue
    if skip:
        if line.strip().startswith('def ') or line.strip().startswith('class ') or line.strip().startswith('@'):
            skip = False
            new_lines.append(line)
        continue
    new_lines.append(line)

build = '''def build_initial_state(
    session_id: str,
    user_message: str,
    prev_values: Optional[Dict[str, Any]] = None,
    trace_session: Any = None,
    idempotency_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble langgraph initial state."""
    if prev_values and prev_values.get('messages'):
        history = list(prev_values['messages'])
    else:
        history = []
    state: Dict[str, Any] = {
        'messages': history + [HumanMessage(content=user_message)],
        'replies': [],
        'intent': 'chat',
        'emotion': 'neutral',
        'emotion_intensity': 1,
        'ending': False,
        'human_input': user_message,
    }
    if idempotency_key is not None:
        state['_idempotency_key'] = idempotency_key
    if user_id:
        state['user_id'] = user_id
    return state
'''

import re
content = ''.join(new_lines)
content = re.sub(r'def build_initial_state\(.*?return state', build.strip(), content, flags=re.DOTALL)

with open('agent/runner.py', 'w', encoding='utf-8') as f:
    f.write(content)

import ast
try:
    ast.parse(content)
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error at line {e.lineno}: {e.msg}')