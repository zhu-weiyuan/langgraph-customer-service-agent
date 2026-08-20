import sys
sys.path.insert(0, '.')
from agent.context_assembler import ContextAssembler, TokenBudgetAllocator

# Test 2: budget truncation - task_goal and doc: should appear in output
alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem = ContextAssembler(allocator=alloc)
long_text = "word " * 400
state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [{'role':'user','content':long_text},{'role':'assistant','content':long_text}] * 5
}
bundle = asem.assemble(state, 'query', '')

sys_content = bundle.messages[0]['content']
print("System content (first 400 chars):")
print(repr(sys_content[:400]))
print()
print("Has task_goal:", "task_goal" in sys_content)
print("Has doc::", "doc:" in sys_content)

# Print all message roles and first few chars
for i, m in enumerate(bundle.messages[:5]):
    print(f"  msg[{i}]: role={m['role']} start={repr(m['content'][:60])}")
