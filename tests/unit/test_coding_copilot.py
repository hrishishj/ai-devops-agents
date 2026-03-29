"""
Unit tests for agents/coding_copilot/agent.py

All external dependencies (LLM, GitHub, Cosmos, Redis) are mocked.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agents.coding_copilot.agent import (
    CodingCopilotAgent,
    ReviewComment,
    ReviewResponse,
    _build_user_message,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    return llm


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_recent_reviews.return_value = []
    return store


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache.claim_idempotency_key.return_value = True
    return cache


@pytest.fixture
def agent(mock_llm, mock_store, mock_cache):
    """Return a CodingCopilotAgent with all dependencies mocked."""
    with patch("agents.coding_copilot.agent.get_llm_client", return_value=mock_llm), \
         patch("agents.coding_copilot.agent.get_cache", return_value=mock_cache), \
         patch("agents.coding_copilot.agent.CosmosMemoryStore", return_value=mock_store):
        a = CodingCopilotAgent()
        a._llm = mock_llm
        a._store = mock_store
        a._cache = mock_cache
        return a


@pytest.fixture
def sample_pr_context():
    from shared.github_client import PullRequestContext
    return PullRequestContext(
        repo_full_name="org/repo",
        pr_number=42,
        base_sha="abc123",
        head_sha="def456",
        diff="diff --git a/auth.py b/auth.py\n+++ b/auth.py\n+    token = request.headers['Authorization']",
        changed_files=["auth.py"],
        title="Fix auth token refresh",
        body="Improves token handling",
    )


def _make_valid_review_json() -> str:
    return json.dumps({
        "summary": "Good overall structure with one security concern.",
        "overall_quality": "needs_work",
        "comments": [
            {
                "path": "auth.py",
                "line": 1,
                "severity": "critical",
                "category": "security",
                "body": "SQL injection risk via string formatting.",
                "suggestion": "Use parameterised queries.",
            }
        ],
    })


# --------------------------------------------------------------------------- #
# ReviewComment validation
# --------------------------------------------------------------------------- #

class TestReviewComment:
    def test_valid_comment(self):
        c = ReviewComment(
            path="main.py",
            line=10,
            severity="warning",
            category="perf",
            body="Consider caching this.",
        )
        assert c.severity == "warning"
        assert c.line == 10

    def test_invalid_severity_raises(self):
        with pytest.raises(Exception):
            ReviewComment(
                path="main.py", line=1, severity="blocker",
                category="bug", body="test"
            )

    def test_zero_line_raises(self):
        with pytest.raises(Exception):
            ReviewComment(
                path="main.py", line=0, severity="info",
                category="docs", body="test"
            )

    def test_negative_line_raises(self):
        with pytest.raises(Exception):
            ReviewComment(
                path="main.py", line=-5, severity="info",
                category="docs", body="test"
            )


# --------------------------------------------------------------------------- #
# ReviewResponse validation
# --------------------------------------------------------------------------- #

class TestReviewResponse:
    def test_valid_response(self):
        r = ReviewResponse(
            summary="All good.",
            overall_quality="good",
            comments=[],
        )
        assert r.overall_quality == "good"
        assert r.comments == []

    def test_invalid_quality_raises(self):
        with pytest.raises(Exception):
            ReviewResponse(summary="x", overall_quality="excellent", comments=[])

    def test_response_with_comments(self):
        r = ReviewResponse(
            summary="Found issues.",
            overall_quality="major_issues",
            comments=[
                ReviewComment(path="a.py", line=5, severity="critical",
                              category="security", body="XSS risk.")
            ],
        )
        assert len(r.comments) == 1
        assert r.comments[0].severity == "critical"


# --------------------------------------------------------------------------- #
# _parse_response
# --------------------------------------------------------------------------- #

class TestParseResponse:
    def test_valid_json(self, agent):
        raw = _make_valid_review_json()
        result = agent._parse_response(raw)
        assert result is not None
        assert result.overall_quality == "needs_work"
        assert len(result.comments) == 1

    def test_invalid_json_returns_none(self, agent):
        result = agent._parse_response("not json {{{")
        assert result is None

    def test_missing_field_returns_none(self, agent):
        bad = json.dumps({"summary": "ok"})  # missing overall_quality, comments
        result = agent._parse_response(bad)
        assert result is None

    def test_wrong_severity_returns_none(self, agent):
        data = json.loads(_make_valid_review_json())
        data["comments"][0]["severity"] = "blocker"
        result = agent._parse_response(json.dumps(data))
        assert result is None


# --------------------------------------------------------------------------- #
# _build_context
# --------------------------------------------------------------------------- #

class TestBuildContext:
    def test_includes_pr_title(self, agent, sample_pr_context):
        ctx = agent._build_context(sample_pr_context)
        assert ctx["pr_title"] == "Fix auth token refresh"

    def test_includes_changed_files(self, agent, sample_pr_context):
        ctx = agent._build_context(sample_pr_context)
        assert "auth.py" in ctx["changed_files"]

    def test_diff_truncated_at_12000_chars(self, agent, sample_pr_context):
        sample_pr_context.diff = "+" + "x" * 20_000
        ctx = agent._build_context(sample_pr_context)
        assert len(ctx["diff"]) <= 12_000

    def test_historical_reviews_fetched(self, agent, sample_pr_context, mock_store):
        mock_store.get_recent_reviews.return_value = [
            {"pr_number": 41, "summary": "ok", "overall_quality": "good"}
        ]
        ctx = agent._build_context(sample_pr_context)
        assert len(ctx["historical_reviews"]) == 1
        assert ctx["historical_reviews"][0]["pr_number"] == 41


# --------------------------------------------------------------------------- #
# handle_pr — idempotency
# --------------------------------------------------------------------------- #

class TestHandlePrIdempotency:
    def test_duplicate_skipped(self, agent, mock_cache):
        mock_cache.claim_idempotency_key.return_value = False
        with patch("agents.coding_copilot.agent.fetch_pr_context") as mock_fetch:
            agent.handle_pr("org/repo", 42)
            mock_fetch.assert_not_called()

    def test_first_call_proceeds(self, agent, mock_cache, mock_llm, sample_pr_context):
        mock_cache.claim_idempotency_key.return_value = True
        mock_llm.complete.return_value = MagicMock(content=_make_valid_review_json())

        with patch("agents.coding_copilot.agent.fetch_pr_context", return_value=sample_pr_context), \
             patch("agents.coding_copilot.agent.post_review_comments"):
            agent.handle_pr("org/repo", 42)
            mock_llm.complete.assert_called_once()


# --------------------------------------------------------------------------- #
# _build_user_message
# --------------------------------------------------------------------------- #

class TestBuildUserMessage:
    def test_contains_title(self):
        ctx = {
            "pr_title": "My Feature",
            "pr_body": "A description",
            "changed_files": ["foo.py"],
            "diff": "+x = 1",
            "historical_reviews": [],
        }
        msg = _build_user_message(ctx)
        assert "My Feature" in msg

    def test_contains_diff(self):
        ctx = {
            "pr_title": "T",
            "pr_body": "",
            "changed_files": [],
            "diff": "+def hello(): pass",
            "historical_reviews": [],
        }
        msg = _build_user_message(ctx)
        assert "+def hello(): pass" in msg

    def test_no_description_fallback(self):
        ctx = {
            "pr_title": "T",
            "pr_body": "",
            "changed_files": [],
            "diff": "",
            "historical_reviews": [],
        }
        msg = _build_user_message(ctx)
        assert "(no description)" in msg

    def test_historical_included_when_present(self):
        ctx = {
            "pr_title": "T",
            "pr_body": "",
            "changed_files": [],
            "diff": "",
            "historical_reviews": [{"pr_number": 1, "summary": "past", "overall_quality": "good"}],
        }
        msg = _build_user_message(ctx)
        assert "Historical Review Context" in msg
