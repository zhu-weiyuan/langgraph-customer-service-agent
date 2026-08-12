<<<<<<< HEAD
# -*- coding: utf-8 -*-
"""
LangGraph Customer Service Agent - Entry Point

Modes:
    python main.py              # Interactive mode
    python main.py --test       # Auto-test (full flow demo)
    python main.py --resume     # Session resume demo

Features:
    - Intent identification (consult/complaint/chat)
    - Multi-turn conversation with context
    - Satisfaction check with retry (max 3 times)
    - Escalation to human via interrupt
    - Session persistence and resume via SQLite checkpointer
"""

import sys
import io
import json
from uuid import uuid4

# Windows UTF-8 compatibility
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import build_graph


def run_interactive(graph):
    """Interactive mode - free conversation with the agent."""
    print()
    print("=" * 60)
    print("Customer Service Agent - Interactive Mode")
    print("=" * 60)
    print("Type 'quit' or 'exit' to leave")
    print("Type 'new' to start a new session")
    print("Type 'resume <session_id>' to restore a session")
    print("=" * 60)
    print()

    session_id = None

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ['quit', 'exit']:
            print("Bye!")
            break

        if user_input.lower() == 'new':
            session_id = str(uuid4())
            print(f"\nNew session created (ID: {session_id})")
            continue

        if user_input.lower().startswith('resume'):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                session_id = parts[1].strip()
                print(f"\nSwitched to session (ID: {session_id})")
            continue

        # Auto-create session if none exists
        if not session_id:
            session_id = str(uuid4())
            print(f"Auto-created new session (ID: {session_id})\n")

        config = {"configurable": {"thread_id": session_id}}

        # Check current state for interrupted sessions
        current_state = graph.get_state(config)

        human_message = HumanMessage(content=user_input)

        try:
            if current_state and current_state.next:
                # Resume interrupted session
                print("\nResuming interrupted session...")
                input_data = {"messages": [human_message]}

                for _ in graph.stream(input_data, config=config, stream_mode="values"):
                    pass

                final_state = graph.get_state(config)
            else:
                # Normal continuation
                input_data = {
                    "messages": [human_message],
                    "session_id": session_id,
                    "retry_count": 0,
                    "escalate": False
                }

                for _ in graph.stream(input_data, config=config, stream_mode="values"):
                    pass

                final_state = graph.get_state(config)

            # Output bot replies
            if final_state and final_state.values:
                messages = final_state.values.get('messages', [])
                for msg in messages:
                    if isinstance(msg, AIMessage):
                        print(f"\nAgent: {msg.content}")

                # Status summary
                intent = final_state.values.get('intent')
                satisfaction = final_state.values.get('satisfaction')
                retry_count = final_state.values.get('retry_count', 0)

                print(f"\n[Status] Intent: {intent} | Satisfaction: {satisfaction} | Retries: {retry_count}")

        except Exception as e:
            if "interrupt" in str(e).lower() or "Interrupt" in str(e):
                print("\nSession suspended, waiting for human intervention")
                print(f"Use 'resume {session_id}' to restore this session")
            else:
                print(f"\nError: {e}")

        print("-" * 40)


def run_test(graph):
    """Auto-test mode - demonstrates the full customer service flow."""
    print()
    print("=" * 60)
    print("Customer Service Agent - Auto Test")
    print("=" * 60)

    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    # --- Scenario 1: User consultation ---
    print()
    print("-" * 60)
    print("Scenario 1: User consultation")
    print("-" * 60)

    input_data = {
        "messages": [HumanMessage(content="How to use the product?")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    print("User: How to use the product?")

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 1 complete")
        print(f"  Intent: {final_state.values.get('intent')}")
        print(f"  Retry count: {final_state.values.get('retry_count', 0)}")

    except Exception as e:
        print(f"Scenario 1 error: {e}")

    # --- Scenario 2: User not satisfied -> retry ---
    print()
    print("-" * 60)
    print("Scenario 2: User not satisfied, trigger retry")
    print("-" * 60)

    input_data = {
        "messages": [HumanMessage(content="Not satisfied")],
        "session_id": session_id
    }

    print("User: Not satisfied")

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 2 complete")
        print(f"  Satisfaction: {final_state.values.get('satisfaction')}")
        print(f"  Retry count: {final_state.values.get('retry_count', 0)}")

    except Exception as e:
        print(f"Scenario 2 error: {e}")

    # --- Scenario 3: Still not satisfied -> escalate ---
    print()
    print("-" * 60)
    print("Scenario 3: Still not satisfied, escalate to human (interrupt)")
    print("-" * 60)

    input_data = {
        "messages": [HumanMessage(content="Still very unsatisfied, want to complain")],
        "session_id": session_id
    }

    print("User: Still very unsatisfied, want to complain")

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 3 complete")
        print(f"  Escalated: {final_state.values.get('escalate')}")

    except Exception as e:
        if "interrupt" in str(e).lower() or "Interrupt" in str(e):
            print("\nSession suspended! Waiting for human intervention")
            print(f"  Session ID: {session_id}")
            print(f"  Use resume_session() to restore")
        else:
            print(f"Scenario 3 error: {e}")

    # --- Scenario 4: Resume suspended session ---
    print()
    print("-" * 60)
    print("Scenario 4: Resume suspended session, human handling")
    print("-" * 60)

    print(f"Restoring session (ID: {session_id})...")

    try:
        current_state = graph.get_state(config)
        print(f"  Current node: {current_state.next}")

        # Simulate human customer service reply
        human_reply = ("Hello, I'm human agent Xiao Wang. "
                       "I understand your issue and will arrange a technician "
                       "to contact you within 24 hours. Anything else?")

        input_data = {
            "messages": [HumanMessage(content="OK, that's fine")],
            "session_id": session_id
        }

        print(f"Human agent: {human_reply}")
        print("User: OK, that's fine")

        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 4 complete - Session resolved")

    except Exception as e:
        print(f"Scenario 4 error: {e}")

    # --- Summary ---
    print()
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"All scenarios completed")
    print(f"  Session ID: {session_id}")
    print(f"  Checkpoints saved to checkpoints.db")
    print(f"  Use 'resume {session_id}' to restore this session")
    print("=" * 60)
    print()


def run_resume_demo(graph):
    """Session resume demo - shows how checkpointer enables state recovery."""
    print()
    print("=" * 60)
    print("Customer Service Agent - Session Resume Demo")
    print("=" * 60)

    # Create new session and run one turn
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    print(f"\nCreating new session (ID: {session_id})")

    input_data = {
        "messages": [HumanMessage(content="Hello, I want to know about product features")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    print("User: Hello, I want to know about product features")

    for _ in graph.stream(input_data, config=config, stream_mode="values"):
        pass

    # Save current state
    saved_state = graph.get_state(config)
    print(f"\nSession state saved")
    print(f"  Message count: {len(saved_state.values.get('messages', []))}")
    print(f"  Intent: {saved_state.values.get('intent')}")

    # Simulate "restart" and resume session
    print("\nSimulating system restart, restoring session...")

    from agent.graph import build_graph as rebuild_graph
    graph_restored = rebuild_graph("checkpoints.db")

    # Restore session
    restored_state = graph_restored.get_state(config)
    print(f"\nSession restored")
    print(f"  Message count: {len(restored_state.values.get('messages', []))}")
    print(f"  Intent: {restored_state.values.get('intent')}")

    # Continue conversation
    print("\nUser (continuing): How exactly do I use it?")

    input_data = {
        "messages": [HumanMessage(content="How exactly do I use it?")]
    }

    for _ in graph_restored.stream(input_data, config=config, stream_mode="values"):
        pass

    print("\nSession resume demo complete!")
    print("=" * 60)
    print()


def main():
    """Main entry point."""
    # Build the graph with SQLite persistence
    graph = build_graph("checkpoints.db")

    # Select mode based on command line args
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == '--test':
            run_test(graph)
        elif mode == '--resume':
            run_resume_demo(graph)
        else:
            print(f"Unknown mode: {mode}")
            print("Available modes: --test, --resume")
            sys.exit(1)
    else:
        # Default: interactive mode
        run_interactive(graph)


if __name__ == "__main__":
    main()
=======
# -*- coding: utf-8 -*-
"""
LangGraph Customer Service Agent - Entry Point

Modes:
    python main.py              # Interactive mode
    python main.py --test       # Auto-test (full flow demo)
    python main.py --resume     # Session resume demo

Features:
    - Intent identification (consult/complaint/chat)
    - Multi-turn conversation with context
    - Satisfaction check with retry (max 3 times)
    - Escalation to human via interrupt
    - Session persistence and resume via SQLite checkpointer
"""

import sys
import io
import json
from uuid import uuid4

# Windows UTF-8 compatibility
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import build_graph


def run_interactive(graph):
    """Interactive mode - free conversation with the agent."""
    print()
    print("=" * 60)
    print("Customer Service Agent - Interactive Mode")
    print("=" * 60)
    print("Type 'quit' or 'exit' to leave")
    print("Type 'new' to start a new session")
    print("Type 'resume <session_id>' to restore a session")
    print("=" * 60)
    print()

    session_id = None

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ['quit', 'exit']:
            print("Bye!")
            break

        if user_input.lower() == 'new':
            session_id = str(uuid4())
            print(f"\nNew session created (ID: {session_id})")
            continue

        if user_input.lower().startswith('resume'):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                session_id = parts[1].strip()
                print(f"\nSwitched to session (ID: {session_id})")
            continue

        # Auto-create session if none exists
        if not session_id:
            session_id = str(uuid4())
            print(f"Auto-created new session (ID: {session_id})\n")

        config = {"configurable": {"thread_id": session_id}}

        # Check current state for interrupted sessions
        current_state = graph.get_state(config)

        human_message = HumanMessage(content=user_input)

        try:
            if current_state and current_state.next:
                # Resume interrupted session
                print("\nResuming interrupted session...")
                input_data = {"messages": [human_message]}

                for _ in graph.stream(input_data, config=config, stream_mode="values"):
                    pass

                final_state = graph.get_state(config)
            else:
                # Normal continuation
                input_data = {
                    "messages": [human_message],
                    "session_id": session_id,
                    "retry_count": 0,
                    "escalate": False
                }

                for _ in graph.stream(input_data, config=config, stream_mode="values"):
                    pass

                final_state = graph.get_state(config)

            # Output bot replies
            if final_state and final_state.values:
                messages = final_state.values.get('messages', [])
                for msg in messages:
                    if isinstance(msg, AIMessage):
                        print(f"\nAgent: {msg.content}")

                # Status summary
                intent = final_state.values.get('intent')
                satisfaction = final_state.values.get('satisfaction')
                retry_count = final_state.values.get('retry_count', 0)

                print(f"\n[Status] Intent: {intent} | Satisfaction: {satisfaction} | Retries: {retry_count}")

        except Exception as e:
            if "interrupt" in str(e).lower() or "Interrupt" in str(e):
                print("\nSession suspended, waiting for human intervention")
                print(f"Use 'resume {session_id}' to restore this session")
            else:
                print(f"\nError: {e}")

        print("-" * 40)


def run_test(graph):
    """Auto-test mode - demonstrates the full customer service flow."""
    print()
    print("=" * 60)
    print("Customer Service Agent - Auto Test")
    print("=" * 60)

    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    # --- Scenario 1: User consultation ---
    print()
    print("-" * 60)
    print("Scenario 1: User consultation")
    print("-" * 60)

    input_data = {
        "messages": [HumanMessage(content="How to use the product?")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    print("User: How to use the product?")

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 1 complete")
        print(f"  Intent: {final_state.values.get('intent')}")
        print(f"  Retry count: {final_state.values.get('retry_count', 0)}")

    except Exception as e:
        print(f"Scenario 1 error: {e}")

    # --- Scenario 2: User not satisfied -> retry ---
    print()
    print("-" * 60)
    print("Scenario 2: User not satisfied, trigger retry")
    print("-" * 60)

    input_data = {
        "messages": [HumanMessage(content="Not satisfied")],
        "session_id": session_id
    }

    print("User: Not satisfied")

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 2 complete")
        print(f"  Satisfaction: {final_state.values.get('satisfaction')}")
        print(f"  Retry count: {final_state.values.get('retry_count', 0)}")

    except Exception as e:
        print(f"Scenario 2 error: {e}")

    # --- Scenario 3: Still not satisfied -> escalate ---
    print()
    print("-" * 60)
    print("Scenario 3: Still not satisfied, escalate to human (interrupt)")
    print("-" * 60)

    input_data = {
        "messages": [HumanMessage(content="Still very unsatisfied, want to complain")],
        "session_id": session_id
    }

    print("User: Still very unsatisfied, want to complain")

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 3 complete")
        print(f"  Escalated: {final_state.values.get('escalate')}")

    except Exception as e:
        if "interrupt" in str(e).lower() or "Interrupt" in str(e):
            print("\nSession suspended! Waiting for human intervention")
            print(f"  Session ID: {session_id}")
            print(f"  Use resume_session() to restore")
        else:
            print(f"Scenario 3 error: {e}")

    # --- Scenario 4: Resume suspended session ---
    print()
    print("-" * 60)
    print("Scenario 4: Resume suspended session, human handling")
    print("-" * 60)

    print(f"Restoring session (ID: {session_id})...")

    try:
        current_state = graph.get_state(config)
        print(f"  Current node: {current_state.next}")

        # Simulate human customer service reply
        human_reply = ("Hello, I'm human agent Xiao Wang. "
                       "I understand your issue and will arrange a technician "
                       "to contact you within 24 hours. Anything else?")

        input_data = {
            "messages": [HumanMessage(content="OK, that's fine")],
            "session_id": session_id
        }

        print(f"Human agent: {human_reply}")
        print("User: OK, that's fine")

        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass

        final_state = graph.get_state(config)
        print(f"\nScenario 4 complete - Session resolved")

    except Exception as e:
        print(f"Scenario 4 error: {e}")

    # --- Summary ---
    print()
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"All scenarios completed")
    print(f"  Session ID: {session_id}")
    print(f"  Checkpoints saved to checkpoints.db")
    print(f"  Use 'resume {session_id}' to restore this session")
    print("=" * 60)
    print()


def run_resume_demo(graph):
    """Session resume demo - shows how checkpointer enables state recovery."""
    print()
    print("=" * 60)
    print("Customer Service Agent - Session Resume Demo")
    print("=" * 60)

    # Create new session and run one turn
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    print(f"\nCreating new session (ID: {session_id})")

    input_data = {
        "messages": [HumanMessage(content="Hello, I want to know about product features")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    print("User: Hello, I want to know about product features")

    for _ in graph.stream(input_data, config=config, stream_mode="values"):
        pass

    # Save current state
    saved_state = graph.get_state(config)
    print(f"\nSession state saved")
    print(f"  Message count: {len(saved_state.values.get('messages', []))}")
    print(f"  Intent: {saved_state.values.get('intent')}")

    # Simulate "restart" and resume session
    print("\nSimulating system restart, restoring session...")

    from agent.graph import build_graph as rebuild_graph
    graph_restored = rebuild_graph("checkpoints.db")

    # Restore session
    restored_state = graph_restored.get_state(config)
    print(f"\nSession restored")
    print(f"  Message count: {len(restored_state.values.get('messages', []))}")
    print(f"  Intent: {restored_state.values.get('intent')}")

    # Continue conversation
    print("\nUser (continuing): How exactly do I use it?")

    input_data = {
        "messages": [HumanMessage(content="How exactly do I use it?")]
    }

    for _ in graph_restored.stream(input_data, config=config, stream_mode="values"):
        pass

    print("\nSession resume demo complete!")
    print("=" * 60)
    print()


def main():
    """Main entry point."""
    # Build the graph with SQLite persistence
    graph = build_graph("checkpoints.db")

    # Select mode based on command line args
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == '--test':
            run_test(graph)
        elif mode == '--resume':
            run_resume_demo(graph)
        else:
            print(f"Unknown mode: {mode}")
            print("Available modes: --test, --resume")
            sys.exit(1)
    else:
        # Default: interactive mode
        run_interactive(graph)


if __name__ == "__main__":
    main()
>>>>>>> origin/master
