import sys
sys.path.insert(0, '.')

from agent.context_assembler import TokenBudgetAllocator, ContextPiece, ContextAssembler

alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem = ContextAssembler(allocator=alloc)

long_text = "word " * 400
state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [{'role':'user','content':long_text},{'role':'assistant','content':long_text}] * 5
}

# Manually trace assemble() to see what pieces are built
prompt = asem.registry.get("system")
pieces = [ContextPiece("system", prompt.content, 100)]

goal = state.get("task_goal")
constraints = state.get("constraints") or []
if goal or (constraints and constraints != []):
    parts = ["Goal: " + str(goal or "")]
    if constraints and constraints != []:
        constraint_str = "; ".join(map(str, constraints))
        if constraint_str:
            parts.append("Constraints: " + constraint_str)
    pieces.append(ContextPiece("task", "\n".join(parts), 90))

# Check what task piece content looks like
task_piece = pieces[1]
print(f"Task piece content start: {repr(task_piece.content[:60])}")
print(f"Has 'Goal: ': {'Goal: ' in task_piece.content}")
print(f"Has 'task_goal:': {'task_goal:' in task_piece.content}")

# Check memory piece
memory = state.get("memory_summary") or state.get("memory")
pieces.append(ContextPiece("memory", str(memory), 60))
mem_piece = pieces[2]
print(f"\nMemory piece content start: {repr(mem_piece.content[:60])}")

# RAG piece
rag_results = state.get("rag_results") or []
if isinstance(rag_results, dict):
    rag_results = [rag_results]
relevant_rag = [r for r in rag_results if r.get("relevant", False)]
for idx, item in enumerate(relevant_rag[:4]):
    if isinstance(item, dict):
        title = item.get("title", "evidence")
        content_val = item.get("content", "") or title
        pieces.append(ContextPiece(
            "rag",
            f"{title}: {content_val}",
            70 + int(item.get("score", 0.5) * 10),
            recency=4 - idx,
        ))

rag_piece = pieces[3]
print(f"\nRAG piece content start: {repr(rag_piece.content[:60])}")

# Now run allocate_pieces to see what gets selected
selected = alloc.allocate_pieces(pieces)
print(f"\n=== Selected pieces ({len(selected)} total) ===")
for p in selected:
    print(f"  [{p.label}] tokens={p.token_estimate} start={repr(p.content[:50])}")

# Now check what assemble() produces for system_parts
print(f"\n=== Building system_parts ===")
system_parts = [prompt.content]

for piece in selected:
    if piece.label == "task":
        raw_content = piece.content
        print(f"Task content: {repr(raw_content[:60])}")
        print(f"  Has 'Goal: ': {'Goal: ' in raw_content}")
        print(f"  Has 'task_goal:': {'task_goal:' in raw_content}")
        if "Goal: " in raw_content or "task_goal:" in raw_content:
            system_parts.append(f"task_goal:{raw_content.strip()}")

for piece in selected:
    if piece.label == "memory" and piece.content.strip():
        print(f"Memory content: {repr(piece.content[:60])}")
        system_parts.append(f"Memory Context: {piece.content}")

for piece in selected:
    if piece.label == "rag" and piece.content.strip():
        print(f"RAG content: {repr(piece.content[:60])}")
        system_parts.append(piece.content)

system_content = "\n\n".join(system_parts)
print(f"\n=== Final system_content ===")
print(system_content[:500])
print()
print(f"Has task_goal: {'task_goal' in system_content}")
print(f"Has doc:: {'doc:' in system_content}")
