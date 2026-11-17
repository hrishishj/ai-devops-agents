# One image, three entrypoints: api | worker | cli  (selected by the command)
FROM python:3.12-slim AS build
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install ".[azure]"

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Apply Debian security updates (the base image lags, e.g. zlib1g fixes).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
# Semgrep lives in its OWN virtualenv so its pinned dependencies (opentelemetry, pydantic, protobuf)
# can never clash with, or be overwritten by, the application's packages.
RUN python -m venv /opt/semgrep \
    && /opt/semgrep/bin/pip install --no-cache-dir --upgrade pip semgrep==1.185.0 \
    && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep \
    && groupadd -r agents && useradd -r -g agents -d /app agents
WORKDIR /app
COPY --from=build /install /usr/local
# The runtime never builds packages, so setuptools/pkg_resources/wheel are dead weight and have had
# HIGH CVEs (e.g. CVE-2025-47273). Remove every copy wherever it lives; log what is removed.
RUN find / -xdev \( -name 'setuptools' -o -name 'setuptools-*' -o -name 'pkg_resources' \
        -o -name 'wheel' -o -name 'wheel-*' -o -name '_distutils_hack' \) \
        -path '*-packages/*' -prune -print -exec rm -rf {} + \
    || true
# Runtime data the service reads: prompts, runbook registry, policy (reviewed like code).
COPY prompts ./prompts
COPY runbooks ./runbooks
COPY policies ./policies
USER agents
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"
# Default: ingress API. Workers override: ["devopsagents","worker","--agent","review"]
CMD ["devopsagents", "serve", "--port", "8000"]
