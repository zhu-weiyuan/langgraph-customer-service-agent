import sys
sys.path.insert(0, '.')
from agent.context_assembler import ContextAssembler, TokenBudgetAllocator

# Test 2: budget truncation behavior
alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem = ContextAssembler(allocator=alloc)
long_text = "word " * 400
state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [
        {'role':'user','content':long_text},
        {'role':'assistant','content':long_text}
    ] * 5
}

bundle = asem.assemble(state, 'query', 'sess-test')

sys_content = bundle.messages[0]['content']
print("=== Test 2 ===")
print(f"Total tokens: {bundle.metadata['token_estimate']} / {(1000-256)}")
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"Has doc:: {'doc:' in sys_content}")
print()
print("System content:")
print(sys_content)
