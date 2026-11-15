# Saathi backend: FastAPI, SQLite on a mounted disk, background scans.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
	PYTHONPATH=/app/backend \
	DATA_DIR=/var/data \
	DATABASE_URL=sqlite:////var/data/saathi.db

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend

RUN mkdir -p /var/data
VOLUME ["/var/data"]

EXPOSE 8000

# An empty or stale database is seeded on startup by the app itself.
CMD ["sh", "-c", "uvicorn saathi.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
