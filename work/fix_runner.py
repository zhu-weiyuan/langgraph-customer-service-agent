
with open('agent/runner.py', encoding='utf-8') as f:
    lines = f.readlines()
new_doc = '''    """Interpret the final graph.ainvoke / astream(updates) values dict.
    Returns the {} used by the app/json layer.
    If state has stacked pending replies, the last participating Message(role="ai")
    content is used as reply.
    interrupt detected (__interrupt__ in values)
    -> interrupted=True -> reply_type="escalated".
    """
'''
lines[214] = new_doc
for _ in range(7):
    lines.pop(215)
with open('agent/runner.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
import ast
try:
    ast.parse(''.join(lines))
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error at line {e.lineno}: {e.msg}')
