# monitoring/

- prometheus.yml：Prometheus 抓取后端 /api/metrics?format=prometheus。
- alerts.yml：错误率、延迟、限流等告警规则。
- grafana/：Grafana 数据源、Dashboard 和自动加载配置。

应用侧指标和 Trace 源码在 agent/metrics.py、agent/observability.py、agent/otel_setup.py。浏览器访问 /api/metrics 时需要后端运行；Prometheus 使用 format=prometheus。
