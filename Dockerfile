# syntax=docker/dockerfile:1
# Multi-stage build: dependencies cached separately, runtime runs as non-root.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# ---- dependency layer (rebuilt only when requirements change) ----
FROM base AS deps
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --default-timeout=120 --retries=5 -r /tmp/requirements.lock.txt

# ---- runtime ----
FROM base AS runtime
WORKDIR /app

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY agent ./agent
COPY config ./config
COPY knowledge ./knowledge
COPY migrations ./migrations
COPY templates ./templates
COPY static ./static
COPY scripts ./scripts
COPY app_fastapi.py main.py ./
COPY requirements.txt requirements.lock.txt ./

# Non-root user (secrets via env at runtime, never baked in)
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/logs /app/work /app/checkpoints \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4).status==200 else 1)"

CMD ["python", "app_fastapi.py"]
