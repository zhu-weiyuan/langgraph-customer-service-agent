"""
Observability: 生产级可观测性层 (Trace + Metrics + Alerts)

功能：
1. TraceService - 分布式追踪（链路嵌套）
2. MetricsCollector - Prometheus 风格指标（计数、延迟直方图、错误率）
3. AlertService - 阈值告警（延迟/错误率超标自动通知）
"""

import json
import time
import threading
from typing import Any, Optional, Dict, List
from datetime import datetime


class TraceEvent:
    """单个追踪事件"""
    def __init__(self, event_type: str, data: dict, duration_ms: float = 0):
        self.event_type = event_type
        self.data = data
        self.duration_ms = duration_ms
        self.timestamp = datetime.now().isoformat()


class TraceSession:
    """一次请求的完整追踪"""
    def __init__(self, request_id: str, user_id: str, input_text: str):
        self.request_id = request_id
        self.user_id = user_id
        self.input_text = input_text[:500]  # 截断过长输入
        self.events: list[TraceEvent] = []
        self.start_time = time.time()
        self.total_latency_ms = 0

    def add_event(self, event_type: str, data: dict, duration_ms: float = 0):
        """添加追踪事件"""
        self.events.append(TraceEvent(event_type, data, duration_ms))

    def finalize(self):
        """完成追踪，计算总耗时"""
        self.total_latency_ms = (time.time() - self.start_time) * 1000

    def to_dict(self) -> dict:
        """转换为可序列化字典"""
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "input_text": self.input_text,
            "total_latency_ms": round(self.total_latency_ms, 2),
            "events": [
                {
                    "type": e.event_type,
                    "data": e.data,
                    "duration_ms": round(e.duration_ms, 2),
                    "timestamp": e.timestamp
                }
                for e in self.events
            ],
            "completed_at": datetime.now().isoformat()
        }


class TraceService:
    """追踪服务：将 trace 持久化到 SQLite"""

    def __init__(self, db_path: str = "agent/trace.db"):
        import sqlite3
        self.db_path = db_path
        # 初始化表结构
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                request_id TEXT PRIMARY KEY,
                user_id TEXT,
                input_text TEXT,
                total_latency_ms REAL,
                completed_at TEXT,
                trace_json TEXT  -- 完整事件列表存 JSON
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON traces(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_time ON traces(completed_at)")
        conn.commit()
        conn.close()

    def save_trace(self, trace: TraceSession):
        """保存追踪记录"""
        import sqlite3
        trace.finalize()
        data = trace.to_dict()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO traces VALUES (?, ?, ?, ?, ?, ?)",
            (trace.request_id, trace.user_id, trace.input_text,
             trace.total_latency_ms, data["completed_at"], json.dumps(data))
        )
        conn.commit()
        conn.close()

    def get_recent_traces(self, limit: int = 20) -> list[dict]:
        """获取最近的追踪记录"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT * FROM traces ORDER BY completed_at DESC LIMIT ?", (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(zip(["request_id", "user_id", "input_text", 
                         "total_latency_ms", "completed_at", "trace_json"], row))
                for row in rows]

    def get_trace_by_id(self, request_id: str) -> Optional[dict]:
        """根据 ID 获取完整追踪"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT * FROM traces WHERE request_id = ?", (request_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(zip(["request_id", "user_id", "input_text",
                            "total_latency_ms", "completed_at", "trace_json"], row))
        return None

    def get_stats(self) -> dict:
        """获取统计信息"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT COUNT(*), AVG(total_latency_ms) FROM traces")
        count, avg_latency = cursor.fetchone()
        conn.close()
        return {
            "total_requests": count or 0,
            "avg_latency_ms": round(avg_latency, 2) if avg_latency else 0
        }


# ── MetricsCollector: Prometheus 风格指标 ───────────────────────
class MetricsCollector:
    """
    生产级指标收集器（Prometheus 风格）。
    
    支持：Counter（计数）、Histogram（延迟直方图）、Gauge（瞬时值）
    所有指标带标签，可按 endpoint、status_code 等维度聚合。
    """

    def __init__(self):
        self._counters: Dict[str, int] = {}
        self._histograms: Dict[str, list] = {}  # bucket boundaries -> counts
        self._gauges: Dict[str, float] = {}
        self._lock = threading.Lock()
        
        # Histogram buckets (ms) — 常用延迟分布
        self._default_buckets = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

    def increment_counter(self, name: str, labels: Dict[str, str] = None) -> None:
        """增加计数器（如：请求数、错误数）"""
        key = self._build_key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    def observe_histogram(self, name: str, value_ms: float, 
                          labels: Dict[str, str] = None,
                          buckets: List[float] = None) -> None:
        """记录延迟观测值（自动分桶）"""
        key = self._build_key(name, labels)
        bucket_list = buckets or self._default_buckets
        
        with self._lock:
            if key not in self._histograms:
                # 初始化每个桶的计数 {bucket: count}
                self._histograms[key] = {b: 0 for b in bucket_list}
                self._histograms[key]['total'] = 0
                self._histograms[key]['sum'] = 0.0
            
            hist = self._histograms[key]
            hist['total'] += 1
            hist['sum'] += value_ms
            # 找到对应的桶（所有 >= 该值的桶都计数）
            for bucket in bucket_list:
                if value_ms <= bucket:
                    hist[bucket] += 1

    def set_gauge(self, name: str, value: float, labels: Dict[str, str] = None) -> None:
        """设置瞬时值（如：活跃连接数）"""
        key = self._build_key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def get_metrics(self) -> Dict[str, Any]:
        """获取所有指标（Prometheus 格式）"""
        with self._lock:
            metrics = {
                'counters': dict(self._counters),
                'histograms': {},
                'gauges': dict(self._gauges)
            }
            # 格式化直方图
            for key, hist in self._histograms.items():
                total = hist.get('total', 0)
                sum_val = hist.get('sum', 0.0)
                avg = sum_val / total if total > 0 else 0
                metrics['histograms'][key] = {
                    'count': total,
                    'sum_ms': round(sum_val, 2),
                    'avg_ms': round(avg, 2),
                    'buckets': {str(b): c for b, c in hist.items() 
                               if b not in ('total', 'sum')}
                }
            return metrics

    def get_prometheus_text(self) -> str:
        """生成 Prometheus 格式的文本输出（可直接暴露到 /metrics）"""
        lines = []
        for key, value in self._counters.items():
            lines.append(f"# TYPE {key} counter")
            lines.append(f"{key} {value}")
        
        for key, hist in self._histograms.items():
            lines.append(f"# TYPE {key} histogram")
            total = hist.get('total', 0)
            sum_val = hist.get('sum', 0.0)
            for bucket in sorted([b for b in hist.keys() if b not in ('total', 'sum')]):
                lines.append(f'{key}_bucket{{le="{bucket}"}} {hist[bucket]}')
            lines.append(f'{key}_bucket{{le="+Inf"}} {total}')
            lines.append(f'{key}_count {total}')
            lines.append(f'{key}_sum {sum_val:.2f}')
        
        for key, value in self._gauges.items():
            lines.append(f"# TYPE {key} gauge")
            lines.append(f"{key} {value}")
        
        return "\n".join(lines)

    def _build_key(self, name: str, labels: Dict[str, str] = None) -> str:
        """构建带标签的指标键"""
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"


# ── AlertService: 阈值告警 ───────────────────────────────────
class AlertService:
    """
    基于指标的自动告警。
    
    规则：当指标超过阈值时触发告警（带冷却时间避免轰炸）
    """

    def __init__(self, metrics: MetricsCollector):
        self.metrics = metrics
        self._alerts_triggered: Dict[str, float] = {}  # alert_name -> last_trigger_time
        self._rules = []

    def add_rule(self, name: str, metric_name: str, threshold: float,
                 window_seconds: int = 300, cooldown_seconds: int = 600) -> None:
        """
        添加告警规则。
        
        Args:
            name: 告警名称
            metric_name: 指标名称（如 'request_latency'）
            threshold: 阈值
            window_seconds: 统计窗口（秒）
            cooldown_seconds: 冷却时间，避免重复告警
        """
        self._rules.append({
            'name': name,
            'metric_name': metric_name,
            'threshold': threshold,
            'window': window_seconds,
            'cooldown': cooldown_seconds
        })

    def check_and_alert(self) -> List[str]:
        """
        检查所有规则，返回触发的告警列表。
        
        Returns:
            触发告警的名称列表
        """
        now = time.time()
        triggered = []
        
        for rule in self._rules:
            name = rule['name']
            # 检查冷却时间
            if name in self._alerts_triggered:
                elapsed = now - self._alerts_triggered[name]
                if elapsed < rule['cooldown']:
                    continue  # 还在冷却中
            
            # 获取指标（这里简化处理，实际应该基于最近 window_seconds 的数据）
            metrics = self.metrics.get_metrics()
            avg_latency = None
            for key, hist in metrics['histograms'].items():
                if rule['metric_name'] in key:
                    avg_latency = hist.get('avg_ms', 0)
                    break
            
            if avg_latency is not None and avg_latency > rule['threshold']:
                self._alerts_triggered[name] = now
                triggered.append(name)
                print(f"[Alert] {name}: avg_latency={avg_latency:.2f}ms > threshold={rule['threshold']}ms")
        
        return triggered
