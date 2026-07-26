"""Direct test of current implementation."""
import sys; sys.path.insert(0, '.')
from agent.context_assembler import ContextAssembler, TokenBudgetAllocator

# TEST 2: budget truncation - task_goal and doc: should appear in output
print("=== TEST 2 ===")
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

print(f"Token budget: {bundle.metadata['token_estimate']} / {744}")
sys_content = bundle.messages[0]['content']
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"First 10 chars of system: {repr(sys_content[:10])}")

# Let me check what RAG actually contains
rag_found = [p for p in [] if False]  # skip this
print("\nAll selected pieces:")
# Need to add debug to allocator
" 2>&1 | ForEach-Object { $_ }