import sys
sys.path.insert(0, '.')

from agent.context_assembler import TokenBudgetAllocator, ContextPiece, ContextAssembler

# Patch _add_piece to trace what gets truncated
orig_add = TokenBudgetAllocator._add_piece
def traced_add(self, piece, available_tokens, preserve_head=True):
    est_val = piece.token_estimate or self._estimate_item_tokens(piece.content)
    
    # Let's see what happens inside
    if available_tokens >= est_val:
        result = orig_add(self, piece, available_tokens, preserve_head)
        print(f"  [FULL] [{piece.label}] avail={available_tokens} head={preserve_head} est={est_val} cost={result[1]}")
        return result
    
    # It's going to be truncated
    if preserve_head:
        target_chars = max(6, int(available_tokens / 0.7))
        truncated = piece.content[:target_chars]
    else:
        target_chars = max(6, int(available_tokens / 0.7))
        truncated = piece.content[len(piece.content) - target_chars:]
    
    t_est = self._estimate_item_tokens(truncated)
    
    print(f"  [TRUNCATE head={preserve_head}] [{piece.label}] avail={available_tokens} est={est_val}")
    print(f"    target_chars={target_chars}, content[:50]={repr(piece.content[:50])}")
    print(f"    truncated[:80]={repr(truncated[:80])}")
    
    result = orig_add(self, piece, available_tokens, preserve_head)
    return result

TokenBudgetAllocator._add_piece = traced_add

# Test 2: oversized content
long_text = "word " * 400
state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [{'role':'user','content':long_text},{'role':'assistant','content':long_text}] * 5
}

alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem = ContextAssembler(allocator=alloc)

bundle = asem.assemble(state, 'query', '')

sys_content = bundle.messages[0]['content']
print(f"\n=== RESULT ===")
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"Has doc:: {'doc:' in sys_content}")
print(f"Total tokens: {bundle.metadata['token_estimate']}")
