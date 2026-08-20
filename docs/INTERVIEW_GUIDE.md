# LangGraph 客服 Agent 面试讲解手册

## 1. 项目定位

这是一个面向售后客服场景的 AI Agent 工程项目，不只是对话 Demo，而是把 **LangGraph 编排、Hybrid RAG、Agentic RAG、LLM Gateway、安全治理、可观测、评测** 串成了一个完整链路。近期又补了 **Vue3 + Vite 前端工作台**，形成了前后端分离结构。

## 2. 一分钟自我讲解版

我做的这个项目核心是把 AI 客服从“模型直出”升级成“可控系统”。后端用 LangGraph 管理状态流转，知识问答用 Hybrid RAG 和 Agentic RAG 提升召回，模型调用通过 Gateway 做路由和 fallback，同时补了 Prompt 注入防护、PII 脱敏、限流、日志指标和基础评测。前端我用 Vue3 + Vite 补了聊天工作台、会话列表、满意度评分和分析面板，让它更接近真实业务交付形态。

## 3. 架构怎么讲

```text
Vue3 Frontend
   ↓
HTTP / SSE API
   ↓
LangGraph Workflow
   ↓
RAG / Agentic RAG / Memory / Redis / SQLite
   ↓
LLM Gateway
```

### 节点流转
- identify_intent
- generate_reply
- check_satisfaction
- process_satisfaction
- finalize / escalate_to_human

## 4. 你做了什么

### 对话编排
- 用 LangGraph 把多轮客服流程拆成节点和条件边。
- 支持满意度检测，不满意可重试，多次失败后升级人工处理。

### RAG
- 基础检索不是单一向量检索，而是 Hybrid RAG：关键词 + 语义检索。
- 对复杂问题增加 Agentic RAG：query rewrite → retrieve → sufficiency check → retry。
- 目的：应对用户表达口语化、模糊化、同义改写后的召回下降。

### 安全治理
- 输入先经过 Prompt Injection Guard。
- 对消息做 PII 扫描和脱敏日志。
- 加 IP 维度限流和 Redis 辅助限频。

### 模型调用治理
- LLM Gateway 做模型路由、fallback、token 成本和延迟统计。
- 这是工程化重点，不让业务代码直接散落调用模型。

### 前端工作台
- 新增 Vue3 + Vite 前端。
- 支持会话列表、聊天工作台、评分面板、分析侧栏。
- 通过 `/api/chat` 和 `/api/*` 接口与后端解耦。

## 5. 高频追问与回答

### Q1：为什么用 LangGraph，不自己写 if/else？
因为客服流程是多状态、多分支、可重试的。if/else 在 demo 时够用，但一旦加上满意度重试、人工升级、记忆恢复、日志追踪，就会越来越难维护。LangGraph 的好处是节点职责清晰、边路由显式、状态可恢复，后面加节点也更自然。

### Q2：Agentic RAG 比普通 RAG 强在哪？
普通 RAG 通常是一次 query 一次 retrieve。Agentic RAG 会先改写问题，再判断检索结果够不够，不够再查。这样对模糊表达、同义表达、长尾问题效果更稳，但代价是多一次模型调用，所以我限制了最大轮数。

### Q3：你怎么排查 RAG 答非所问？
我按四层排查：文档是否被切对了、正确文档是否进候选集、候选排序是否合理、模型有没有忠实使用上下文。评测上看 HitRate@K、Recall@K、MRR、Coverage，再结合 bad case 分析。

### Q4：为什么还要做前端？
因为纯后端 demo 很难证明你真的理解业务交付。补了 Vue 前端后，项目变成完整的前后端分离 AI 应用，面试官更容易把它当成真实工程项目，而不是脚本集合。

### Q5：你觉得这个项目离生产还差什么？
还差更完整的权限体系、灰度发布、trace replay、真实业务数据闭环、模型效果线上监控，以及更系统的 integration / e2e 自动化测试。

## 6. 你要背住的关键词
- LangGraph 状态编排
- Hybrid RAG
- Agentic RAG
- LLM Gateway
- Prompt Injection Guard
- PII Redaction
- SSE Streaming
- Vue3 + Vite 前后端分离
- 评测闭环：HitRate / Recall / MRR / Coverage
