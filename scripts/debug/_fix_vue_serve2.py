"""Re-apply Vue frontend serving and assets route to app_fastapi.py."""
content = open('/app/app_fastapi.py', encoding='utf-8').read()

# 1) Root route: serve Vue dist, fallback to legacy template
old_root = '''@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((ROOT / "templates" / "index.html")
                        .read_text(encoding="utf-8"))'''

new_root = '''@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the Vue-built frontend (Aster Support). Fallback to legacy template."""
    vue_index = ROOT / "frontend" / "dist" / "index.html"
    if vue_index.is_file():
        return HTMLResponse(vue_index.read_text(encoding="utf-8"))
    return HTMLResponse((ROOT / "templates" / "index.html")
                        .read_text(encoding="utf-8"))'''

if old_root in content:
    content = content.replace(old_root, new_root, 1)
    print('OK: root route updated')
else:
    print('FAIL: root route not found')

# 2) Add /assets/ route before /static/ route
old_static = '''@app.get("/static/{file_path:path}")
async def static_file(file_path: str):'''

new_assets = '''@app.get("/assets/{file_path:path}")
async def vue_assets(file_path: str):
    """Serve Vue frontend built assets (*.js, *.css)."""
    target = (ROOT / "frontend" / "dist" / "assets" / file_path).resolve()
    assets_root = (ROOT / "frontend" / "dist" / "assets").resolve()
    if not str(target).startswith(str(assets_root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/static/{file_path:path}")
async def static_file(file_path: str):'''

if old_static in content:
    content = content.replace(old_static, new_assets, 1)
    print('OK: /assets/ route added')
else:
    print('FAIL: /static/ route not found')

open('/app/app_fastapi.py', 'w', encoding='utf-8').write(content)
print('Done')
