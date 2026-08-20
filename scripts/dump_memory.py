# -*- coding: utf-8 -*-
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

from agent.memory import build_memory_context, get_connection
import inspect

# 1. 看 build_memory_context 源代码
src = inspect.getsource(build_memory_context)
with open(os.path.join(base, 'scripts', 'memory_src.txt'), 'w', encoding='utf-8') as f:
    f.write(src)
print("Source written to scripts/memory_src.txt")

# 2. 执行并保存原始输出
ctx = build_memory_context('zwy')
with open(os.path.join(base, 'scripts', 'memory_output.txt'), 'w', encoding='utf-8') as f:
    f.write(ctx if ctx else "(empty)")
print("Output written to scripts/memory_output.txt")
print("Output repr:", repr(ctx[:80]) if ctx else "None")
