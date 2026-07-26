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
