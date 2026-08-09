# -*- coding: utf-8 -*-
"""分析长期记忆写入/读取机制"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

import inspect
from agent import memory as mem_module

# Find all functions that mention user_memories or memory context
print("=== Functions writing to or reading from user_memories ===\n")
for name in dir(mem_module):
    obj = getattr(mem_module, name)
    if callable(obj) and hasattr(obj, '__code__'):
        try:
            src = inspect.getsource(obj)
            if 'user_memories' in src or 'build_memory_context' in name or 'save_memory' in name.lower():
                print("=== %s ===" % name)
                print(src[:1500])
                print()
        except Exception as e:
            pass
