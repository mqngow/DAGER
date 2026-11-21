# ==============================================================================
# Base Runtime Image
# Lightweight Python 3.12 container configured for Hugging Face Spaces (port 7860)
# and cloud container deployments.
# ==============================================================================
FROM python:3.12-slim

WORKDIR /app

# Prevent Python from writing .pyc files, buffer logs, and point writable caches
# to `/tmp` so non-root cloud containers (like Hugging Face Spaces) never hit permission errors.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PREFECT_HOME=/tmp/.prefect \
    HF_HOME=/tmp/.cache/huggingface

# ==============================================================================
# Dependency Installation & Non-Root User Setup
# Installs pinned dependencies from `requirements.txt`, creates an unprivileged
# `appuser` account, and removes overly permissive `777` directory rights.
# ==============================================================================
COPY pyproject.toml README.md main.py requirements.txt ./
COPY dager ./dager
COPY flows ./flows

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps -e . && \
    addgroup --system appuser && \
    adduser --system --ingroup appuser appuser && \
    mkdir -p /app/logs && \
    chown -R appuser:appuser /app

USER appuser

# ==============================================================================
# Default Web Entrypoint
# Honors cloud-injected `$PORT` (Google Cloud Run = 8080, Render = $PORT, HF = 7860).
# ==============================================================================
EXPOSE 8080

CMD ["sh", "-c", "uvicorn dager.web.api:app --host 0.0.0.0 --port ${PORT:-8080}"]

