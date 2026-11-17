# Minimal image for running generated tests (see DockerSandbox). No network, no secrets, no app code:
# just Python and pytest. Build:  docker build -t devopsagents-sandbox infra/sandbox
FROM python:3.15-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && pip install pytest==9.2.0 \
    && pip uninstall -y pip
USER 65534:65534
WORKDIR /tmp
