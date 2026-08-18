"""Quick test runner for the 3 failing scenarios."""
import sys; sys.path.insert(0, '.')
from agent.context_assembler import ContextAssembler, TokenBudgetAllocator

# TEST 2: budget truncation
print("=== TEST 2: Budget Truncation ===")
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
sys_content = bundle.messages[0]['content']
print(f"Has task_goal: {'task_goal' in sys_content}")
print(f"Has doc:: {'doc:' in sys_content}")
print(f"Total tokens: {bundle.metadata['token_estimate']} / {1000-256}")

# TEST 3: priority ordering with emotional messages
print("\n=== TEST 3: Priority Ordering ===")
filler = "filler " * 100
msgs = [
    {'role': 'user', 'content': 'This is unacceptable!'},
    {'role': 'assistant', 'content': 'Sorry for the delay.'},
] + [{'role': 'user', 'content': filler}] * 20 + [{'role': 'assistant', 'content': filler}] * 20

state3 = {
    'task_goal': 'URGENT: Handle refund request immediately',
    'constraints': [],
    'memory_summary': 'User is angry about late delivery',
    'rag_results': [{'title': 'Policy', 'content': 'Refund process takes 5-7 days', 'relevant': True}],
    'messages': msgs,
}

alloc3 = TokenBudgetAllocator(context_window=500, reserved_output=128)
asem3 = ContextAssembler(allocator=alloc3)
bundle3 = asem3.assemble(state3, "I want my money back now!", '')
sys3 = bundle3.messages[0]['content']

print(f"Has unacceptable: {'unacceptable' in sys3}")
print(f"Has money back: {'money back' in sys3}")
print(f"Sys content:\n{sys3}")
print()
print("All messages:")
for i, m in enumerate(bundle3.messages):
    print(f"  [{i}] {m['role']}: {repr(m['content'][:60])}")

# TEST 4: RAG score weighting
print("\n=== TEST 4: RAG Score Weighting ===")
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

print(f"Has Official solution: {'Official solution' in sys4}")
print(f"Has Step-by-step guide: {'Step-by-step guide' in sys4}")
print(f"Has Unverified user tip: {'Unverified user tip' in sys4}")
print(f"Source counts: {bundle4.metadata['source_counts']}")
print(f"Sys content:\n{sys4}")
