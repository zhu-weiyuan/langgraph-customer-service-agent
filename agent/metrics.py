<<<<<<< HEAD
"""
agent/metrics.py — 统一指标层 (P3 可观测性栈)

设计:
- 优先使用 prometheus_client (Counter/Histogram/Gauge + generate_latest)。
- prometheus_client 不可用时, 使用内置降级实现, 输出**合法的 Prometheus 文本格式**:
  * `# HELP` / `# TYPE` 行只含裸指标名 (无大括号、无 label)。
  * label 只出现在样本行: `name{k="v",...} value`。
  * histogram 输出 `_bucket{le=...}` / `_sum` / `_count`。

指标集 (全部单位为秒/次/元):
- http_requests_total{method,endpoint,status}          counter
- http_request_duration_seconds{method,endpoint}       histogram
- llm_tokens_total{model,scene,direction}              counter
- llm_cost_yuan_total{model}                           counter
- llm_requests_total{model,outcome}                    counter
- node_duration_seconds{node}                          histogram
- rag_hit_ratio                                        gauge
- rate_limit_events_total{tier}                        counter
- circuit_breaker_state{name}                          gauge (0=closed,1=half_open,2=open)
- cache_events_total{cache,result}                     counter
- feedback_events_total{kind}                          counter

用法 (app 层):
    from agent.metrics import metrics, record_http_request
    record_http_request("POST", "/api/chat", 200, 0.42)
    text, content_type = metrics.render()   # 暴露在 GET /api/metrics

本模块替代旧的两套冲突 MetricsCollector (原 agent/metrics.py 与
agent/observability.py 中各一套), 是项目内唯一指标出口。
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

# ── 可选依赖: prometheus_client ────────────────────────────────
try:  # pragma: no cover - 环境相关
    from prometheus_client import (  # type: ignore
        CollectorRegistry,
        Counter as _PromCounter,
        Gauge as _PromGauge,
        Histogram as _PromHistogram,
        generate_latest as _prom_generate_latest,
        CONTENT_TYPE_LATEST as _PROM_CONTENT_TYPE,
    )
    from prometheus_client import multiprocess as _prom_multiprocess  # type: ignore
    _HAS_PROMETHEUS_CLIENT = True
except Exception:  # pragma: no cover
    _HAS_PROMETHEUS_CLIENT = False
    _prom_multiprocess = None
    _PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

FALLBACK_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
logger = logging.getLogger("agent.metrics")

# 默认延迟桶 (秒), 与 prometheus_client 默认桶量级一致, 面向 LLM 场景加长尾
DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
    1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
)


def _escape_label_value(value: str) -> str:
    """按 Prometheus 文本格式规范转义 label 值。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _format_labels(label_names: Sequence[str], label_values: Sequence[str]) -> str:
    if not label_names:
        return ""
    pairs = ",".join(
        f'{k}="{_escape_label_value(v)}"' for k, v in zip(label_names, label_values)
    )
    return "{" + pairs + "}"


def _format_value(v: float) -> str:
    if v == math.inf:
        return "+Inf"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(float(v))


# ── 内置降级实现 ───────────────────────────────────────────────
class _FallbackMetric:
    kind = "untyped"

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._lock = threading.Lock()

    def _key(self, label_values: Sequence[str]) -> Tuple[str, ...]:
        vals = tuple(str(v) for v in label_values)
        if len(vals) != len(self.label_names):
            raise ValueError(
                f"metric {self.name}: expected labels {self.label_names}, got {vals}"
            )
        return vals

    def render(self) -> List[str]:  # pragma: no cover - overridden
        raise NotImplementedError


class FallbackCounter(_FallbackMetric):
    kind = "counter"

    def __init__(self, name, help_text, label_names=()):
        super().__init__(name, help_text, label_names)
        self._values: Dict[Tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, *label_values: str) -> None:
        if amount < 0:
            raise ValueError("counter cannot decrease")
        key = self._key(label_values)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}",
                 f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        if not items and not self.label_names:
            items = [((), 0.0)]
        for key, val in items:
            lines.append(
                f"{self.name}{_format_labels(self.label_names, key)} {_format_value(val)}"
            )
        return lines


class FallbackGauge(_FallbackMetric):
    kind = "gauge"

    def __init__(self, name, help_text, label_names=()):
        super().__init__(name, help_text, label_names)
        self._values: Dict[Tuple[str, ...], float] = {}

    def set(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}",
                 f"# TYPE {self.name} gauge"]
        with self._lock:
            items = sorted(self._values.items())
        if not items and not self.label_names:
            items = [((), 0.0)]
        for key, val in items:
            lines.append(
                f"{self.name}{_format_labels(self.label_names, key)} {_format_value(val)}"
            )
        return lines


class FallbackHistogram(_FallbackMetric):
    kind = "histogram"

    def __init__(self, name, help_text, label_names=(),
                 buckets: Sequence[float] = DEFAULT_BUCKETS):
        super().__init__(name, help_text, label_names)
        self.buckets = tuple(sorted(buckets))
        # key -> {"buckets": [count per bucket], "sum": float, "count": int}
        self._series: Dict[Tuple[str, ...], dict] = {}

    def observe(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            s = self._series.get(key)
            if s is None:
                s = {"buckets": [0] * len(self.buckets), "sum": 0.0, "count": 0}
                self._series[key] = s
            s["sum"] += float(value)
            s["count"] += 1
            for i, b in enumerate(self.buckets):
                if value <= b:
                    s["buckets"][i] += 1

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}",
                 f"# TYPE {self.name} histogram"]
        with self._lock:
            items = sorted(
                (k, {"buckets": list(v["buckets"]), "sum": v["sum"], "count": v["count"]})
                for k, v in self._series.items()
            )
        for key, s in items:
            for b, c in zip(self.buckets, s["buckets"]):
                labels = _format_labels(
                    tuple(self.label_names) + ("le",),
                    tuple(key) + (_format_value(float(b)),),
                )
                lines.append(f"{self.name}_bucket{labels} {c}")
            inf_labels = _format_labels(
                tuple(self.label_names) + ("le",), tuple(key) + ("+Inf",)
            )
            lines.append(f"{self.name}_bucket{inf_labels} {s['count']}")
            base = _format_labels(self.label_names, key)
            lines.append(f"{self.name}_sum{base} {_format_value(s['sum'])}")
            lines.append(f"{self.name}_count{base} {s['count']}")
        return lines


class FallbackRegistry:
    def __init__(self):
        self._metrics: List[_FallbackMetric] = []
        self._lock = threading.Lock()

    def register(self, metric: _FallbackMetric) -> _FallbackMetric:
        with self._lock:
            self._metrics.append(metric)
        return metric

    def render_text(self) -> str:
        lines: List[str] = []
        with self._lock:
            metrics_list = list(self._metrics)
        for m in metrics_list:
            lines.extend(m.render())
        return "\n".join(lines) + "\n"


# ── 统一 Metrics 门面 ─────────────────────────────────────────
class Metrics:
    """统一指标门面。所有业务代码只与本类 (或模块级便捷函数) 交互。

    参数:
        use_prometheus_client: None=自动检测; False=强制降级实现 (测试用)。
    """

    def __init__(self, use_prometheus_client: Optional[bool] = None):
        self._legacy_request_counts: Dict[str, int] = {}
        self._legacy_error_counts: Dict[str, int] = {}
        if use_prometheus_client is None:
            use_prometheus_client = _HAS_PROMETHEUS_CLIENT
        self.using_prometheus_client = bool(use_prometheus_client and _HAS_PROMETHEUS_CLIENT)
        self.using_prometheus_multiprocess = bool(
            self.using_prometheus_client
            and _prom_multiprocess is not None
            and os.getenv("PROMETHEUS_MULTIPROC_DIR", "").strip()
        )

        if self.using_prometheus_client:
            self._registry = CollectorRegistry()

            def c(name, doc, labels=()):
                return _PromCounter(name, doc, list(labels), registry=self._registry)

            def g(name, doc, labels=()):
                kwargs = {"registry": self._registry}
                if self.using_prometheus_multiprocess:
                    kwargs["multiprocess_mode"] = "mostrecent"
                return _PromGauge(name, doc, list(labels), **kwargs)

            def h(name, doc, labels=(), buckets=DEFAULT_BUCKETS):
                return _PromHistogram(name, doc, list(labels),
                                      buckets=buckets, registry=self._registry)
        else:
            self._registry = FallbackRegistry()
            reg = self._registry.register

            def c(name, doc, labels=()):
                return reg(FallbackCounter(name, doc, labels))

            def g(name, doc, labels=()):
                return reg(FallbackGauge(name, doc, labels))

            def h(name, doc, labels=(), buckets=DEFAULT_BUCKETS):
                return reg(FallbackHistogram(name, doc, labels, buckets=buckets))

        self.http_requests_total = c(
            "http_requests_total", "Total HTTP requests",
            ("method", "endpoint", "status"))
        self.http_request_duration_seconds = h(
            "http_request_duration_seconds", "HTTP request latency in seconds",
            ("method", "endpoint"))
        self.llm_tokens_total = c(
            "llm_tokens_total", "LLM tokens consumed",
            ("model", "scene", "direction"))
        self.llm_cost_yuan_total = c(
            "llm_cost_yuan_total", "Cumulative LLM cost in CNY yuan", ("model",))
        # Cost attribution is intentionally separate from the low-cardinality
        # model-only counter above. The user label is a stable anonymized key
        # supplied by the gateway, so dashboards can group by tenant/user/scene
        # without exposing raw identity values in Prometheus.
        self.llm_cost_attribution_yuan_total = c(
            "llm_cost_attribution_yuan_total",
            "LLM cost attributed by model, tenant, user, scene and prompt version",
            ("model", "tenant", "user", "scene", "prompt_version"))
        self.llm_requests_total = c(
            "llm_requests_total", "LLM API requests by outcome",
            ("model", "outcome"))
        self.llm_attempts_total = c(
            "llm_attempts_total", "LLM model attempts by status",
            ("model", "scene", "status"))
        self.llm_latency_seconds = h(
            "llm_latency_seconds", "LLM request latency in seconds",
            ("model", "scene"))
        self.llm_ttft_seconds = h(
            "llm_ttft_seconds", "LLM time to first token in seconds",
            ("model", "scene"))
        self.llm_fallback_total = c(
            "llm_fallback_total", "LLM fallback selections",
            ("scene", "model"))
        self.llm_route_total = c(
            "llm_route_total", "LLM routing decisions",
            ("scene", "model"))
        self.llm_errors_total = c(
            "llm_errors_total", "LLM errors by model and type",
            ("model", "scene", "error_type"))
        self.node_duration_seconds = h(
            "node_duration_seconds", "LangGraph node execution time in seconds",
            ("node",))
        self.rag_queries_total = c(
            "rag_queries_total", "RAG retrieval queries attempted")
        self.rag_hits_total = c(
            "rag_hits_total", "RAG retrieval queries with usable results")
        self.rag_hit_ratio = g(
            "rag_hit_ratio", "Sliding-window RAG retrieval hit ratio (0..1)")
        self.rag_authoritative_queries = g(
            "rag_authoritative_queries",
            "Authoritative cumulative RAG query count from PostgreSQL")
        self.rag_authoritative_hits = g(
            "rag_authoritative_hits",
            "Authoritative cumulative RAG hit count from PostgreSQL")
        try:
            window_size = int(os.environ.get("RAG_METRICS_WINDOW", "100"))
        except (TypeError, ValueError):
            window_size = 100
        self._rag_window = deque(maxlen=max(1, window_size))
        self._rag_lock = threading.Lock()
        # RAG counters are exported by every worker, while the authoritative
        # aggregate is kept in PostgreSQL.  This prevents a restart or a
        # multi-worker split-brain from making the dashboard show 0% again.
        self._rag_total = 0
        self._rag_hits = 0
        self._rag_persistence_loaded = False
        self.rate_limit_events_total = c(
            "rate_limit_events_total", "Requests rejected by rate limiter", ("tier",))
        self.circuit_breaker_state = g(
            "circuit_breaker_state",
            "Circuit breaker state (0=closed,1=half_open,2=open)", ("name",))
        self.cache_events_total = c(
            "cache_events_total", "Cache lookup events", ("cache", "result"))
        self.feedback_events_total = c(
            "feedback_events_total", "User feedback events", ("kind",))
        self.background_events_dropped_total = c(
            "background_events_dropped_total",
            "Best-effort background events dropped because a queue was full",
            ("queue",))

    # ── 内部适配: 屏蔽两套后端 API 差异 ──
    def _inc(self, metric, amount: float, *labels: str) -> None:
        if self.using_prometheus_client:
            (metric.labels(*labels) if labels else metric).inc(amount)
        else:
            metric.inc(amount, *labels)

    def _observe(self, metric, value: float, *labels: str) -> None:
        if self.using_prometheus_client:
            (metric.labels(*labels) if labels else metric).observe(value)
        else:
            metric.observe(value, *labels)

    def _set(self, metric, value: float, *labels: str) -> None:
        if self.using_prometheus_client:
            (metric.labels(*labels) if labels else metric).set(value)
        else:
            metric.set(value, *labels)

    def record_http_request(self, method: str, endpoint: str,
                            status: int, duration_seconds: float) -> None:
        self._inc(self.http_requests_total, 1, str(method).upper(), endpoint, str(status))
        self._observe(self.http_request_duration_seconds, duration_seconds,
                      str(method).upper(), endpoint)

    def record_request(self, endpoint: str, status: int, duration_ms: float) -> None:
        """Legacy compatibility wrapper; duration is converted from ms to seconds."""
        self._legacy_request_counts[endpoint] = self._legacy_request_counts.get(endpoint, 0) + 1
        if int(status) >= 500:
            self._legacy_error_counts[endpoint] = self._legacy_error_counts.get(endpoint, 0) + 1
        self.record_http_request("GET", endpoint, status, float(duration_ms) / 1000.0)

    @property
    def request_counts(self) -> Dict[str, int]:
        return dict(self._legacy_request_counts)

    @property
    def error_counts(self) -> Dict[str, int]:
        return dict(self._legacy_error_counts)

    def get_metrics(self) -> str:
        return self.render()[0]

    def record_llm_tokens(self, model: str, scene: str, direction: str, count: int) -> None:
        """direction: 'input' | 'output'"""
        self._inc(self.llm_tokens_total, max(0, int(count)), model, scene, direction)

    def record_llm_cost(self, model: str, cost_yuan: float) -> None:
        self._inc(self.llm_cost_yuan_total, max(0.0, float(cost_yuan)), model)

    def record_llm_cost_attribution(self, model: str, tenant: str, user: str,
                                    scene: str, prompt_version: str,
                                    cost_yuan: float) -> None:
        """Record a cost slice suitable for tenant/user/scene dashboards."""
        self._inc(
            self.llm_cost_attribution_yuan_total,
            max(0.0, float(cost_yuan)),
            model or "unknown", tenant or "default", user or "anonymous",
            scene or "default", prompt_version or "v1",
        )

    def record_llm_request(self, model: str, outcome: str) -> None:
        """outcome: 'success' | 'error' | 'timeout' | 'circuit_open' ..."""
        self._inc(self.llm_requests_total, 1, model, outcome)

    def record_llm_attempt(self, model: str, scene: str, status: str) -> None:
        self._inc(self.llm_attempts_total, 1, model or "unknown", scene or "default", status or "attempt")

    def record_llm_latency(self, model: str, scene: str, seconds: float) -> None:
        self._observe(self.llm_latency_seconds, max(0.0, float(seconds)), model or "unknown", scene or "default")

    def record_llm_ttft(self, model: str, scene: str, seconds: float) -> None:
        self._observe(self.llm_ttft_seconds, max(0.0, float(seconds)), model or "unknown", scene or "default")

    def record_llm_fallback(self, scene: str, model: str) -> None:
        self._inc(self.llm_fallback_total, 1, scene or "default", model or "unknown")

    def record_llm_route(self, scene: str, model: str) -> None:
        self._inc(self.llm_route_total, 1, scene or "default", model or "unknown")

    def record_llm_error(self, model: str, scene: str, error_type: str) -> None:
        self._inc(self.llm_errors_total, 1, model or "unknown", scene or "default", error_type or "error")

    def record_node_duration(self, node: str, duration_seconds: float) -> None:
        self._observe(self.node_duration_seconds, duration_seconds, node)

    def set_rag_hit_ratio(self, ratio: float) -> None:
        self._set(self.rag_hit_ratio, min(1.0, max(0.0, float(ratio))))

    def load_persistent_rag_metrics(self) -> None:
        """Load the shared RAG counters from PostgreSQL after schema init.

        Metrics are intentionally best-effort: a metrics failure must never
        make the chat endpoint fail.  A missing/unavailable database simply
        leaves the collector in its local in-process mode.
        """
        with self._rag_lock:
            if self._rag_persistence_loaded:
                return
            self._rag_persistence_loaded = True
        try:
            from agent.runtime_db import connect, is_postgres_available
            if not is_postgres_available():
                return
            with connect() as conn:
                row = conn.execute(
                    "SELECT queries, hits FROM rag_metric_state "
                    "WHERE metric_name = 'retrieval'"
                ).fetchone()
            total = int((row or {}).get("queries") or 0)
            hits = int((row or {}).get("hits") or 0)
            with self._rag_lock:
                self._rag_total = max(0, total)
                self._rag_hits = max(0, min(hits, self._rag_total))
            if total and not self.using_prometheus_multiprocess:
                self._inc(self.rag_queries_total, total)
            if hits and not self.using_prometheus_multiprocess:
                self._inc(self.rag_hits_total, hits)
            self._set(self.rag_authoritative_queries, total)
            self._set(self.rag_authoritative_hits, hits)
            self.set_rag_hit_ratio((hits / total) if total else 0.0)
        except Exception as exc:  # pragma: no cover - deployment dependent
            logger.debug("persistent RAG metrics unavailable: %s", exc)

    def _persist_rag_query(self, hit: bool):
        try:
            from agent.runtime_db import connect, is_postgres_available
            if not is_postgres_available():
                return None
            with connect() as conn:
                row = conn.execute(
                    """INSERT INTO rag_metric_state(metric_name, queries, hits)
                       VALUES ('retrieval', 1, %s)
                       ON CONFLICT(metric_name) DO UPDATE SET
                         queries = rag_metric_state.queries + 1,
                         hits = rag_metric_state.hits + EXCLUDED.hits,
                         updated_at = NOW()
                       RETURNING queries, hits""",
                    (1 if hit else 0,),
                ).fetchone()
            return int(row["queries"]), int(row["hits"])
        except Exception as exc:  # pragma: no cover - deployment dependent
            logger.debug("persistent RAG metrics write failed: %s", exc)
            return None

    def record_rag_query(self, hit: bool) -> None:
        """Record a retrieval and expose a restart-safe cumulative hit ratio.

        ``rag_queries_total`` and ``rag_hits_total`` remain Prometheus
        counters.  ``rag_hit_ratio`` is based on the shared PostgreSQL totals
        when PostgreSQL is available, and falls back to the local bounded
        window for hermetic/unit-test environments.
        """
        hit = bool(hit)
        persisted = self._persist_rag_query(hit)
        if persisted is not None:
            total, hits = persisted
            self._inc(self.rag_queries_total, 1)
            if hit:
                self._inc(self.rag_hits_total, 1)
            with self._rag_lock:
                self._rag_total, self._rag_hits = total, hits
            self._set(self.rag_authoritative_queries, total)
            self._set(self.rag_authoritative_hits, hits)
            self.set_rag_hit_ratio((hits / total) if total else 0.0)
            return

        self._inc(self.rag_queries_total, 1)
        if hit:
            self._inc(self.rag_hits_total, 1)
        with self._rag_lock:
            self._rag_total += 1
            self._rag_hits += int(hit)
            self._rag_window.append(hit)
            ratio = sum(self._rag_window) / len(self._rag_window)
            total, hits = self._rag_total, self._rag_hits
        self._set(self.rag_authoritative_queries, total)
        self._set(self.rag_authoritative_hits, hits)
        self.set_rag_hit_ratio(ratio)

    def record_rate_limit_event(self, tier: str = "default") -> None:
        self._inc(self.rate_limit_events_total, 1, tier)

    def set_circuit_breaker_state(self, name: str, state) -> None:
        """state 可为 int(0/1/2) 或字符串 'closed'/'half_open'/'open'。"""
        mapping = {"closed": 0, "half_open": 1, "half-open": 1, "open": 2}
        if isinstance(state, str):
            state = mapping.get(state.lower(), 0)
        self._set(self.circuit_breaker_state, float(state), name)

    def record_cache_event(self, cache: str, result: str) -> None:
        """result: 'hit' | 'miss' | 'error' | 'set'"""
        self._inc(self.cache_events_total, 1, cache, result)

    def record_feedback(self, kind: str) -> None:
        """kind: 'thumbs_up' | 'thumbs_down' | 'rating' ..."""
        self._inc(self.feedback_events_total, 1, kind)

    def record_background_drop(self, queue: str) -> None:
        self._inc(self.background_events_dropped_total, 1, queue or "unknown")

    # ── 输出 ──
    def render(self) -> Tuple[str, str]:
        """返回 (prometheus 文本, content_type), 供 /api/metrics 端点使用。"""
        if self.using_prometheus_multiprocess:
            output_registry = CollectorRegistry()
            _prom_multiprocess.MultiProcessCollector(output_registry)
            return (_prom_generate_latest(output_registry).decode("utf-8"),
                    _PROM_CONTENT_TYPE)
        if self.using_prometheus_client:
            return _prom_generate_latest(self._registry).decode("utf-8"), _PROM_CONTENT_TYPE
        return self._registry.render_text(), FALLBACK_CONTENT_TYPE


class MetricsCollector:
    """Backward-compatible local collector for the pre-P3 test/API contract."""

    def __init__(self):
        self.request_counts: Dict[str, int] = {}
        self.error_counts: Dict[str, int] = {}
        self._latencies: Dict[str, List[float]] = {}
        self._user_stats: Dict[str, Dict[str, int]] = {}
        self._rate_limit_events: List[Dict[str, str]] = []
        self._scene_stats: Dict[str, int] = {}

    def record_request(self, endpoint: str, status: int, duration_ms: float,
                       user_key: Optional[str] = None,
                       tenant_id: Optional[str] = None) -> None:
        self.request_counts[endpoint] = self.request_counts.get(endpoint, 0) + 1
        if int(status) >= 500:
            self.error_counts[endpoint] = self.error_counts.get(endpoint, 0) + 1
        self._latencies.setdefault(endpoint, []).append(float(duration_ms))
        if user_key:
            key = f"{tenant_id or 'default'}:{user_key}"
            stats = self._user_stats.setdefault(key, {"total_requests": 0, "errors": 0})
            stats["total_requests"] += 1
            stats["errors"] += int(int(status) >= 500)

    def get_user_stats(self) -> Dict[str, Dict[str, int]]:
        return dict(self._user_stats)

    def record_rate_limit(self, user_key: str, tenant_id: str = "") -> None:
        self._rate_limit_events.append({"user_key": user_key, "tenant_id": tenant_id})

    def get_rate_limit_stats(self) -> Dict[str, int]:
        return {"total_rate_limited": len(self._rate_limit_events),
                "recent_events_count": len(self._rate_limit_events)}

    def record_scene_request(self, scene: str, status: int, duration_ms: float) -> None:
        self._scene_stats[scene] = self._scene_stats.get(scene, 0) + 1

    def get_scene_stats(self) -> Dict[str, int]:
        return dict(self._scene_stats)

    def get_metrics(self) -> str:
        lines = ["# TYPE http_requests_total counter"]
        for endpoint, count in sorted(self.request_counts.items()):
            errors = self.error_counts.get(endpoint, 0)
            values = sorted(self._latencies.get(endpoint, []))
            lines.append(f'http_requests_total{{endpoint="{endpoint}"}} {count}')
            lines.append(f'http_error_rate{{endpoint="{endpoint}"}} '
                         f'{errors / count if count else 0.0}')
            if values:
                for quantile, index in (("0.50", 0.50), ("0.95", 0.95), ("0.99", 0.99)):
                    pos = min(len(values) - 1, int(index * (len(values) - 1)))
                    lines.append(f'http_request_duration_ms{{endpoint="{endpoint}",quantile="{quantile}"}} {values[pos]}')
        lines.append("process_uptime_seconds 0")
        return "\n".join(lines) + "\n"

# ── 全局单例 + 模块级便捷函数 ─────────────────────────────────
metrics = Metrics()
record_http_request = metrics.record_http_request
record_llm_tokens = metrics.record_llm_tokens
record_llm_cost = metrics.record_llm_cost
record_llm_cost_attribution = metrics.record_llm_cost_attribution
record_llm_request = metrics.record_llm_request
record_node_duration = metrics.record_node_duration
set_rag_hit_ratio = metrics.set_rag_hit_ratio
record_rag_query = metrics.record_rag_query
record_rate_limit_event = metrics.record_rate_limit_event
set_circuit_breaker_state = metrics.set_circuit_breaker_state
record_cache_event = metrics.record_cache_event
record_feedback = metrics.record_feedback
record_background_drop = metrics.record_background_drop
render_metrics = metrics.render

__all__ = [
    "MetricsCollector", "metrics", "DEFAULT_BUCKETS",
    "FallbackCounter", "FallbackGauge", "FallbackHistogram", "FallbackRegistry",
    "record_http_request", "record_llm_tokens", "record_llm_cost",
    "record_llm_cost_attribution", "record_llm_request",
    "record_node_duration", "set_rag_hit_ratio",
    "record_rag_query",
    "record_rate_limit_event", "set_circuit_breaker_state",
    "record_cache_event", "record_feedback", "render_metrics",
    "record_background_drop",
]
=======
"""
agent/metrics.py — 统一指标层 (P3 可观测性栈)

设计:
- 优先使用 prometheus_client (Counter/Histogram/Gauge + generate_latest)。
- prometheus_client 不可用时, 使用内置降级实现, 输出**合法的 Prometheus 文本格式**:
  * `# HELP` / `# TYPE` 行只含裸指标名 (无大括号、无 label)。
  * label 只出现在样本行: `name{k="v",...} value`。
  * histogram 输出 `_bucket{le=...}` / `_sum` / `_count`。

指标集 (全部单位为秒/次/元):
- http_requests_total{method,endpoint,status}          counter
- http_request_duration_seconds{method,endpoint}       histogram
- llm_tokens_total{model,scene,direction}              counter
- llm_cost_yuan_total{model}                           counter
- llm_requests_total{model,outcome}                    counter
- node_duration_seconds{node}                          histogram
- rag_hit_ratio                                        gauge
- rate_limit_events_total{tier}                        counter
- circuit_breaker_state{name}                          gauge (0=closed,1=half_open,2=open)
- cache_events_total{cache,result}                     counter
- feedback_events_total{kind}                          counter

用法 (app 层):
    from agent.metrics import metrics, record_http_request
    record_http_request("POST", "/api/chat", 200, 0.42)
    text, content_type = metrics.render()   # 暴露在 GET /api/metrics

本模块替代旧的两套冲突 MetricsCollector (原 agent/metrics.py 与
agent/observability.py 中各一套), 是项目内唯一指标出口。
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

# ── 可选依赖: prometheus_client ────────────────────────────────
try:  # pragma: no cover - 环境相关
    from prometheus_client import (  # type: ignore
        CollectorRegistry,
        Counter as _PromCounter,
        Gauge as _PromGauge,
        Histogram as _PromHistogram,
        generate_latest as _prom_generate_latest,
        CONTENT_TYPE_LATEST as _PROM_CONTENT_TYPE,
    )
    _HAS_PROMETHEUS_CLIENT = True
except Exception:  # pragma: no cover
    _HAS_PROMETHEUS_CLIENT = False
    _PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

FALLBACK_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
logger = logging.getLogger("agent.metrics")

# 默认延迟桶 (秒), 与 prometheus_client 默认桶量级一致, 面向 LLM 场景加长尾
DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
    1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
)


def _escape_label_value(value: str) -> str:
    """按 Prometheus 文本格式规范转义 label 值。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _format_labels(label_names: Sequence[str], label_values: Sequence[str]) -> str:
    if not label_names:
        return ""
    pairs = ",".join(
        f'{k}="{_escape_label_value(v)}"' for k, v in zip(label_names, label_values)
    )
    return "{" + pairs + "}"


def _format_value(v: float) -> str:
    if v == math.inf:
        return "+Inf"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(float(v))


# ── 内置降级实现 ───────────────────────────────────────────────
class _FallbackMetric:
    kind = "untyped"

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._lock = threading.Lock()

    def _key(self, label_values: Sequence[str]) -> Tuple[str, ...]:
        vals = tuple(str(v) for v in label_values)
        if len(vals) != len(self.label_names):
            raise ValueError(
                f"metric {self.name}: expected labels {self.label_names}, got {vals}"
            )
        return vals

    def render(self) -> List[str]:  # pragma: no cover - overridden
        raise NotImplementedError


class FallbackCounter(_FallbackMetric):
    kind = "counter"

    def __init__(self, name, help_text, label_names=()):
        super().__init__(name, help_text, label_names)
        self._values: Dict[Tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, *label_values: str) -> None:
        if amount < 0:
            raise ValueError("counter cannot decrease")
        key = self._key(label_values)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}",
                 f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        if not items and not self.label_names:
            items = [((), 0.0)]
        for key, val in items:
            lines.append(
                f"{self.name}{_format_labels(self.label_names, key)} {_format_value(val)}"
            )
        return lines


class FallbackGauge(_FallbackMetric):
    kind = "gauge"

    def __init__(self, name, help_text, label_names=()):
        super().__init__(name, help_text, label_names)
        self._values: Dict[Tuple[str, ...], float] = {}

    def set(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}",
                 f"# TYPE {self.name} gauge"]
        with self._lock:
            items = sorted(self._values.items())
        if not items and not self.label_names:
            items = [((), 0.0)]
        for key, val in items:
            lines.append(
                f"{self.name}{_format_labels(self.label_names, key)} {_format_value(val)}"
            )
        return lines


class FallbackHistogram(_FallbackMetric):
    kind = "histogram"

    def __init__(self, name, help_text, label_names=(),
                 buckets: Sequence[float] = DEFAULT_BUCKETS):
        super().__init__(name, help_text, label_names)
        self.buckets = tuple(sorted(buckets))
        # key -> {"buckets": [count per bucket], "sum": float, "count": int}
        self._series: Dict[Tuple[str, ...], dict] = {}

    def observe(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            s = self._series.get(key)
            if s is None:
                s = {"buckets": [0] * len(self.buckets), "sum": 0.0, "count": 0}
                self._series[key] = s
            s["sum"] += float(value)
            s["count"] += 1
            for i, b in enumerate(self.buckets):
                if value <= b:
                    s["buckets"][i] += 1

    def render(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}",
                 f"# TYPE {self.name} histogram"]
        with self._lock:
            items = sorted(
                (k, {"buckets": list(v["buckets"]), "sum": v["sum"], "count": v["count"]})
                for k, v in self._series.items()
            )
        for key, s in items:
            for b, c in zip(self.buckets, s["buckets"]):
                labels = _format_labels(
                    tuple(self.label_names) + ("le",),
                    tuple(key) + (_format_value(float(b)),),
                )
                lines.append(f"{self.name}_bucket{labels} {c}")
            inf_labels = _format_labels(
                tuple(self.label_names) + ("le",), tuple(key) + ("+Inf",)
            )
            lines.append(f"{self.name}_bucket{inf_labels} {s['count']}")
            base = _format_labels(self.label_names, key)
            lines.append(f"{self.name}_sum{base} {_format_value(s['sum'])}")
            lines.append(f"{self.name}_count{base} {s['count']}")
        return lines


class FallbackRegistry:
    def __init__(self):
        self._metrics: List[_FallbackMetric] = []
        self._lock = threading.Lock()

    def register(self, metric: _FallbackMetric) -> _FallbackMetric:
        with self._lock:
            self._metrics.append(metric)
        return metric

    def render_text(self) -> str:
        lines: List[str] = []
        with self._lock:
            metrics_list = list(self._metrics)
        for m in metrics_list:
            lines.extend(m.render())
        return "\n".join(lines) + "\n"


# ── 统一 Metrics 门面 ─────────────────────────────────────────
class Metrics:
    """统一指标门面。所有业务代码只与本类 (或模块级便捷函数) 交互。

    参数:
        use_prometheus_client: None=自动检测; False=强制降级实现 (测试用)。
    """

    def __init__(self, use_prometheus_client: Optional[bool] = None):
        self._legacy_request_counts: Dict[str, int] = {}
        self._legacy_error_counts: Dict[str, int] = {}
        if use_prometheus_client is None:
            use_prometheus_client = _HAS_PROMETHEUS_CLIENT
        self.using_prometheus_client = bool(use_prometheus_client and _HAS_PROMETHEUS_CLIENT)

        if self.using_prometheus_client:
            self._registry = CollectorRegistry()

            def c(name, doc, labels=()):
                return _PromCounter(name, doc, list(labels), registry=self._registry)

            def g(name, doc, labels=()):
                return _PromGauge(name, doc, list(labels), registry=self._registry)

            def h(name, doc, labels=(), buckets=DEFAULT_BUCKETS):
                return _PromHistogram(name, doc, list(labels),
                                      buckets=buckets, registry=self._registry)
        else:
            self._registry = FallbackRegistry()
            reg = self._registry.register

            def c(name, doc, labels=()):
                return reg(FallbackCounter(name, doc, labels))

            def g(name, doc, labels=()):
                return reg(FallbackGauge(name, doc, labels))

            def h(name, doc, labels=(), buckets=DEFAULT_BUCKETS):
                return reg(FallbackHistogram(name, doc, labels, buckets=buckets))

        self.http_requests_total = c(
            "http_requests_total", "Total HTTP requests",
            ("method", "endpoint", "status"))
        self.http_request_duration_seconds = h(
            "http_request_duration_seconds", "HTTP request latency in seconds",
            ("method", "endpoint"))
        self.llm_tokens_total = c(
            "llm_tokens_total", "LLM tokens consumed",
            ("model", "scene", "direction"))
        self.llm_cost_yuan_total = c(
            "llm_cost_yuan_total", "Cumulative LLM cost in CNY yuan", ("model",))
        # Cost attribution is intentionally separate from the low-cardinality
        # model-only counter above. The user label is a stable anonymized key
        # supplied by the gateway, so dashboards can group by tenant/user/scene
        # without exposing raw identity values in Prometheus.
        self.llm_cost_attribution_yuan_total = c(
            "llm_cost_attribution_yuan_total",
            "LLM cost attributed by model, tenant, user, scene and prompt version",
            ("model", "tenant", "user", "scene", "prompt_version"))
        self.llm_requests_total = c(
            "llm_requests_total", "LLM API requests by outcome",
            ("model", "outcome"))
        self.llm_attempts_total = c(
            "llm_attempts_total", "LLM model attempts by status",
            ("model", "scene", "status"))
        self.llm_latency_seconds = h(
            "llm_latency_seconds", "LLM request latency in seconds",
            ("model", "scene"))
        self.llm_ttft_seconds = h(
            "llm_ttft_seconds", "LLM time to first token in seconds",
            ("model", "scene"))
        self.llm_fallback_total = c(
            "llm_fallback_total", "LLM fallback selections",
            ("scene", "model"))
        self.llm_route_total = c(
            "llm_route_total", "LLM routing decisions",
            ("scene", "model"))
        self.llm_errors_total = c(
            "llm_errors_total", "LLM errors by model and type",
            ("model", "scene", "error_type"))
        self.node_duration_seconds = h(
            "node_duration_seconds", "LangGraph node execution time in seconds",
            ("node",))
        self.rag_queries_total = c(
            "rag_queries_total", "RAG retrieval queries attempted")
        self.rag_hits_total = c(
            "rag_hits_total", "RAG retrieval queries with usable results")
        self.rag_hit_ratio = g(
            "rag_hit_ratio", "Sliding-window RAG retrieval hit ratio (0..1)")
        try:
            window_size = int(os.environ.get("RAG_METRICS_WINDOW", "100"))
        except (TypeError, ValueError):
            window_size = 100
        self._rag_window = deque(maxlen=max(1, window_size))
        self._rag_lock = threading.Lock()
        # RAG counters are exported by every worker, while the authoritative
        # aggregate is kept in PostgreSQL.  This prevents a restart or a
        # multi-worker split-brain from making the dashboard show 0% again.
        self._rag_total = 0
        self._rag_hits = 0
        self._rag_persistence_loaded = False
        self.rate_limit_events_total = c(
            "rate_limit_events_total", "Requests rejected by rate limiter", ("tier",))
        self.circuit_breaker_state = g(
            "circuit_breaker_state",
            "Circuit breaker state (0=closed,1=half_open,2=open)", ("name",))
        self.cache_events_total = c(
            "cache_events_total", "Cache lookup events", ("cache", "result"))
        self.feedback_events_total = c(
            "feedback_events_total", "User feedback events", ("kind",))

    # ── 内部适配: 屏蔽两套后端 API 差异 ──
    def _inc(self, metric, amount: float, *labels: str) -> None:
        if self.using_prometheus_client:
            (metric.labels(*labels) if labels else metric).inc(amount)
        else:
            metric.inc(amount, *labels)

    def _observe(self, metric, value: float, *labels: str) -> None:
        if self.using_prometheus_client:
            (metric.labels(*labels) if labels else metric).observe(value)
        else:
            metric.observe(value, *labels)

    def _set(self, metric, value: float, *labels: str) -> None:
        if self.using_prometheus_client:
            (metric.labels(*labels) if labels else metric).set(value)
        else:
            metric.set(value, *labels)

    def record_http_request(self, method: str, endpoint: str,
                            status: int, duration_seconds: float) -> None:
        self._inc(self.http_requests_total, 1, str(method).upper(), endpoint, str(status))
        self._observe(self.http_request_duration_seconds, duration_seconds,
                      str(method).upper(), endpoint)

    def record_request(self, endpoint: str, status: int, duration_ms: float) -> None:
        """Legacy compatibility wrapper; duration is converted from ms to seconds."""
        self._legacy_request_counts[endpoint] = self._legacy_request_counts.get(endpoint, 0) + 1
        if int(status) >= 500:
            self._legacy_error_counts[endpoint] = self._legacy_error_counts.get(endpoint, 0) + 1
        self.record_http_request("GET", endpoint, status, float(duration_ms) / 1000.0)

    @property
    def request_counts(self) -> Dict[str, int]:
        return dict(self._legacy_request_counts)

    @property
    def error_counts(self) -> Dict[str, int]:
        return dict(self._legacy_error_counts)

    def get_metrics(self) -> str:
        return self.render()[0]

        self._inc(self.http_requests_total, 1, str(method).upper(), endpoint, str(status))
        self._observe(self.http_request_duration_seconds, duration_seconds,
                      str(method).upper(), endpoint)

    def record_llm_tokens(self, model: str, scene: str, direction: str, count: int) -> None:
        """direction: 'input' | 'output'"""
        self._inc(self.llm_tokens_total, max(0, int(count)), model, scene, direction)

    def record_llm_cost(self, model: str, cost_yuan: float) -> None:
        self._inc(self.llm_cost_yuan_total, max(0.0, float(cost_yuan)), model)

    def record_llm_cost_attribution(self, model: str, tenant: str, user: str,
                                    scene: str, prompt_version: str,
                                    cost_yuan: float) -> None:
        """Record a cost slice suitable for tenant/user/scene dashboards."""
        self._inc(
            self.llm_cost_attribution_yuan_total,
            max(0.0, float(cost_yuan)),
            model or "unknown", tenant or "default", user or "anonymous",
            scene or "default", prompt_version or "v1",
        )

    def record_llm_request(self, model: str, outcome: str) -> None:
        """outcome: 'success' | 'error' | 'timeout' | 'circuit_open' ..."""
        self._inc(self.llm_requests_total, 1, model, outcome)

    def record_llm_attempt(self, model: str, scene: str, status: str) -> None:
        self._inc(self.llm_attempts_total, 1, model or "unknown", scene or "default", status or "attempt")

    def record_llm_latency(self, model: str, scene: str, seconds: float) -> None:
        self._observe(self.llm_latency_seconds, max(0.0, float(seconds)), model or "unknown", scene or "default")

    def record_llm_ttft(self, model: str, scene: str, seconds: float) -> None:
        self._observe(self.llm_ttft_seconds, max(0.0, float(seconds)), model or "unknown", scene or "default")

    def record_llm_fallback(self, scene: str, model: str) -> None:
        self._inc(self.llm_fallback_total, 1, scene or "default", model or "unknown")

    def record_llm_route(self, scene: str, model: str) -> None:
        self._inc(self.llm_route_total, 1, scene or "default", model or "unknown")

    def record_llm_error(self, model: str, scene: str, error_type: str) -> None:
        self._inc(self.llm_errors_total, 1, model or "unknown", scene or "default", error_type or "error")

    def record_node_duration(self, node: str, duration_seconds: float) -> None:
        self._observe(self.node_duration_seconds, duration_seconds, node)

    def set_rag_hit_ratio(self, ratio: float) -> None:
        self._set(self.rag_hit_ratio, min(1.0, max(0.0, float(ratio))))

    def load_persistent_rag_metrics(self) -> None:
        """Load the shared RAG counters from PostgreSQL after schema init.

        Metrics are intentionally best-effort: a metrics failure must never
        make the chat endpoint fail.  A missing/unavailable database simply
        leaves the collector in its local in-process mode.
        """
        with self._rag_lock:
            if self._rag_persistence_loaded:
                return
            self._rag_persistence_loaded = True
        try:
            from agent.runtime_db import connect, is_postgres_available
            if not is_postgres_available():
                return
            with connect() as conn:
                row = conn.execute(
                    "SELECT queries, hits FROM rag_metric_state "
                    "WHERE metric_name = 'retrieval'"
                ).fetchone()
            total = int((row or {}).get("queries") or 0)
            hits = int((row or {}).get("hits") or 0)
            with self._rag_lock:
                self._rag_total = max(0, total)
                self._rag_hits = max(0, min(hits, self._rag_total))
            if total:
                self._inc(self.rag_queries_total, total)
            if hits:
                self._inc(self.rag_hits_total, hits)
            self.set_rag_hit_ratio((hits / total) if total else 0.0)
        except Exception as exc:  # pragma: no cover - deployment dependent
            logger.debug("persistent RAG metrics unavailable: %s", exc)

    def _persist_rag_query(self, hit: bool):
        try:
            from agent.runtime_db import connect, is_postgres_available
            if not is_postgres_available():
                return None
            with connect() as conn:
                row = conn.execute(
                    """INSERT INTO rag_metric_state(metric_name, queries, hits)
                       VALUES ('retrieval', 1, %s)
                       ON CONFLICT(metric_name) DO UPDATE SET
                         queries = rag_metric_state.queries + 1,
                         hits = rag_metric_state.hits + EXCLUDED.hits,
                         updated_at = NOW()
                       RETURNING queries, hits""",
                    (1 if hit else 0,),
                ).fetchone()
            return int(row["queries"]), int(row["hits"])
        except Exception as exc:  # pragma: no cover - deployment dependent
            logger.debug("persistent RAG metrics write failed: %s", exc)
            return None

    def record_rag_query(self, hit: bool) -> None:
        """Record a retrieval and expose a restart-safe cumulative hit ratio.

        ``rag_queries_total`` and ``rag_hits_total`` remain Prometheus
        counters.  ``rag_hit_ratio`` is based on the shared PostgreSQL totals
        when PostgreSQL is available, and falls back to the local bounded
        window for hermetic/unit-test environments.
        """
        hit = bool(hit)
        persisted = self._persist_rag_query(hit)
        if persisted is not None:
            total, hits = persisted
            self._inc(self.rag_queries_total, 1)
            if hit:
                self._inc(self.rag_hits_total, 1)
            with self._rag_lock:
                self._rag_total, self._rag_hits = total, hits
            self.set_rag_hit_ratio((hits / total) if total else 0.0)
            return

        self._inc(self.rag_queries_total, 1)
        if hit:
            self._inc(self.rag_hits_total, 1)
        with self._rag_lock:
            self._rag_total += 1
            self._rag_hits += int(hit)
            self._rag_window.append(hit)
            ratio = sum(self._rag_window) / len(self._rag_window)
        self.set_rag_hit_ratio(ratio)

    def record_rate_limit_event(self, tier: str = "default") -> None:
        self._inc(self.rate_limit_events_total, 1, tier)

    def set_circuit_breaker_state(self, name: str, state) -> None:
        """state 可为 int(0/1/2) 或字符串 'closed'/'half_open'/'open'。"""
        mapping = {"closed": 0, "half_open": 1, "half-open": 1, "open": 2}
        if isinstance(state, str):
            state = mapping.get(state.lower(), 0)
        self._set(self.circuit_breaker_state, float(state), name)

    def record_cache_event(self, cache: str, result: str) -> None:
        """result: 'hit' | 'miss' | 'error' | 'set'"""
        self._inc(self.cache_events_total, 1, cache, result)

    def record_feedback(self, kind: str) -> None:
        """kind: 'thumbs_up' | 'thumbs_down' | 'rating' ..."""
        self._inc(self.feedback_events_total, 1, kind)

    # ── 输出 ──
    def render(self) -> Tuple[str, str]:
        """返回 (prometheus 文本, content_type), 供 /api/metrics 端点使用。"""
        if self.using_prometheus_client:
            return _prom_generate_latest(self._registry).decode("utf-8"), _PROM_CONTENT_TYPE
        return self._registry.render_text(), FALLBACK_CONTENT_TYPE


class MetricsCollector:
    """Backward-compatible local collector for the pre-P3 test/API contract."""

    def __init__(self):
        self.request_counts: Dict[str, int] = {}
        self.error_counts: Dict[str, int] = {}
        self._latencies: Dict[str, List[float]] = {}
        self._user_stats: Dict[str, Dict[str, int]] = {}
        self._rate_limit_events: List[Dict[str, str]] = []
        self._scene_stats: Dict[str, int] = {}

    def record_request(self, endpoint: str, status: int, duration_ms: float,
                       user_key: Optional[str] = None,
                       tenant_id: Optional[str] = None) -> None:
        self.request_counts[endpoint] = self.request_counts.get(endpoint, 0) + 1
        if int(status) >= 500:
            self.error_counts[endpoint] = self.error_counts.get(endpoint, 0) + 1
        self._latencies.setdefault(endpoint, []).append(float(duration_ms))
        if user_key:
            key = f"{tenant_id or 'default'}:{user_key}"
            stats = self._user_stats.setdefault(key, {"total_requests": 0, "errors": 0})
            stats["total_requests"] += 1
            stats["errors"] += int(int(status) >= 500)

    def get_user_stats(self) -> Dict[str, Dict[str, int]]:
        return dict(self._user_stats)

    def record_rate_limit(self, user_key: str, tenant_id: str = "") -> None:
        self._rate_limit_events.append({"user_key": user_key, "tenant_id": tenant_id})

    def get_rate_limit_stats(self) -> Dict[str, int]:
        return {"total_rate_limited": len(self._rate_limit_events),
                "recent_events_count": len(self._rate_limit_events)}

    def record_scene_request(self, scene: str, status: int, duration_ms: float) -> None:
        self._scene_stats[scene] = self._scene_stats.get(scene, 0) + 1

    def get_scene_stats(self) -> Dict[str, int]:
        return dict(self._scene_stats)

    def get_metrics(self) -> str:
        lines = ["# TYPE http_requests_total counter"]
        for endpoint, count in sorted(self.request_counts.items()):
            errors = self.error_counts.get(endpoint, 0)
            values = sorted(self._latencies.get(endpoint, []))
            lines.append(f'http_requests_total{{endpoint="{endpoint}"}} {count}')
            lines.append(f'http_error_rate{{endpoint="{endpoint}"}} '
                         f'{errors / count if count else 0.0}')
            if values:
                for quantile, index in (("0.50", 0.50), ("0.95", 0.95), ("0.99", 0.99)):
                    pos = min(len(values) - 1, int(index * (len(values) - 1)))
                    lines.append(f'http_request_duration_ms{{endpoint="{endpoint}",quantile="{quantile}"}} {values[pos]}')
        lines.append("process_uptime_seconds 0")
        return "\n".join(lines) + "\n"

# ── 全局单例 + 模块级便捷函数 ─────────────────────────────────
metrics = Metrics()
record_http_request = metrics.record_http_request
record_llm_tokens = metrics.record_llm_tokens
record_llm_cost = metrics.record_llm_cost
record_llm_cost_attribution = metrics.record_llm_cost_attribution
record_llm_request = metrics.record_llm_request
record_node_duration = metrics.record_node_duration
set_rag_hit_ratio = metrics.set_rag_hit_ratio
record_rag_query = metrics.record_rag_query
record_rate_limit_event = metrics.record_rate_limit_event
set_circuit_breaker_state = metrics.set_circuit_breaker_state
record_cache_event = metrics.record_cache_event
record_feedback = metrics.record_feedback
render_metrics = metrics.render

__all__ = [
    "MetricsCollector", "metrics", "DEFAULT_BUCKETS",
    "FallbackCounter", "FallbackGauge", "FallbackHistogram", "FallbackRegistry",
    "record_http_request", "record_llm_tokens", "record_llm_cost",
    "record_llm_cost_attribution", "record_llm_request",
    "record_node_duration", "set_rag_hit_ratio",
    "record_rag_query",
    "record_rate_limit_event", "set_circuit_breaker_state",
    "record_cache_event", "record_feedback", "render_metrics",
]
>>>>>>> origin/master
