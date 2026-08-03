# frontend/src/

Vue 3 前端源码，按“页面 -> 状态 -> API -> 组件”阅读。

- App.vue：主页面布局和模块组合。
- main.ts：Vue/Pinia 应用入口。
- api/client.ts：后端类型、HTTP 封装和 SSE 解析。
- stores/chat.ts：会话、消息、流式增量、刷新恢复、反馈和记忆。
- stores/ui.ts：主题、登录和界面状态。
- components/：聊天、会话侧栏、记忆和观测面板组件。
- utils/markdown.ts：Markdown 渲染和安全处理。
- styles.css：全局样式。
