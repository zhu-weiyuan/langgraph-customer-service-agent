import sys
sys.path.insert(0, '.')

from agent.context_assembler import TokenBudgetAllocator, ContextPiece

# Direct test: simulate exactly what assemble() does for test 2
alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
full = alloc.full_budget  # 744

long_text = "word " * 400
print(f"long_text: {len(long_text)} chars")

# System piece
sys_content = "You are a helpful customer service assistant."
system_piece = ContextPiece("system", sys_content, 100)
est_sys = alloc._estimate_item_tokens(sys_content)
print(f"\n=== Phase 1: System ===")
print(f"  content='{sys_content}' ({len(sys_content)} chars)")
print(f"  tokens={est_sys}")

# Task piece
task_content = "Goal: " + long_text
task_piece = ContextPiece("task", task_content, 90)
est_task_raw = alloc._estimate_item_tokens(task_content)
print(f"\n=== Phase 2: Task ===")
print(f"  raw tokens={est_task_raw}")
print(f"  char count={len(task_content)}")

# Task budget cap
task_budget_cap = max(15, int(full * 0.18))
print(f"  task_budget_cap = max(15, int({full} * 0.18)) = {task_budget_cap}")

# What happens during truncation?
avail_after_system = full - est_sys
print(f"  avail after system: {avail_after_system}")

# _truncate_head logic
target_chars = max(6, int(task_budget_cap / 0.7))
truncated_task = task_content[:target_chars]
est_truncated = alloc._estimate_item_tokens(truncated_task)
print(f"  target_chars for truncation: {target_chars}")
print(f"  truncated content ({len(truncated_task)} chars): '{truncated_task[:60]}...'")
print(f"  estimated tokens of truncated: {est_truncated}")

# So what's used after task?
used_after_phase2 = est_sys + est_truncated
print(f"\n  Total used after Phases 1+2: {est_sys} + {est_truncated} = {used_after_phase2}")
avail_after_phase2 = full - used_after_phase2
print(f"  Available for RAG: {avail_after_phase2}")

# Now Phase 3: RAG
rag_content = "doc: " + long_text
rag_piece = ContextPiece("rag", rag_content, 82, recency=3)
est_rag_raw = alloc._estimate_item_tokens(rag_content)
print(f"\n=== Phase 3: RAG ===")
print(f"  raw tokens={est_rag_raw}")

# _add_piece logic for RAG
avail_for_rag_add = min(avail_after_phase2, max(15, int((avail_after_phase2) * 0.95)))
print(f"  add_piece available_tokens: min({avail_after_phase2}, max(15, int({avail_after_phase2} * 0.95))) = {avail_for_rag_add}")

# Truncation of RAG
rag_target_chars = max(6, int(avail_for_rag_add / 0.7))
rag_truncated = rag_content[:rag_target_chars]
rag_est = alloc._estimate_item_tokens(rag_truncated)
print(f"  target_chars for RAG truncation: {rag_target_chars}")
print(f"  truncated content ({len(rag_truncated)} chars): '{rag_truncated[:60]}...'")
print(f"  estimated tokens of truncated RAG: {rag_est}")

# After RAG
used_after_rag = used_after_phase2 + rag_est
avail_after_rag = full - used_after_rag
print(f"\n  Total used after Phases 1-3: {used_after_phase2} + {rag_est} = {used_after_rag}")
print(f"  Available for Memory: {avail_after_rag}")

# Phase 4: Memory budget cap
mem_budget_cap = max(15, int(full * 0.12))
print(f"  mem_budget_cap = max(15, int({full} * 0.12)) = {mem_budget_cap}")
mem_actual_cap = min(avail_after_rag, mem_budget_cap)
print(f"  actual mem cap: min({avail_after_rag}, {mem_budget_cap}) = {mem_actual_cap}")

# So the question is: does RAG make it? YES in theory. Let me check if _add_piece
# returns None due to some bug.

added_rag, cost_rag = alloc._add_piece(rag_piece, avail_for_rag_add, preserve_head=True)
print(f"\n  _add_piece returned: {'added' if added_rag else 'None'}, cost={cost_rag}")
if added_rag:
    print(f"  RAG piece content start: '{added_rag.content[:50]}'")

# Hmm wait - the issue might be that avail_for_rag_add is computed from avail_after_phase2,
# which was already used by the task. But I'm computing it from avail_after_system + cost_task...
# Let me check the actual code flow again.

print("\n=== Re-checking Phase 2 budget math ===")
# In _add_piece: if avail_tokens >= est (raw estimate), returns full piece
# est for task = est_truncated = ~106 tokens (after truncation)
# avail = task_budget_cap = 133
# But wait - _add_piece takes available_tokens=task_budget_cap=133
# and the raw estimate (before any internal truncation) is...

print(f"  Task raw estimate: {est_task_raw}")
print(f"  Task budget passed to _add_piece: {task_budget_cap}")
print(f"  Raw est ({est_task_raw}) > avail ({task_budget_cap}) → will truncate")

# But what does _estimate_item_tokens of the truncated piece actually return?
# That's what _add_piece returns as cost.
# Let me check directly:
added, cost = alloc._add_piece(task_piece, task_budget_cap)
print(f"  _add_piece(task, {task_budget_cap}) -> cost={cost}")

# Now Phase 3 actual flow:
# After system+task: avail = full - est_sys - cost = 744 - 28 - cost = ?
avail_after_phases12 = full - est_sys - cost
print(f"  avail after phases 1+2: {avail_after_phases12}")

rag_actual_avail = min(avail_after_phases12, max(15, int((avail_after_phases12) * 0.95)))
print(f"  RAG add_piece available_tokens: min({avail_after_phases12}, ...) = {rag_actual_avail}")

# Hmm, maybe the issue is that _add_piece doesn't properly compute cost after truncation?
# Or maybe est_rag > available_tokens but truncated piece has even smaller est...
# Let's just call it:
added_rag2, cost_rag2 = alloc._add_piece(rag_piece, rag_actual_avail)
print(f"  _add_piece(rag, {rag_actual_avail}) -> {'added' if added_rag2 else 'None'} cost={cost_rag2}")

# Check what assemble() ACTUALLY produces by running it directly
from agent.context_assembler import ContextAssembler

state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [{'role':'user','content':long_text},{'role':'assistant','content':long_text}] * 5
}

asem = ContextAssembler(allocator=alloc)
bundle = asem.assemble(state, 'query', '')
sys_content = bundle.messages[0]['content']
print(f"\n=== ACTUAL assemble() output ===")
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"Has doc:: {'doc:' in sys_content}")
# Find where 'doc' appears if anywhere
for i, char in enumerate(sys_content):
    if char == 'd' and i+3 < len(sys_content) and sys_content[i:i+4] == 'doc:':
        print(f"  'doc:' found at position {i}")
        break
# Also check what's after task_goal line
print(f"\nFirst 200 chars of system content:")
print(repr(sys_content[:200]))
