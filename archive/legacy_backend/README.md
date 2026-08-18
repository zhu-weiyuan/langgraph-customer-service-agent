# Archived legacy backend

This directory preserves the three historical synchronous `http.server` entry
points for code-study and migration comparison only:

- `app_legacy.py`
- `app_sync_legacy.py`
- `app_original_sync_legacy.py`

They are **not supported runtime entry points**. The active backend is
`app_fastapi.py`, served with `uvicorn app_fastapi:app`; the active Vue frontend
is in `frontend/` and normally runs on port 5173.

Do not use archived modules for production startup, browser testing, database
migrations, or load testing. They are retained so historical code can be read
without making it easy to start the wrong server by accident.
