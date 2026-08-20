with open('agent/runner.py', encoding='utf-8') as f:
    lines = f.readlines()

# Fix corrupted unicode characters in docstrings/comments
fixed_lines = []
for line in lines:
    new_line = []
    for ch in line:
        cp = ord(ch)
        # Keep ASCII and CJK ranges
        if cp < 0x80 or (0x4E00 <= cp <= 0x9FFF) or (0x3000 <= cp <= 0x303F):
            new_line.append(ch)
        elif cp >= 0xE000 and cp <= 0xF8FF:
            new_line.append('?')  # PUA corrupted
        elif cp > 0x7F and cp < 0x3000:
            new_line.append(' ')  # various misc ranges, replace with space
        else:
            new_line.append(ch)
    fixed_lines.append(''.join(new_line))

text = ''.join(fixed_lines)

# Fix: requests�超时 -> 超时 etc - just replace remaining errant chars
import re
text = re.sub(r'\s+', ' ', text)  # normalize whitespace

with open('agent/runner.py', 'w', encoding='utf-8') as f:
    f.write(text)

import ast
try:
    ast.parse(text)
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error at line {e.lineno}: {e.msg}')
    if e.lineno:
        t = text.split('\n')
        for i in range(max(0,e.lineno-3), min(len(t), e.lineno+2)):
            print(f'  {i+1}: {t[i][:100]}')