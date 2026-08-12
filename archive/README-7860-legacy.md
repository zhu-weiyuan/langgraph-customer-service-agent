# 7860 旧版前端归档说明

- 7860 原先承载的 Jinja2/原生 JavaScript 前端已经归档，不再作为默认前端页面提供。
- 旧版页面文件保留在 `archive/old_frontend_index.html`，未删除。
- 访问 `http://localhost:7860/` 会显示“旧版前端已归档”的提示页。
- 当前 Vue + Vite 前端地址是 `http://localhost:5173/`。
- 7860 继续运行 FastAPI 后端/API 服务，Vue 开发服务器通过代理访问 `/api/*` 和 `/healthz`。
- 如需查看旧版前端与新版前端的差异，请参阅 `archive/README.md`。

归档日期：2026 年 7 月 28 日。