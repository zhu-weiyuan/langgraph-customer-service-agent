import sys
sys.path.insert(0, '.')

from agent.context_assembler import TokenBudgetAllocator, ContextPiece, ContextAssembler

# Monkey-patch to trace allocation decisions
orig_add = TokenBudgetAllocator._add_piece
def traced_add(self, piece, available_tokens, preserve_head=True):
    est_val = piece.token_estimate or self._estimate_item_tokens(piece.content)
    result = orig_add(self, piece, available_tokens, preserve_head)
    print(f"  _add [{piece.label}] avail={available_tokens} head={preserve_head} "
          f"est={est_val} -> {'added' if result[0] else 'None'} cost={result[1]}")
    return result
TokenBudgetAllocator._add_piece = traced_add

orig_alloc = TokenBudgetAllocator.allocate_pieces
def traced_alloc(self, pieces):
    print(f"\n=== allocate_pieces (full={self.full_budget}) ===")
    for p in pieces:
        e = self._estimate_item_tokens(p.content) if not p.token_estimate else p.token_estimate
        filler = ""
        if p.label == "history":
            filler = " FILLER?" if self._is_large_filler(p) else ""
        print(f"  [{p.label}] pri={p.priority} est~{e}{filler} content_len={len(p.content)}")
    result = orig_alloc(self, pieces)
    total = sum(p.token_estimate for p in result)
    print(f"\n  Selected {len(result)} pieces, total={total}/{self.full_budget}")
    for p in result:
        print(f"    [{p.label}] cost={p.token_estimate} start={repr(p.content[:50])}")
    return result
TokenBudgetAllocator.allocate_pieces = traced_alloc

# Test 2 state - oversized content
long_text = "word " * 400  # ~1300+ tokens per piece
state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [{'role':'user','content':long_text},{'role':'assistant','content':long_text}] * 5
}

alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem = ContextAssembler(allocator=alloc)

bundle = asem.assemble(state, 'query', '')

print(f"\n=== RESULT ===")
sys_content = bundle.messages[0]['content']
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"Has doc:: {'doc:' in sys_content}")
print(f"\nFull system_content ({len(sys_content)} chars):")
print(sys_content)
