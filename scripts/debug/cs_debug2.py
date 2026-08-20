from agent.context_assembler import ContextAssembler, TokenBudgetAllocator

# Test 3: check history handling
alloc = TokenBudgetAllocator(context_window=500, reserved_output=128)
asem = ContextAssembler(allocator=alloc)
filler = "filler " * 100
msgs = [{"role":"user","content":"This is unacceptable!"},{"role":"assistant","content":"Sorry for the delay."}] + [{"role":"user","content":filler}] * 20 + [{"role":"assistant","content":filler}] * 20
state3 = {"task_goal":"URGENT: Handle refund request immediately","constraints":[],"memory_summary":"User is angry about late delivery","rag_results":[{"title":"Policy","content":"Refund process takes 5-7 days","relevant":True}],"messages":msgs}
bundle3 = asem.assemble(state3, "I want my money back now!", "")

print("=== Test 3 Debug ===")
sys_content = bundle3.messages[0]["content"]
print(f"Has 'unacceptable' in sys: {'unacceptable' in sys_content}")
print(f"Has 'money back' in sys: {'money back' in sys_content}")
print(f"System content:\n{sys_content}")
print()

all_msgs = bundle3.messages
for i, m in enumerate(all_msgs):
    print(f"  msg[{i}] role={m['role']} len={len(m['content'])} start={repr(m['content'][:40])}")

# Check test assertions
user_msgs_in_output = [m for m in all_msgs if m["role"] == "user" and m.get("content") != "I want my money back now!"]
print(f"\nUser msgs other than query: {len(user_msgs_in_output)}")
for m in user_msgs_in_output:
    print(f"  '{m['content'][:40]}'")
