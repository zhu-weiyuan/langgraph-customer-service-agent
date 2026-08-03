# frontend/src/stores/

- chat.ts：业务状态核心。研究消息如何先显示、如何追加 SSE delta、结束后如何重新读取服务端会话。
- ui.ts：主题、登录、侧栏和界面偏好。

遇到“刷新消息消失”问题时，先看 chat.ts 的发送/结束/恢复流程，再对照后端 app_fastapi.py 和 agent/runtime_db.py。
