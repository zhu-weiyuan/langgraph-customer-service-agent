# LangGraph Customer Service Agent - Production Deployment Guide

## Overview

Production deployment hardening for the LangGraph Customer Service Agent, implementing JavaGuide "AI Application System Design" best practices.

## Architecture

```
┌─────────────┐     ┌───────────────────┐     ┌─────────────┐
│   Client    │────▶│ LangGraph Agent   │────▶│    Redis    │
│             │     │   (FastAPI)       │     │  (Cache)    │
└─────────────┘     └───────────────────┘     └─────────────┘
                          │
                          ▼
                   ┌──────────────┐     ┌──────────────┐
                   │  LLM Service │     │  PostgreSQL  │
                   │ (llama.cpp)  │     │ (Persistence)│
                   └──────────────┘     └──────────────┘
                          │
                          ▼
                   ┌──────────────┐
                   │  Knowledge   │
                   │    Base      │
                   └──────────────┘
```

## Quick Start

### Development
```bash
docker-compose up -d
```

### Production
```bash
# Set environment variables
export API_KEY=your-secret-api-key
export JWT_SECRET=your-jwt-secret-min-32-chars
export OPENAI_API_KEY=your-api-key
export OPENAI_BASE_URL=http://your-llm-service:8080

# Start with production profile
docker-compose -f docker-compose.prod.yml --profile monitoring up -d
```

## Environment Variables

### Required
| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | LLM API key | `sk-local` |
| `OPENAI_BASE_URL` | LLM endpoint URL | `http://host.docker.internal:8080` |

### LangGraph Configuration
| Variable | Description | Default |
|----------|-------------|---------|
| `GRAPH_MAX_RETRIES` | Max graph execution retries | `3` |
| `GRAPH_TIMEOUT_SECONDS` | Graph execution timeout | `120` |
| `CONTEXT_MAX_TOKENS` | Max context window size | `4000` |
| `USE_SQLITE` | Enable SQLite checkpointing | `1` |

### RAG Configuration
| Variable | Description | Default |
|----------|-------------|---------|
| `RAG_MAX_ROUNDS` | Max RAG retrieval rounds | `3` |
| `RAG_MIN_CONTEXT_SCORE` | Minimum context relevance score | `0.5` |
| `KNOWLEDGE_BASE_PATH` | Path to knowledge base | `/app/knowledge` |

### Circuit Breaker
| Variable | Description | Default |
|----------|-------------|---------|
| `CIRCUIT_BREAKER_THRESHOLD` | Failures before opening | `5` |
| `CIRCUIT_BREAKER_TIMEOUT` | Timeout before half-open (s) | `60` |

### Security
| Variable | Description | Default |
|----------|-------------|---------|
| `API_KEY` | API authentication key | (empty) |
| `JWT_SECRET` | JWT signing secret | `change-me-in-production` |

## Health Check Endpoints

### `/api/health` - Basic Health
```bash
curl http://localhost:7860/api/health
```

Response includes:
- Service status
- LLM connectivity
- Database stats
- Knowledge base stats
- Request counters

### `/api/metrics` - Prometheus Metrics
```bash
curl http://localhost:7860/api/metrics
```

Key metrics:
- `http_requests_total` - Request counts by endpoint
- `http_request_duration_ms` - Latency histogram
- `langgraph_graph_executions_total` - Graph execution count
- `langgraph_rag_retrievals_total` - RAG retrieval count
- `circuit_breaker_state` - Circuit breaker status
- `cache_hit_ratio` - Redis cache efficiency

### `/api/ready` - Readiness Probe
Checks:
- ✅ LLM connectivity
- ✅ Redis connection
- ✅ Graph initialization
- ✅ Knowledge base loaded

## LangGraph Graph Health Monitoring

### Graph Execution Metrics

```python
# HELP langgraph_graph_executions_total Total graph executions
# TYPE langgraph_graph_executions_total counter
langgraph_graph_executions_total{status="success"} 1234
langgraph_graph_executions_total{status="error"} 12
langgraph_graph_executions_total{status="interrupted"} 5

# HELP langgraph_graph_execution_duration_ms Graph execution latency
# TYPE langgraph_graph_execution_duration_ms histogram
langgraph_graph_execution_duration_ms_bucket{le="1000"} 890
langgraph_graph_execution_duration_ms_bucket{le="5000"} 1200
langgraph_graph_execution_duration_ms_bucket{le="10000"} 1234
```

### Node-Level Metrics

```python
# HELP langgraph_node_executions_total Executions per graph node
# TYPE langgraph_node_executions_total counter
langgraph_node_executions_total{node="identify_intent"} 1523
langgraph_node_executions_total{node="generate_reply"} 1498
langgraph_node_executions_total{node="rag_retrieval"} 890

# HELP langgraph_node_errors_total Errors per graph node
# TYPE langgraph_node_errors_total counter
langgraph_node_errors_total{node="rag_retrieval"} 3
```

### State Size Monitoring

```python
# HELP langgraph_state_size_bytes Current graph state size
# TYPE langgraph_state_size_bytes gauge
langgraph_state_size_bytes{session_id="abc123"} 4096

# HELP langgraph_message_count Total messages in graph state
# TYPE langgraph_message_count gauge
langgraph_message_count{session_id="abc123"} 12
```

## Circuit Breaker Metrics Dashboard

### Circuit Breaker States

```
# HELP circuit_breaker_state Circuit breaker state
# TYPE circuit_breaker_state gauge
# States: 0=closed, 1=open, 2=half-open
circuit_breaker_state{service="llm"} 0
circuit_breaker_state{service="redis"} 0
circuit_breaker_state{service="rag"} 0

# HELP circuit_breaker_failures_total Circuit breaker failure count
# TYPE circuit_breaker_failures_total counter
circuit_breaker_failures_total{service="llm"} 3

# HELP circuit_breaker_successes_total Circuit breaker success count
# TYPE circuit_breaker_successes_total counter
circuit_breaker_successes_total{service="llm"} 1520

# HELP circuit_breaker_last_failure_seconds Time since last failure
# TYPE circuit_breaker_last_failure_seconds gauge
circuit_breaker_last_failure_seconds{service="llm"} 300
```

### Grafana Dashboard Configuration

Import this JSON dashboard or create manually:

**Panel 1: Circuit Breaker State**
- Type: Stat
- Query: `circuit_breaker_state{service="llm"}`
- Thresholds: 0 (green), 1 (red), 2 (yellow)

**Panel 2: Failure Rate Over Time**
- Type: Graph
- Query: `rate(circuit_breaker_failures_total[5m])`

**Panel 3: Success vs Failure**
- Type: Pie Chart
- Queries: `circuit_breaker_successes_total`, `circuit_breaker_failures_total`

## Rate Limiting Visualization

### Rate Limit Metrics

```python
# HELP rate_limit_events_total Total rate limit events
# TYPE rate_limit_events_total counter
rate_limit_events_total{user="user123"} 5
rate_limit_events_total{user="user456"} 2

# HELP rate_limit_remaining_requests Remaining requests in window
# TYPE rate_limit_remaining_requests gauge
rate_limit_remaining_requests{user="user123", endpoint="/api/chat"} 25

# HELP rate_limit_window_reset_seconds Seconds until window reset
# TYPE rate_limit_window_reset_seconds gauge
rate_limit_window_reset_seconds{user="user123"} 45
```

### Grafana Visualization

**Panel: Rate Limit Events by User**
- Type: Bar Chart
- Query: `rate(rate_limit_events_total[1h])`
- Group by: user

**Panel: Current Rate Limit Status**
- Type: Table
- Query: `rate_limit_remaining_requests`
- Columns: user, endpoint, remaining, reset_in

## Scaling Strategies

### Horizontal Scaling
```bash
# Docker Swarm
docker service scale langgraph-cs-agent=3

# Kubernetes
kubectl scale deployment langgraph-cs-agent --replicas=3
```

### Session Affinity
For LangGraph stateful sessions, enable sticky sessions:

```nginx
upstream langgraph_agent {
    ip_hash;
    server agent1:7860;
    server agent2:7860;
    server agent3:7860;
}
```

### Database Scaling
For high-traffic deployments, use PostgreSQL:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: langgraph
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: langgraph
    volumes:
      - postgres_data:/var/lib/postgresql/data
```

Update connection string:
```bash
export CHECKPOINT_DB=postgresql://langgraph:password@postgres:5432/langgraph
```

## Monitoring Stack

### Prometheus Configuration
```yaml
scrape_configs:
  - job_name: 'langgraph-agent'
    static_configs:
      - targets: ['langgraph-cs-agent:7860']
    metrics_path: '/api/metrics'
    scrape_interval: 15s
```

### Alerting Rules

```yaml
groups:
  - name: langgraph-alerts
    rules:
      - alert: HighGraphErrorRate
        expr: |
          sum(rate(langgraph_graph_executions_total{status="error"}[5m])) 
          / sum(rate(langgraph_graph_executions_total[5m])) > 0.05
        for: 5m
        annotations:
          summary: "High graph error rate"
          
      - alert: CircuitBreakerOpen
        expr: circuit_breaker_state{service="llm"} == 1
        for: 1m
        annotations:
          summary: "LLM circuit breaker is open"
          
      - alert: HighRAGLatency
        expr: |
          histogram_quantile(0.95, 
            rate(langgraph_rag_duration_ms_bucket[5m])
          ) > 3000
        for: 5m
        annotations:
          summary: "RAG retrieval latency is high"
```

## Graceful Shutdown

The application handles SIGTERM/SIGINT:

1. Stop accepting new graph executions
2. Wait for active executions to complete (max 30s)
3. Save checkpoint state
4. Close Redis/database connections
5. Flush logs
6. Exit cleanly

```bash
docker stop langgraph-cs-agent
```

## Configuration Validation

On startup, validates:
- ✅ Environment variables present
- ✅ LLM endpoint reachable
- ✅ Redis connection successful
- ✅ Knowledge base files exist
- ✅ Checkpoint database writable

Invalid configuration causes immediate exit with error details.

## Security Hardening

### Network Isolation
- Redis: localhost only
- Database: localhost only
- Internal service network

### Secrets Management
```bash
docker secret create api_key api_key.txt
docker secret create jwt_secret jwt_secret.txt
```

### Input Validation
- Message length: 4000 chars max
- PII detection and redaction
- Prompt injection detection
- Session ID validation

## Performance Benchmarks

### Target Metrics

| Metric | Target | Warning | Critical |
|--------|--------|---------|----------|
| P50 Graph Latency | < 1000ms | > 2000ms | > 3000ms |
| P95 Graph Latency | < 3000ms | > 5000ms | > 8000ms |
| RAG Retrieval Time | < 500ms | > 1000ms | > 2000ms |
| Error Rate | < 2% | > 5% | > 10% |
| Cache Hit Ratio | > 60% | < 40% | < 20% |

## Backup and Recovery

### Checkpoint Backup
```bash
# Backup SQLite checkpoints
docker exec langgraph-cs-agent cp /app/checkpoints/checkpoints.db /backup/
cp /var/lib/docker/volumes/checkpoints/_data/checkpoints.db ./backup/

# Backup Redis data
docker exec langgraph-redis redis-cli BGSAVE
cp /var/lib/docker/volumes/redis_data/_data/dump.rdb ./backup/
```

### Knowledge Base Backup
```bash
tar -czf backup/knowledge-$(date +%Y%m%d).tar.gz ./knowledge
```

## Troubleshooting

### Check Graph State
```bash
curl http://localhost:7860/api/session/{session_id}
```

### View Active Sessions
```bash
curl http://localhost:7860/api/sessions
```

### Debug Mode
```bash
export LOG_LEVEL=DEBUG
docker-compose up -d
docker logs -f langgraph-cs-agent
```

### Circuit Breaker Debug
```bash
curl http://localhost:7860/api/circuit-breaker/status
```

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-07-26 | Initial production deployment guide |
