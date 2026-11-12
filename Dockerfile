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
# Dependency Installation
# Copies package files and installs DAGER + runtime dependencies.
# ==============================================================================
COPY pyproject.toml README.md main.py ./
COPY dager ./dager
COPY flows ./flows

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e . && \
    mkdir -p /app/logs && chmod -R 777 /app/logs

# ==============================================================================
# Default Web Entrypoint (Hugging Face Spaces uses port 7860)
# ==============================================================================
EXPOSE 7860

CMD ["uvicorn", "dager.web.api:app", "--host", "0.0.0.0", "--port", "7860"]

