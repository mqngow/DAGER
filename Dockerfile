# ==============================================================================
# Base Runtime Image
# Lightweight Python 3.12 container for running DAGER Prefect flows or FastAPI.
# ==============================================================================
FROM python:3.12-slim

WORKDIR /app

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ==============================================================================
# Dependency Installation
# Installs core DAGER dependencies first to leverage Docker layer caching.
# ==============================================================================
COPY pyproject.toml README.md ./
COPY dager ./dager
COPY flows ./flows

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# ==============================================================================
# Default Entrypoint
# Runs the daily S&P 500 evaluation batch or serves the Prefect deployment.
# Override CMD in cloud platforms (e.g., `uvicorn dager.web.api:app --host 0.0.0.0 --port 8000`).
# ==============================================================================
CMD ["python", "-m", "flows.evaluation_flow", "--limit", "50"]

