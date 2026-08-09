import sys
sys.path.insert(0, '.')

from agent.context_assembler import TokenBudgetAllocator, ContextPiece, ContextAssembler

# Patch to trace allocation decisions
orig_add = TokenBudgetAllocator._add_piece
def traced_add(self, piece, available_tokens, preserve_head=True):
    est_val = piece.token_estimate or self._estimate_item_tokens(piece.content)
    result = orig_add(self, piece, available_tokens, preserve_head)
    print(f"  _add [{piece.label}] avail={available_tokens} head={preserve_head} est={est_val} -> {'added' if result[0] else 'None'} cost={result[1]}")
    return result
TokenBudgetAllocator._add_piece = traced_add

orig_alloc = TokenBudgetAllocator.allocate_pieces
def traced_alloc(self, pieces):
    print(f"=== allocate_pieces (full={self.full_budget}) ===")
    print(f"  Pieces: {[(p.label, p.priority, len(p.content)) for p in pieces]}")
    result = orig_alloc(self, pieces)
    total = sum(p.token_estimate for p in result)
    print(f"  Selected {len(result)} pieces, total={total}/{self.full_budget}")
    for p in result:
        print(f"    [{p.label}] pri={p.priority} cost={p.token_estimate} start={repr(p.content[:50])}")
    return result
TokenBudgetAllocator.allocate_pieces = traced_alloc

# Test 4 state
state4 = {
    'task_goal': 'Answer question accurately',
    'constraints': [],
    'memory_summary': '',
    'rag_results': [
        {'title': 'Manual', 'content': 'Step-by-step guide', 'relevant': True, 'score': 0.95},
        {'title': 'Forum', 'content': 'Unverified user tip', 'relevant': False, 'score': 0.4},
        {'title': 'KB', 'content': 'Official solution', 'relevant': True, 'score': 0.98}
    ],
    'messages': [
        {'role': 'user', 'content': 'How to reset?'},
        {'role': 'assistant', 'content': 'Try power cycling.'}
    ]
}

alloc4 = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem4 = ContextAssembler(allocator=alloc4)

bundle4 = asem4.assemble(state4, "Reset not working", '')
sys4 = bundle4.messages[0]['content']

print(f"\n=== RESULT ===")
print(f"Has Official solution: {'Official solution' in sys4}")
print(f"Has Step-by-step guide: {'Step-by-step guide' in sys4}")
print(f"Has Unverified user tip: {'Unverified user tip' in sys4}")
print(f"\nSystem content:\n{sys4}")
