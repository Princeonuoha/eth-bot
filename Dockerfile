# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY app/ app/

# Create persistent directories
# data/ → trades.db and position store (mounted as Docker volume)
# logs/ → rotating log files (mounted as Docker volume)
RUN mkdir -p data logs \
 && useradd -m botuser \
 && chown -R botuser:botuser /app

USER botuser

# Health check — used by docker-compose depends_on
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import time, pathlib; age = time.time() - float(pathlib.Path('/app/data/heartbeat').read_text()); exit(0 if age < 60 else 1)"

CMD ["python", "-m", "app.main"]
