import sys
sys.path.insert(0, '.')

from agent.context_assembler import TokenBudgetAllocator, ContextPiece, ContextAssembler

# Monkey-patch to trace allocation
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
    print("=== allocate_pieces ===")
    print(f"  full budget: {self.full_budget}")
    print(f"  piece labels: {[p.label for p in pieces]}")
    result = orig_alloc(self, pieces)
    print(f"  selected: {len(result)} pieces:")
    total = 0
    for p in result:
        total += p.token_estimate
        print(f"    [{p.label}] cost={p.token_estimate} start={repr(p.content[:40])}")
    print(f"  TOTAL: {total}/{self.full_budget}")
    return result
TokenBudgetAllocator.allocate_pieces = traced_alloc

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

print("\n=== RESULT ===")
sys_content = bundle.messages[0]['content']
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"Has doc:: {'doc:' in sys_content}")
print(f"Total tokens: {bundle.metadata['token_estimate']}")
