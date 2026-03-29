"""
Shared pytest configuration and fixtures.

Patches the most common external dependencies globally so unit tests
don't accidentally connect to real Azure services.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# --------------------------------------------------------------------------- #
# Set dummy environment variables BEFORE any application code is imported.
# This prevents pydantic-settings from failing on missing required fields.
# --------------------------------------------------------------------------- #

os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://dummy.openai.azure.com/")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "dummy-key-for-testing")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
os.environ.setdefault("COSMOS_ENDPOINT", "https://dummy.documents.azure.com:443/")
os.environ.setdefault("COSMOS_KEY", "dummykey==")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PASSWORD", "dummy")
os.environ.setdefault("REDIS_SSL", "false")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("GITHUB_TOKEN", "ghp_dummy_token")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("SERVICEBUS_CONNECTION_STRING", "Endpoint=sb://dummy.servicebus.windows.net/;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=dummy=")
os.environ.setdefault("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
os.environ.setdefault("OTEL_SERVICE_NAME", "test")


# --------------------------------------------------------------------------- #
# Auto-patch OpenTelemetry so it doesn't try to connect to Azure Monitor
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def patch_telemetry(monkeypatch):
    """Prevent real OTel exporters from being initialised during tests."""
    with patch("shared.telemetry.init_telemetry"), \
         patch("shared.telemetry.get_tracer", return_value=MagicMock(
             start_as_current_span=lambda *a, **kw: _noop_span_ctx()
         )):
        yield


class _NoopSpan:
    """Context manager that does nothing — replaces real OTel spans."""
    def __enter__(self):
        span = MagicMock()
        span.set_attribute = MagicMock()
        return span

    def __exit__(self, *args):
        pass


def _noop_span_ctx():
    return _NoopSpan()


# --------------------------------------------------------------------------- #
# Shared mock factories
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_llm_response():
    """Factory for creating mock LLM responses."""
    def _make(content: str):
        resp = MagicMock()
        resp.content = content
        resp.prompt_tokens = 100
        resp.completion_tokens = 200
        resp.total_tokens = 300
        resp.model = "gpt-4o"
        resp.finish_reason = "stop"
        resp.latency_ms = 500.0
        return resp
    return _make


@pytest.fixture
def mock_pr_context():
    """A reusable PullRequestContext for testing."""
    from shared.github_client import PullRequestContext
    return PullRequestContext(
        repo_full_name="test-org/test-repo",
        pr_number=1,
        base_sha="base000",
        head_sha="head111",
        diff=(
            "diff --git a/app.py b/app.py\n"
            "+++ b/app.py\n"
            "+def new_function():\n"
            "+    password = 'hardcoded123'\n"
            "+    return eval(user_input)\n"
        ),
        changed_files=["app.py"],
        title="Add new feature",
        body="This PR adds a new function.",
    )
