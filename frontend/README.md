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
