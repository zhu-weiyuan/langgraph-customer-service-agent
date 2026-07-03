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
        self.start_time = time.time()
    
    def record_request(self, endpoint: str, status_code: int, duration_ms: float):
        """Record a request."""
        self.request_counts[endpoint] += 1
        
        if status_code >= 400:
            self.error_counts[endpoint] += 1
        
        times = self.response_times[endpoint]
        times.append(duration_ms)
        if len(times) > 100:
            self.response_times[endpoint] = times[-100:]
    
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


metrics = MetricsCollector()
