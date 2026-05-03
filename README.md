# LangGraph Customer Service Agent

A customer service system built with LangGraph, supporting multi-turn dialogue, intent identification, satisfaction checking, and human escalation.

## Core Features

- **SSE Streaming**: Real-time token-by-token response streaming via Server-Sent Events — users see replies appear character by character instead of waiting for the full response
- **Sentiment Analysis**: Detects user emotion (angry/sad/anxious/happy) and adjusts bot tone accordingly
- **RAG Knowledge Base**: Local docs/FAQ retrieval with TF-IDF scoring — grounded answers from product manuals
- **Intent Identification**: Auto-classify user messages (consult / complaint / chat)
- **Multi-turn Dialogue**: Context-aware continuous conversation
- **Multi-turn Memory**: Cross-session user preferences, product interests, and history (SQLite)
- **Dialogue Summary**: Auto-generate service ticket summaries at session end
- **Satisfaction Check**: Retry on dissatisfaction, max 3 attempts
- **Human Escalation**: Use `interrupt` to suspend session for human handling
- **Session Persistence**: SQLite-based state checkpointing and recovery
- **Dark Mode UI**: Toggle between light/dark themes with localStorage persistence
- **Quick Reply Buttons**: Context-aware suggestion buttons after bot responses
- **Typing Animation**: Character-by-character output with adaptive speed (Chinese vs English)
- **Session History API**: REST endpoints for session state and memory stats

## Architecture

```
START -> identify_intent -> generate_reply (+RAG) -> check_satisfaction
                                                      |
                                                 process_satisfaction
                                                      |
                                      +---------------+--------------+
                                      |               |              |
                                 (satisfied)    (not satisfied)   (3 retries used)
                                      |               |              |
                                 finalize         retry        escalate_to_human
                                                   |                  |
                                              generate_reply       finalize
```

### RAG Pipeline

```
User Question → TF-IDF Retrieval → Top-3 Sections → Context Injection → LLM Reply
(knowledge/*.md)    (rag.py)        (scored)      (nodes.py)
```

## Project Structure

```
langgraph-customer-service-agent/
├── agent/
│   ├── __init__.py
│   ├── state.py          # State definition (TypedDict)
│   ├── nodes.py          # Node function implementations (+RAG + Sentiment)
│   ├── rag.py            # RAG retrieval (TF-IDF, no external deps)
│   ├── sentiment.py      # Emotion detection + tone adjustment
│   └── graph.py          # Graph construction and compilation
├── knowledge/            # Knowledge base (auto-loaded)
│   ├── product-manual.md # Product specs, features, pricing
│   ├── troubleshooting.md # Troubleshooting guides
│   └── faq.md            # Common Q&A
├── app.py                # Web UI (port 7860)
├── main.py               # Entry point (interactive / test / resume modes)
├── test_agent.py         # Automated test suite
├── requirements.txt      # Dependencies
└── README.md             # This file
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run tests

```bash
# Full automated test (demonstrates complete flow)
python main.py --test

# Session resume demo
python main.py --resume

# Independent test suite
python test_agent.py
```

### 3. Interactive mode

```bash
python main.py
```

## Key LangGraph Concepts

| Concept | Description |
|---------|-------------|
| **State** | TypedDict defining the graph's data schema; all nodes read/write via state |
| **Nodes** | Functions that receive state and return state updates |
| **Edges** | `add_edge()` for fixed transitions; `add_conditional_edges()` for dynamic routing |
| **Checkpointer** | `SqliteSaver` for persistence; `MemorySaver` for testing |
| **Interrupt** | Pause graph execution for human-in-the-loop workflows |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/api/chat` | POST | `{"message": "...", "session_id": "..."}` → bot response (JSON) |
| `/api/chat` | POST | `{"message": "...", "stream": true}` → SSE streaming tokens (real-time token-by-token output) |
| `/api/session/<id>` | GET | Get session state (messages, intent, emotion) |
| `/api/export/<id>` | GET | Export full session history as JSON (downloadable) |
| `/api/stats` | GET | Memory database statistics |

## Extending

### Add Knowledge Base Documents

Drop any `.md` file into `knowledge/` — it's auto-loaded on startup:

```markdown
# 新功能说明书

## 产品概述
...

## 常见问题
**Q: 怎么用？**
A: ...
```

The RAG module parses headings into sections and scores them against user queries.

### Connect a real LLM

Replace mock logic in `nodes.py`:

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4")

def identify_intent(state: dict) -> dict:
    response = llm.invoke(f"Classify intent: {user_message}")
    intent = parse_intent(response)
    return {'intent': intent}
```

### Add a new node

```python
# 1. Define in nodes.py
def new_node(state: dict) -> dict:
    return {'new_field': value}

# 2. Register in graph.py
graph.add_node('new_node', new_node)
graph.add_edge('existing_node', 'new_node')
```

### Custom conditional routing

```python
def custom_router(state: dict) -> str:
    if state.get('some_condition'):
        return 'path_a'
    else:
        return 'path_b'

graph.add_conditional_edges(
    'source_node',
    custom_router,
    {'path_a': 'node_a', 'path_b': 'node_b'}
)
```

## Test Scenarios

1. **New session** - User asks about product usage
2. **Satisfaction check** - Bot asks for feedback after reply
3. **Retry mechanism** - User dissatisfied, bot regenerates reply
4. **Human escalation** - After 3 unsatisfied retries, escalate via interrupt
5. **Session resume** - Restore suspended session and continue

## Notes

- Mock data is for testing only; connect real LLMs in production
- SQLite database file `checkpoints.db` is auto-created
- Each session uses a unique `thread_id` for isolation
- `interrupt` requires proper exception handling and resume logic

## License

MIT
