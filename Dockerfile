FROM python:3.11-slim

WORKDIR /app

# Install PostgreSQL client libraries for psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=120 --retries=5 -r requirements.txt

COPY . .

EXPOSE 7860

CMD ["python", "app_fastapi.py"]
