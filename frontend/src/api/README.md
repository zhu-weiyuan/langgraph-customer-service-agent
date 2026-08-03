# frontend/src/api/

client.ts 是前端与后端的主要通信层。

重点看 sendChat()、streamChat() 的 POST + SSE 解析，以及 fetchSession() / fetchSessions() 的刷新恢复；再对照类型定义检查前后端字段一致性。
