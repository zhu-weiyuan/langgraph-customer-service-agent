"""
Prometheus-style metrics for customer service agent.

Provides /api/metrics endpoint with request counts, latencies, error rates.
"""

import time
from collections import defaultdict


class MetricsCollector:
    """Simple in-memory metrics collector."""
    
    def __init__(self):
        self.request_counts = defaultdict(int)
        self.response_times = defaultdict(list)
        self.error_counts = defaultdict(int)
        # Phase A enhancements: per-user/tenant tracking
        self.user_request_counts = defaultdict(lambda: {"total_requests": 0, "error_count": 0, "total_duration_ms": 0})
        # Scene tracking
        self.scene_request_counts = defaultdict(lambda: {"count": 0, "errors": 0, "total_duration_ms": 0})
        # Rate limit tracking
        self.rate_limit_events = []
        self.start_time = time.time()
    
    def record_request(self, endpoint: str, status_code: int, duration_ms: float, user_key: str = None, tenant_id: str = None):
        """Record a request (Phase A: supports per-user/tenant tracking)."""
        self.request_counts[endpoint] += 1
        
        if status_code >= 400:
            self.error_counts[endpoint] += 1
        
        times = self.response_times[endpoint]
        times.append(duration_ms)
        if len(times) > 100:
            self.response_times[endpoint] = times[-100:]
        
        # Per-user/tenant tracking
        if user_key and tenant_id:
            user_key_full = f"{tenant_id}:{user_key}"
            self.user_request_counts[user_key_full]["total_requests"] += 1
            self.user_request_counts[user_key_full]["total_duration_ms"] += duration_ms
            if status_code >= 400:
                self.user_request_counts[user_key_full]["error_count"] += 1
    
    def get_metrics(self) -> str:
        """Generate Prometheus-style metrics text."""
        lines = []
        
        for endpoint, count in self.request_counts.items():
            lines.append(f'# HELP http_requests_total Total HTTP requests')
            lines.append(f'# TYPE http_requests_total counter')
            lines.append(f'http_requests_total{{endpoint="{endpoint}"}} {count}')
        
        for endpoint, errors in self.error_counts.items():
            total = self.request_counts[endpoint]
            rate = errors / total if total > 0 else 0
            lines.append(f'# HELP http_error_rate Error rate by endpoint')
            lines.append(f'# TYPE http_error_rate gauge')
            lines.append(f'http_error_rate{{endpoint="{endpoint}"}} {rate:.4f}')
        
        for endpoint, times in self.response_times.items():
            if not times:
                continue
            sorted_times = sorted(times)
            p50 = sorted_times[len(sorted_times) // 2]
            p95 = sorted_times[int(len(sorted_times) * 0.95)]
            p99 = sorted_times[min(int(len(sorted_times) * 0.99), len(sorted_times)-1)]
            
            lines.append(f'# HELP http_request_duration_ms Request duration in milliseconds')
            lines.append(f'# TYPE http_request_duration_ms summary')
            lines.append(f'http_request_duration_ms{{endpoint="{endpoint}",quantile="0.50"}} {p50:.2f}')
            lines.append(f'http_request_duration_ms{{endpoint="{endpoint}",quantile="0.95"}} {p95:.2f}')
            lines.append(f'http_request_duration_ms{{endpoint="{endpoint}",quantile="0.99"}} {p99:.2f}')
        
        uptime = time.time() - self.start_time
        lines.append(f'# HELP process_uptime_seconds Process uptime in seconds')
        lines.append(f'# TYPE process_uptime_seconds gauge')
        lines.append(f'process_uptime_seconds {uptime:.2f}')
        
        return '\n'.join(lines)
    
    def get_user_stats(self):
        """Return per-user/tenant statistics."""
        return dict(self.user_request_counts)
    
    def record_rate_limit(self, user_key: str = None, tenant_id: str = None):
        """Record a rate limit event."""
        self.rate_limit_events.append({
            "timestamp": time.time(),
            "user_key": user_key,
            "tenant_id": tenant_id,
        })
        # Keep only last 1000 events
        if len(self.rate_limit_events) > 1000:
            self.rate_limit_events = self.rate_limit_events[-1000:]
    
    def get_rate_limit_stats(self):
        """Return rate limit statistics."""
        return {
            "total_rate_limited": len(self.rate_limit_events),
            "recent_events_count": len([e for e in self.rate_limit_events if time.time() - e["timestamp"] < 3600]),  # Last hour
        }
    
    def record_scene_request(self, scene: str, status_code: int, duration_ms: float):
        """Record a request by scene (for JavaGuide-style observability)."""
        self.scene_request_counts[scene]["count"] += 1
        self.scene_request_counts[scene]["total_duration_ms"] += duration_ms
        if status_code >= 400:
            self.scene_request_counts[scene]["errors"] += 1
    
    def get_scene_stats(self):
        """Return per-scene request counts."""
        return {scene: data["count"] for scene, data in self.scene_request_counts.items()}


metrics = MetricsCollector()
