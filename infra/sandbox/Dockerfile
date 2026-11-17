# Minimal image for running generated tests (see DockerSandbox). No network, no secrets, no app code:
# just Python and pytest. Build:  docker build -t devopsagents-sandbox infra/sandbox
# Keep this in step with the Python version of the repositories whose tests it runs (this project: 3.12).
# A newer interpreter can fail tests for reasons unrelated to the code under test.
FROM python:3.12-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && pip install pytest==9.2.0 \
    && pip uninstall -y pip
USER 65534:65534
WORKDIR /tmp
