# LangGraph Customer Service Agent

A customer service system built with LangGraph, featuring SSE streaming, sentiment analysis, RAG knowledge base, and a polished web UI.

## Features

### Core Chat Engine
- **SSE Streaming**: Real-time token-by-token response streaming via Server-Sent Events
- **Streaming Progress Events**: SSE includes `progress: analyzing` event before LLM generation starts
- **Intent Identification**: Auto-classify user messages (consult / complaint / chat)
- **Sentiment Analysis**: Detects user emotion (angry/sad/anxious/happy) and adjusts bot tone
- **Multi-turn Dialogue**: Context-aware continuous conversation with memory
- **Satisfaction Check**: Retry on dissatisfaction, max 3 attempts before human escalation
- **Human Escalation**: Uses LangGraph `interrupt` to suspend session for human handling

### RAG Knowledge Base
- **TF-IDF Retrieval**: Local docs/FAQ retrieval with scoring — grounded answers from product manuals
- **Hot Reload**: Reload KB without restarting the server via `/api/rag/reload`
- **Knowledge Documents**: Auto-loaded from `knowledge/` directory (product manuals, FAQ, troubleshooting)

### Security
- **Prompt Injection Guard**: Scans input for injection attempts before processing
- **PII Redaction**: Detects and logs personally identifiable information
- **Rate Limiting**: Sliding-window per-IP rate limiter (default 60 req/60s), configurable via env vars

### Web UI
- **Modern Design**: Purple/blue gradient palette with glassmorphism effects
- **Dark Mode**: Toggle between light/dark themes with localStorage persistence
- **Bilingual**: Chinese / English toggle with full i18n support
- **Voice I/O**: Web Speech API for voice input and text-to-speech output
- **Emoji Reactions**: 👍👎💡 reactions on bot messages
- **Star Ratings**: ⭐ 1-5 star helpfulness rating after each bot reply
- **Quick Replies**: Context-aware suggestion buttons
- **Session Search**: Search across all sessions from the UI
- **Session Export**: Export full session history as JSON
- **Copy Conversation**: One-click copy of entire conversation
- **Scroll-to-Bottom**: Floating button appears when scrolled up
- **Character Counter**: Live character count with warning threshold

### Analytics Dashboard
- **KPI Cards**: Total conversations, average reply length, ratings, ticket counts
- **Intent/Emotion Distribution**: Visual bar charts
- **Rating Visualization**: Star rating display with averages
- **Session Table**: Recent sessions with message counts and previews
- **Auto-Refresh**: 30-second auto-refresh with toggle

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Chat UI |
| `/analytics` | GET | Analytics Dashboard |
| `/api/chat` | POST | `{"message": "...", "session_id": "..."}` → bot response (JSON or SSE stream) |
| `/api/session/<id>` | GET | Get session state (messages, intent, emotion) |
| `/api/export/<id>` | GET | Export full session history as JSON |
| `/api/stats` | GET | Memory database statistics + rating summary |
| `/api/rating` | POST | `{"session_id": "...", "stars": 5}` → log helpfulness rating |
| `/api/reaction` | POST | `{"session_id": "...", "emoji": "👍"}` → log emoji reaction |
| `/api/rag/reload` | GET | Hot reload knowledge base |
| `/api/sessions` | GET | List all sessions (supports `?search=` query) |
| `/api/health` | GET | Health check (LLM connectivity, DB stats, KB status) |
| `/api/analytics` | GET | Conversation analytics data |

## Architecture

```
START → identify_intent → generate_reply (+RAG) → check_satisfaction
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
│   ├── memory.py         # SQLite conversation memory
│   ├── llm_client.py     # LLM API client (llama.cpp)
│   ├── summary.py        # Dialogue summary generation
│   └── graph.py          # Graph construction and compilation
├── knowledge/            # Knowledge base (auto-loaded)
│   ├── product-manual.md
│   ├── troubleshooting.md
│   ├── faq.md
│   └── ...
├── templates/            # HTML templates
│   ├── index.html        # Chat UI
│   └── analytics.html    # Analytics dashboard
├── app.py                # Web server (port 7860)
├── main.py               # Entry point (interactive / test / resume modes)
├── test_agent.py         # Core agent tests
├── test_rag.py           # RAG retrieval tests
├── test_sentiment.py     # Sentiment analysis tests
├── test_eval.py          # Evaluation tests
├── requirements.txt      # Dependencies
├── Dockerfile            # Docker build
├── docker-compose.yml    # Docker Compose config
└── README.md             # This file
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Start llama.cpp (required for LLM)

Ensure llama.cpp is running on port 8080 with your preferred model.

### 3. Run the server

```bash
python app.py
```

Visit http://localhost:7860

### 4. Run tests

```bash
python test_agent.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_SQLITE` | `0` | Set to `1` for persistent SQLite checkpoints |
| `CHECKPOINT_DB` | `checkpoints.db` | Path to checkpoint database |
| `RATE_LIMIT_REQUESTS` | `60` | Max requests per window |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds |

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

### Add a New Node

```python
# 1. Define in nodes.py
def new_node(state: dict) -> dict:
    return {'new_field': value}

# 2. Register in graph.py
graph.add_node('new_node', new_node)
graph.add_edge('existing_node', 'new_node')
```

## Docker Deployment

```bash
docker-compose up -d

# Access at http://localhost:7860
```

## License

MIT
