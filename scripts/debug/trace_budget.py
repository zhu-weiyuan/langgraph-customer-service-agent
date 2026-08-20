import sys
sys.path.insert(0, '.')
from agent.context_assembler import TokenBudgetAllocator, ContextPiece

alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
full = alloc.full_budget  # 744
print(f"Full budget: {full}")

# Build pieces like assemble() does
long_text = "word " * 400

system_content = "You are a helpful customer service assistant."
system_piece = ContextPiece("system", system_content, 100)
est_system = alloc._estimate_item_tokens(system_content)
print(f"System tokens: {est_system}")

task_content = "Goal: " + long_text
task_piece = ContextPiece("task", task_content, 90)
est_task = alloc._estimate_item_tokens(task_content)
print(f"Task raw est tokens: {est_task}, char len: {len(task_content)}")

# Phase 2: task budget cap
task_budget = max(15, int(full * 0.18))  # max(15, 133) = 133
print(f"Task budget cap: {task_budget}")

added_task, cost_task = alloc._add_piece(task_piece, task_budget)
print(f"Task added: yes, tokens={cost_task}, starts with: {repr(added_task.content[:50]) if added_task else 'None'}")

# Phase 3: RAG budget
rag_content = "doc: " + long_text
rag_piece = ContextPiece("rag", rag_content, 78, recency=3)
est_rag = alloc._estimate_item_tokens(rag_content)
print(f"RAG raw est tokens: {est_rag}")

# After system+task, avail = full - cost_task (since system is small)
avail_after_system_and_task = full - 28 - cost_task
print(f"Available for RAG: {avail_after_system_and_task}")

rag_budget_limit = max(20, int(full * 0.30))
print(f"RAG budget limit: {rag_budget_limit} (max(20, int(744*0.30)))")

# RAG add: min(avail, max(15, int((avail) * 0.95)))
rag_add_cap = min(avail_after_system_and_task, max(15, int((avail_after_system_and_task) * 0.95)))
print(f"RAG actual add cap: {rag_add_cap}")

added_rag, cost_rag = alloc._add_piece(rag_piece, rag_add_cap, preserve_head=True)
print(f"RAG added: yes, tokens={cost_rag}, starts with: {repr(added_rag.content[:50]) if added_rag else 'None'}")

# Memory after RAG
avail_after_rag = avail_after_system_and_task - cost_rag
mem_content = long_text  # no "Goal:" prefix since it comes from state['memory_summary']
est_mem = alloc._estimate_item_tokens(mem_content)
print(f"Memory raw est tokens: {est_mem}")

mem_budget_cap = max(15, int(full * 0.12))  # max(15, 89) = 89
print(f"Memory budget cap: {mem_budget_cap}")

added_mem, cost_mem = alloc._add_piece(ContextPiece("memory", mem_content, 60), min(avail_after_rag, mem_budget_cap))
print(f"Memory added: yes, tokens={cost_mem}, starts with: {repr(added_mem.content[:50]) if added_mem else 'None'}")

# Total
total_used = cost_task + est_system + (cost_rag if cost_rag else 0) + (cost_mem if cost_mem else 0)
print(f"\nTotal used: {total_used} / {full}")
