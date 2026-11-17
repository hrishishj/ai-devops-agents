# One image, three entrypoints: api | worker | cli  (selected by the command)
FROM python:3.12-slim AS build
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install ".[azure]"

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir semgrep==1.86.0 \
    && groupadd -r agents && useradd -r -g agents -d /app agents
WORKDIR /app
COPY --from=build /install /usr/local
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
