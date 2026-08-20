# LangGraph 客服 Agent 系统设计说明

## 1. 系统目标

把一个“直接调用大模型回复用户”的 Demo，升级成具备基本工程属性的 AI 客服系统：

- 可编排
- 可检索
- 可观测
- 可防护
- 可评测
- 可前后端分离交付

## 2. 系统分层

```text
[Vue3 Frontend]
    ↓
[HTTP / SSE API Layer]
    ↓
[LangGraph Workflow Layer]
    ↓
[RAG / Memory / Redis / SQLite]
    ↓
[LLM Gateway]
    ↓
[Security / Metrics / Evaluation]
```

## 3. 各层职责

### 3.1 前端层（Vue3 + Vite）
- 聊天工作台
- 会话列表
- 满意度评分
- 基础分析面板
- 与后端 API 解耦

### 3.2 API 层
- 提供 `/api/chat`、`/api/session/:id`、`/api/sessions`、`/api/analytics`、`/api/rating` 等接口
- 支持 JSON 和 SSE 流式输出
- 负责输入校验、返回格式、基础限流

### 3.3 编排层（LangGraph）
- 按节点拆分意图识别、回复生成、满意度检查、重试、人工升级
- 通过条件边管理分支和重试逻辑

### 3.4 知识与状态层
- RAG：问题到知识库检索
- Redis：缓存、热点问题、在线用户、限流辅助
- SQLite：会话历史、评分、反馈、trace 持久化

### 3.5 LLM Gateway
- 统一模型调用入口
- 处理路由、fallback、token 预算、日志、成本和延迟统计

### 3.6 安全与观测
- Prompt Injection Guard
- PII 扫描/脱敏
- 指标监控、trace、健康检查、评分收集

## 4. 关键设计取舍

### 为什么不是纯 Prompt + if/else？
因为一旦涉及多轮状态、满意度回路、人工升级、检索重试和观测埋点，纯 if/else 会迅速变得难维护。LangGraph 能把流程结构显式化。

### 为什么要 Hybrid / Agentic RAG？
单一检索方式对中文口语化和同义表达不稳。Hybrid RAG 提高召回鲁棒性；Agentic RAG 在复杂问题上进一步通过 query rewrite 和 sufficiency check 提高命中率。

### 为什么要前端分离？
因为面试官看项目时，不只看后端代码，还看你是否有完整交付思维。前后端分离后，项目更接近真实业务系统。

## 5. 仍可继续增强的方向

- ECharts 图表化 analytics
- 引用来源展示
- trace 可视化
- 更完整的 e2e 测试
- 登录体系/API Key 管理页
- 更细粒度的权限控制
