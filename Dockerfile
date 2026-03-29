# ============================================================
# Multi-stage Dockerfile for AI DevOps Agents
# Python 3.12 — Azure Container Apps optimised
# ============================================================

# ─── Stage 1: dependency builder ────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools for packages with C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─── Stage 2: production image ──────────────────────────────
FROM python:3.12-slim AS production

# Security: run as non-root
RUN groupadd -r agents && useradd -r -g agents agents

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY agents/       ./agents/
COPY api/          ./api/
COPY shared/       ./shared/
COPY config/       ./config/

# Create log directory owned by app user
RUN mkdir -p /app/logs && chown -R agents:agents /app

USER agents

# Health check — used by Container Apps liveness probe
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

# Default: run the webhook API
# Override CMD for the consumer: ["python", "-m", "api.service_bus_consumer"]
EXPOSE 8000
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info", \
     "--access-log"]


# ─── Stage 3: development / test image ──────────────────────
FROM production AS development

USER root

# Install dev/test extras
RUN pip install --no-cache-dir \
    pytest==8.2.0 \
    pytest-asyncio==0.23.7 \
    pytest-cov==5.0.0 \
    pytest-mock==3.14.0 \
    httpx==0.27.0 \
    ruff==0.4.4 \
    mypy==1.10.0

COPY tests/ ./tests/

USER agents
CMD ["pytest", "tests/unit/", "-v", "--tb=short"]
