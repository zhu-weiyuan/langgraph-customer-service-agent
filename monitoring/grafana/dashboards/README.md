# monitoring/grafana/dashboards/

这里放 Grafana Dashboard JSON。当前核心面板是 agent-overview.json。

修改面板时，先确认字段来自 Prometheus 的实际指标名称，再用 /api/metrics?format=prometheus 验证数据。

