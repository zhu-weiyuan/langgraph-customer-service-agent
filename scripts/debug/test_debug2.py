"""Direct smoke test for the current context assembler implementation."""

from agent.context_assembler import ContextAssembler, TokenBudgetAllocator


def main() -> None:
    alloc = TokenBudgetAllocator(context_window=1000, reserved_output=256)
    assembler = ContextAssembler(allocator=alloc)
    long_text = "word " * 400
    state = {
        "task_goal": long_text,
        "memory_summary": long_text,
        "rag_results": [{"title": "doc", "content": long_text}],
        "messages": [
            {"role": "user", "content": long_text},
            {"role": "assistant", "content": long_text},
        ] * 5,
    }
    bundle = assembler.assemble(state, "query", "")
    print(f"Token budget: {bundle.metadata['token_estimate']} / 744")
    system_content = bundle.messages[0]["content"]
    print(f"Has task_goal: {'task_goal' in system_content}")
    print(f"First 10 chars of system: {system_content[:10]!r}")


if __name__ == "__main__":
    main()
