# monitoring/grafana/

- provisioning/datasources/：数据源配置。
- provisioning/dashboards/：Dashboard 自动加载配置。
- dashboards/agent-overview.json：Agent 观测面板定义。

面板的数据来源仍是应用 Prometheus 指标。面板显示 0 时，先检查应用是否产生指标，再检查 Prometheus 抓取和时间窗口。
