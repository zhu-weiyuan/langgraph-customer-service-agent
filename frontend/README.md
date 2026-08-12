<<<<<<< HEAD
# frontend/：Vue 3 + Vite 前端

当前前端运行在 http://localhost:5173/，后端 FastAPI 运行在 http://localhost:7860/。Vite 将 /api/* 请求代理到后端。

## 启动和构建

    cd frontend
    npm install
    npm run dev
    npm run build

## 学习顺序

1. src/App.vue：主页面组合。
2. src/stores/chat.ts：消息、会话、SSE 流式和刷新恢复。
3. src/api/client.ts：API 类型、请求封装和 SSE 解析。
4. src/components/MessageList.vue、ChatInput.vue：聊天交互。
5. src/components/SessionSidebar.vue：会话列表和切换。
6. src/components/AnalyticsPanel.vue、MemoryPanel.vue：观测和长期记忆。
7. src/styles.css、vite.config.ts：样式和代理/构建配置。

旧的 7860 页面已经归档到 archive/，当前不要把 templates/ 或 static/ 当作 Vue 主前端。
=======
# Frontend (Vue 3 + Vite)

## 作用

给智能客服 Agent 补完整前端工程，支持：
- 聊天工作台
- 会话列表
- 满意度评分
- 基础分析面板

## 启动

```bash
cd frontend
npm install
npm run dev
```

默认通过 Vite 代理把 `/api/*` 请求转发到 `http://127.0.0.1:7860`。

## 目录

```text
src/
├── api/           # 后端 API 封装
├── components/    # 可复用组件
├── stores/        # Pinia 状态管理
├── App.vue        # 主工作台
├── main.ts
└── styles.css
```

## 后续建议

1. 增加登录/API Key 输入
2. 增加引用来源展示、Trace 查看
3. 把当前基础分析条形展示升级为 ECharts 图表
4. 增加会话搜索结果高亮与导出能力
>>>>>>> origin/master
