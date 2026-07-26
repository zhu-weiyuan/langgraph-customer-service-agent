import sys
sys.path.insert(0, '.')

# Patch _add_piece to see exact budget flow
from agent.context_assembler import TokenBudgetAllocator, ContextPiece, ContextAssembler

orig_add = TokenBudgetAllocator._add_piece
def debug_add(self, piece, available_tokens, preserve_head=True):
    result = orig_add(self, piece, available_tokens, preserve_head)
    print(f"  _add_piece [{piece.label}] avail={available_tokens} head={preserve_head} -> {result[0].content[:30]!r if result[0] else None} cost={result[1]}")
    return result
TokenBudgetAllocator._add_piece = debug_add

alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem = ContextAssembler(allocator=alloc)

long_text = "word " * 400
state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [{'role':'user','content':long_text},{'role':'assistant','content':long_text}] * 5
}

print("=== assemble() allocation trace ===")
bundle = asem.assemble(state, 'query', '')

print(f"\nTotal tokens: {bundle.metadata['token_estimate']}/{alloc.full_budget}")
sys_content = bundle.messages[0]['content']
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"Has doc:: {'doc:' in sys_content}")
