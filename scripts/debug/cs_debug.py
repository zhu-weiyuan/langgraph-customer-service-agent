from agent.context_assembler import ContextAssembler, TokenBudgetAllocator, ContextPiece
import re

def est(s):
    c = len(re.findall(r'[一-鿿]', s))
    e = len(re.findall(r'[a-zA-Z]+', s))
    o = max(0, len(s) - c - e)
    return int(c*1.5 + e*1.3 + o*0.5), c, e

# ============================================================
# TEST 2: test_budget_truncation_behavior
# Budget=1000-256=744 tokens. Need 'task_goal' AND 'doc:' in output
# ============================================================
print("=== TEST 2 ===")
alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
asem = ContextAssembler(allocator=alloc)
long_text = "word " * 400  # ~600 tokens each

state = {
    'task_goal': long_text,
    'memory_summary': long_text,
    'rag_results': [{'title': 'doc', 'content': long_text}],
    'messages': [{'role':'user','content':long_text},{'role':'assistant','content':long_text}] * 5
}

bundle = asem.assemble(state, 'query', '')
sys_content = bundle.messages[0]['content']
print("Has task_goal:", "task_goal" in sys_content)
print("Has doc::", "doc:" in sys_content)

tg_pos = sys_content.find('task_goal')
dpos = sys_content.find('doc:')
ddot = sys_content.find('doc ')
print(f"task_goal at: {tg_pos}, 'doc:' at: {dpos}, 'doc ' at: {ddot}")

# ============================================================  
# TEST 3: test_priority_ordering_correctness
# Budget=500-128=372. Max 5 history messages. Need emotional msgs NOT replaced by filler
# ============================================================
print()
print("=== TEST 3 ===")
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

print("Has unacceptable:", "unacceptable" in sys3)
print("Has money back:", "money back" in sys3)
print("Has filler:", "filler" in sys3)

hist_msgs = [m for m in bundle3.messages if m['role'] in ('user', 'assistant')]
print(f"History messages: {len(hist_msgs)}")
for i, m in enumerate(hist_msgs):
    print(f"  [{i}] {m['role']}: {repr(m['content'][:50])}")

# ============================================================
# TEST 4: test_rag_score_weighting
# High-scoring relevant RAG should dominate. 'Official solution' must be present
# ============================================================
print()
print("=== TEST 4 ===")
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
print("Has Official solution:", "Official solution" in sys4)
print("Has Step-by-step guide:", "Step-by-step guide" in sys4)
print("Has Unverified user tip:", "Unverified user tip" in sys4)

# Print what's actually there
print()
print("Full system prompt:")
print(sys4)
