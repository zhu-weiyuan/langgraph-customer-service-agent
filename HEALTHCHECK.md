# LangGraph Customer Service Agent - Health Check & Monitoring Guide

## Overview

Health check endpoints, monitoring strategy, and alerting configuration for production deployment.

## Health Check Endpoints

### 1. `/api/health` - Basic Health Status

**Purpose**: Liveness check for load balancers and orchestrators.

**Method**: GET

**Response Time**: < 100ms

**Success Response** (200 OK):
```json
{
  "ok": true,
  "service": "LangGraph Customer Service Agent",
  "port": 7860,
  "platform": "Windows 10",
  "python": "3.11.9",
  "llm": {
    "reachable": true,
    "url": "http://127.0.0.1:8080"
  },
  "database": {
    "conversations": 1523,
    "tickets": 45,
    "total_ratings": 234,
    "avg_rating": 4.2
  },
  "knowledge_base": {
    "documents": 6,
    "sections": 42
  },
  "requests": {
    "total": 5678,
    "errors": 23
  }
}
```

### 2. `/api/ready` - Readiness Probe

**Purpose**: Determine if service is ready to accept traffic.

**Checks**:
- ✅ LLM API connectivity
- ✅ Redis connection
- ✅ Graph initialization complete
- ✅ Knowledge base loaded
- ✅ Checkpoint database accessible

**Success Response** (200 OK):
```json
{
  "ready": true,
  "checks": {
    "llm": {"status": "ok", "latency_ms": 45},
    "redis": {"status": "ok", "latency_ms": 2},
    "graph": {"status": "ok", "initialized": true},
    "knowledge_base": {"status": "ok", "documents": 6},
    "checkpoint_db": {"status": "ok", "writable": true}
  }
}
```

### 3. `/api/metrics` - Prometheus Metrics

**Purpose**: Expose metrics for Prometheus scraping.

**Content-Type**: `text/plain; version=0.0.4`

**Key Metrics**:

#### HTTP Metrics
```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{endpoint="/api/chat",status="success"} 5234
http_requests_total{endpoint="/api/chat",status="error"} 45

# HELP http_request_duration_ms Request latency histogram
# TYPE http_request_duration_ms histogram
http_request_duration_ms_bucket{endpoint="/api/chat",le="1000"} 3456
http_request_duration_ms_bucket{endpoint="/api/chat",le="5000"} 5100
http_request_duration_ms_bucket{endpoint="/api/chat",le="+Inf"} 5234
```

#### LangGraph Metrics
```
# HELP langgraph_graph_executions_total Graph execution count
# TYPE langgraph_graph_executions_total counter
langgraph_graph_executions_total{status="success"} 5200
langgraph_graph_executions_total{status="error"} 34
langgraph_graph_executions_total{status="interrupted"} 12

# HELP langgraph_graph_execution_duration_ms Graph execution latency
# TYPE langgraph_graph_execution_duration_ms histogram
langgraph_graph_execution_duration_ms_bucket{le="2000"} 4500
langgraph_graph_execution_duration_ms_bucket{le="5000"} 5100
langgraph_graph_execution_duration_ms_sum 12345678.9
langgraph_graph_execution_duration_ms_count 5234

# HELP langgraph_node_executions_total Node execution count
# TYPE langgraph_node_executions_total counter
langgraph_node_executions_total{node="identify_intent"} 5234
langgraph_node_executions_total{node="generate_reply"} 5198
langgraph_node_executions_total{node="rag_retrieval"} 3456

# HELP langgraph_state_size_bytes Current state size
# TYPE langgraph_state_size_bytes gauge
langgraph_state_size_bytes{session_id="abc123"} 4096

# HELP langgraph_active_sessions_count Active session count
# TYPE langgraph_active_sessions_count gauge
langgraph_active_sessions_count 42
```

#### RAG Metrics
```
# HELP langgraph_rag_retrievals_total RAG retrieval count
# TYPE langgraph_rag_retrievals_total counter
langgraph_rag_retrievals_total{status="success"} 3400
langgraph_rag_retrievals_total{status="timeout"} 56

# HELP langgraph_rag_duration_ms RAG retrieval latency
# TYPE langgraph_rag_duration_ms histogram
langgraph_rag_duration_ms_bucket{le="500"} 2800
langgraph_rag_duration_ms_bucket{le="1000"} 3200
langgraph_rag_duration_ms_bucket{le="2000"} 3400

# HELP langgraph_rag_context_size_bytes RAG context size
# TYPE langgraph_context_size_bytes gauge
langgraph_rag_context_size_bytes 2048

# HELP langgraph_rag_rounds_total RAG iteration rounds
# TYPE langgraph_rag_rounds_total counter
langgraph_rag_rounds_total{rounds="1"} 2500
langgraph_rag_rounds_total{rounds="2"} 800
langgraph_rag_rounds_total{rounds="3"} 100
```

#### Circuit Breaker Metrics
```
# HELP circuit_breaker_state Circuit breaker state (0=closed, 1=open, 2=half-open)
# TYPE circuit_breaker_state gauge
circuit_breaker_state{service="llm"} 0
circuit_breaker_state{service="redis"} 0
circuit_breaker_state{service="rag"} 0

# HELP circuit_breaker_failures_total Circuit breaker failures
# TYPE circuit_breaker_failures_total counter
circuit_breaker_failures_total{service="llm"} 3

# HELP circuit_breaker_successes_total Circuit breaker successes
# TYPE circuit_breaker_successes_total counter
circuit_breaker_successes_total{service="llm"} 5200

# HELP circuit_breaker_last_failure_seconds Time since last failure
# TYPE circuit_breaker_last_failure_seconds gauge
circuit_breaker_last_failure_seconds{service="llm"} 300
```

#### Rate Limiting Metrics
```
# HELP rate_limit_events_total Rate limit events
# TYPE rate_limit_events_total counter
rate_limit_events_total{user="user123"} 5

# HELP rate_limit_remaining_requests Remaining requests in window
# TYPE rate_limit_remaining_requests gauge
rate_limit_remaining_requests{user="user123",endpoint="/api/chat"} 25

# HELP rate_limit_window_reset_seconds Seconds until reset
# TYPE rate_limit_window_reset_seconds gauge
rate_limit_window_reset_seconds{user="user123"} 45
```

#### Cache Metrics
```
# HELP cache_requests_total Cache requests
# TYPE cache_requests_total counter
cache_requests_total{type="hit"} 3200
cache_requests_total{type="miss"} 2000

# HELP cache_hit_ratio Cache hit ratio
# TYPE cache_hit_ratio gauge
cache_hit_ratio 0.62

# HELP cache_size_bytes Cache memory usage
# TYPE cache_size_bytes gauge
cache_size_bytes 52428800
```

## Monitoring Architecture

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
│ LangGraph Agent │────▶│  Prometheus  │────▶│   Grafana    │
│  /api/metrics   │     │   (scrape)   │     │  (dashboard) │
└─────────────────┘     └──────────────┘     └──────────────┘
                              │
                              ▼
                       ┌──────────────┐
                       │  Alertmanager│
                       └──────────────┘
                              │
                              ▼
                       ┌──────────────┐
                       │  Slack/Email │
                       └──────────────┘
```

## Grafana Dashboard Panels

### Recommended Panels:

1. **Request Rate & Error Rate** (Graph)
   - Query: `rate(http_requests_total[5m])`
   - Query: `rate(http_requests_total{status="error"}[5m])`

2. **Graph Execution Latency** (Heatmap)
   - Query: `rate(langgraph_graph_execution_duration_ms_bucket[5m])`

3. **P95 Latency by Endpoint** (Stat)
   - Query: `histogram_quantile(0.95, rate(http_request_duration_ms_bucket[5m]))`

4. **Active Sessions** (Stat)
   - Query: `langgraph_active_sessions_count`

5. **Circuit Breaker State** (Stat)
   - Query: `circuit_breaker_state{service="llm"}`
   - Thresholds: 0=green, 1=red, 2=yellow

6. **RAG Retrieval Performance** (Graph)
   - Query: `histogram_quantile(0.50/0.95, rate(langgraph_rag_duration_ms_bucket[5m]))`

7. **Cache Hit Ratio** (Gauge)
   - Query: `cache_hit_ratio`
   - Thresholds: <40% red, <60% yellow, >60% green

8. **Rate Limit Events** (Bar Chart)
   - Query: `rate(rate_limit_events_total[1h])`

9. **Graph Node Execution** (Table)
   - Query: `langgraph_node_executions_total`

10. **System Resources** (Time Series)
    - CPU, Memory, Disk usage

## Alerting Rules

### Prometheus Alert Rules

```yaml
groups:
  - name: langgraph-alerts
    rules:
      # High Graph Error Rate
      - alert: LangGraphHighErrorRate
        expr: |
          sum(rate(langgraph_graph_executions_total{status="error"}[5m])) 
          / sum(rate(langgraph_graph_executions_total[5m])) > 0.05
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High graph error rate"
          description: "Error rate is {{ $value | humanizePercentage }}"
          
      # Circuit Breaker Open
      - alert: LangGraphCircuitBreakerOpen
        expr: circuit_breaker_state{service="llm"} == 1
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "LLM circuit breaker is open"
          description: "LLM service circuit breaker has been open for 1 minute"
          
      # High RAG Latency
      - alert: LangGraphHighRAGLatency
        expr: |
          histogram_quantile(0.95, 
            rate(langgraph_rag_duration_ms_bucket[5m])
          ) > 3000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High RAG retrieval latency"
          description: "P95 RAG latency is {{ $value }}ms"
          
      # High Graph Latency
      - alert: LangGraphHighGraphLatency
        expr: |
          histogram_quantile(0.95, 
            rate(langgraph_graph_execution_duration_ms_bucket[5m])
          ) > 5000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High graph execution latency"
          description: "P95 graph latency is {{ $value }}ms"
          
      # Service Down
      - alert: LangGraphDown
        expr: up{job="langgraph-agent"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "LangGraph agent is down"
          
      # High Rate Limiting
      - alert: LangGraphFrequentRateLimiting
        expr: rate(rate_limit_events_total[5m]) > 10
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Frequent rate limiting"
          
      # Low Cache Hit Ratio
      - alert: LangGraphLowCacheHitRatio
        expr: cache_hit_ratio < 0.4
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Low cache hit ratio"
          description: "Cache hit ratio is {{ $value | humanizePercentage }}"
```

## Performance Benchmarks

### Target Metrics

| Metric | Target | Warning | Critical |
|--------|--------|---------|----------|
| P50 Graph Latency | < 1000ms | > 2000ms | > 3000ms |
| P95 Graph Latency | < 3000ms | > 5000ms | > 8000ms |
| P99 Graph Latency | < 5000ms | > 7000ms | > 10000ms |
| RAG P95 Latency | < 1000ms | > 2000ms | > 3000ms |
| Error Rate | < 2% | > 5% | > 10% |
| Cache Hit Ratio | > 60% | < 40% | < 20% |
| Circuit Breaker Open | 0 | 1 | > 60s |

## Incident Response

### Runbook: High Graph Error Rate

1. Check `/api/health` endpoint
2. Review graph execution logs
3. Check LLM service status
4. Verify RAG retrieval success rate
5. Check circuit breaker state
6. Review recent knowledge base changes
7. Scale horizontally if load-related

### Runbook: Circuit Breaker Open

1. Check LLM service health
2. Review LLM error logs
3. Check network connectivity
4. Verify LLM resource utilization
5. Consider failing over to backup LLM
6. Monitor half-open transition

### Runbook: High RAG Latency

1. Check vector database performance
2. Review knowledge base size
3. Check embedding generation time
4. Verify network to embedding service
5. Consider caching optimization
6. Review RAG round configuration

## Log Aggregation

### Structured Log Format
```json
{
  "timestamp": "2026-07-26T10:30:45.123Z",
  "level": "INFO",
  "logger": "agent.graph",
  "message": "Graph execution completed",
  "request_id": "req-abc123",
  "session_id": "sess-xyz789",
  "graph_duration_ms": 2453,
  "nodes_executed": 3,
  "rag_rounds": 2,
  "intent": "consult",
  "emotion": "neutral",
  "module": "graph",
  "function": "stream"
}
```

## Contact & Escalation

- **Level 1**: On-call engineer
- **Level 2**: Platform team lead
- **Level 3**: System architect

## Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-07-26 | LangGraph Team | Initial health check documentation |
