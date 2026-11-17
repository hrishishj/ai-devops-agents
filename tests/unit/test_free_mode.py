from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from devopsagents.agents.review.workflow import ReviewAgent
from devopsagents.apps.cli.main import main as cli_main
from devopsagents.apps.cli.run_pr import render_summary, run_pr
from devopsagents.apps.container import NoBackend, build_container
from devopsagents.core.config.paths import resolve_data
from devopsagents.core.config.settings import Settings
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMGateway, ModelTier, TransientLLMError
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.client import GitHubClient
from devopsagents.integrations.llm.openai_compat import OpenAICompatBackend
from tests.unit.test_review import DIFF, FakeGH

OK = {
    "choices": [{"message": {"content": '{"a": 1}'}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    "model": "m",
}


def backend(handler, **kw):
    return OpenAICompatBackend(
        "http://llm.test/v1/",
        kw.pop("key", "k"),
        "big",
        "small",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def call(b, tier=ModelTier.LARGE):
    return b.complete(tier=tier, system="s", user="u", json_schema={}, max_tokens=50)


# ---- OpenAI-compatible adapter -----------------------------------------------------------------
def test_request_shape_auth_and_tier_model_selection():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json=OK)

    b = backend(handler)
    c = call(b, ModelTier.LARGE)
    call(b, ModelTier.SMALL)
    url, auth, body = seen[0]
    assert url == "http://llm.test/v1/chat/completions" and auth == "Bearer k"
    assert body["model"] == "big" and seen[1][2]["model"] == "small"
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert (c.content, c.prompt_tokens, c.completion_tokens) == ('{"a": 1}', 5, 2)


def test_no_auth_header_for_keyless_local_servers_and_json_mode_can_be_disabled():
    seen = []

    def handler(req):
        seen.append((req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json=OK)

    call(backend(handler, key="", json_mode=False))
    assert seen[0][0] is None and "response_format" not in seen[0][1]


def test_usage_is_estimated_when_the_server_omits_it():
    b = backend(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "x" * 40}}]}))
    c = call(b)
    assert c.prompt_tokens >= 0 and c.completion_tokens == 10


def test_rate_limits_and_server_errors_are_transient_with_retry_after():
    with pytest.raises(TransientLLMError) as e:
        call(backend(lambda r: httpx.Response(429, headers={"retry-after": "7"}, text="slow down")))
    assert e.value.retry_after == 7.0
    with pytest.raises(TransientLLMError):
        call(backend(lambda r: httpx.Response(503)))
    with pytest.raises(TransientLLMError):
        call(backend(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))))


def test_permanent_errors_fail_loudly_and_are_not_retried():
    with pytest.raises(RuntimeError, match="HTTP 401"):
        call(backend(lambda r: httpx.Response(401, text="bad key")))


class Out(BaseModel):
    a: int


def test_gateway_honours_retry_after_but_caps_it(store):
    sleeps = []
    b = ScriptedBackend(
        replies=[TransientLLMError("x", retry_after=30), TransientLLMError("y", retry_after=9999), {"a": 1}]
    )
    g = LLMGateway(b, store, sleep=sleeps.append, backoff_base=1.0)
    assert g.structured(tier=ModelTier.SMALL, system="s", user="u", response_model=Out, scope="o/r").a == 1
    assert sleeps == [30, 90]  # server hint wins over 1s/2s backoff; unbounded hints are capped


# ---- token auth + data paths --------------------------------------------------------------------
def test_github_token_mode_never_mints_an_installation_token():
    calls = []

    def handler(req):
        calls.append((req.url.path, req.headers["authorization"]))
        return httpx.Response(200, json={"title": "t", "head": {"sha": "h"}, "base": {"sha": "b"}})

    gh = GitHubClient(token="ghs_abc", transport=httpx.MockTransport(handler))
    gh.get_pr(0, "o/r", 1)
    assert calls == [("/repos/o/r/pulls/1", "Bearer ghs_abc")]


def test_resolve_data_prefers_local_then_packaged(tmp_path):
    base, pkg = tmp_path / "base", tmp_path / "pkg"
    (pkg / "prompts").mkdir(parents=True)
    (pkg / "policies").mkdir()
    (pkg / "policies" / "default.yaml").write_text("kill_switch: false")
    assert resolve_data(base, "prompts", packaged_root=pkg) == pkg / "prompts"
    assert resolve_data(base, "policies/default.yaml", packaged_root=pkg) == pkg / "policies" / "default.yaml"
    (base / "prompts").mkdir(parents=True)
    assert resolve_data(base, "prompts", packaged_root=pkg) == base / "prompts"  # local override wins


# ---- container provider selection ----------------------------------------------------------------
def test_default_provider_is_openai_compatible_and_azure_is_opt_in(repo_root):
    c = build_container(Settings(database_url="sqlite+pysqlite://"), base_dir=repo_root)
    assert isinstance(c.handlers[AgentKind.INCIDENT].__self__._llm._backend, OpenAICompatBackend)
    c2 = build_container(
        Settings(database_url="sqlite+pysqlite://", llm_provider="azure"), base_dir=repo_root
    )
    assert isinstance(c2.handlers[AgentKind.INCIDENT].__self__._llm._backend, NoBackend)


def test_token_only_config_registers_review_and_security(repo_root):
    s = Settings(database_url="sqlite+pysqlite://", github_token=SecretStr("t"))
    c = build_container(s, base_dir=repo_root)
    assert {AgentKind.REVIEW, AgentKind.SECURITY} <= set(c.handlers)


# ---- Actions mode end to end ----------------------------------------------------------------------
GOOD = {
    "candidates": [
        {
            "path": "app/db.py",
            "line": 11,
            "quote": 'f"SELECT',
            "severity": "high",
            "category": "security",
            "title": "SQL injection",
            "detail": "bad",
            "confidence": 0.9,
        }
    ]
}


def prepared(repo_root, gh):
    backend_ = ScriptedBackend(replies=[GOOD])
    c = build_container(
        Settings(database_url="sqlite+pysqlite://", github_token=SecretStr("t")),
        backend=backend_,
        base_dir=repo_root,
    )
    llm = LLMGateway(backend_, c.store, sleep=lambda s: None)
    c.github = gh
    c.handlers = {
        AgentKind.REVIEW: ReviewAgent(gh, llm, PromptRegistry(repo_root / "prompts"), c.store).handle
    }
    return c


def test_run_pr_runs_agents_and_summary_shows_shadow_findings(repo_root):
    gh = FakeGH(cfg=None)  # no .ai-agents.yml -> shadow mode: nothing posted, but visible in the summary
    runs = run_pr(prepared(repo_root, gh), "o/r", 7)
    assert [(r.agent, r.outcome) for r in runs] == [("review", "succeeded")] and gh.posted == []
    text = render_summary("o/r", 7, runs)
    assert "app/db.py:11" in text and "SQL injection" in text and "shadow" in text


def test_run_pr_requires_credentials(repo_root):
    c = build_container(Settings(database_url="sqlite+pysqlite://"), base_dir=repo_root)
    with pytest.raises(RuntimeError, match="DA_GITHUB_TOKEN"):
        run_pr(c, "o/r", 1)


def test_cli_run_pr_writes_the_step_summary(repo_root, tmp_path, monkeypatch):
    gh = FakeGH(cfg="review:\n  mode: comment\n")
    monkeypatch.setattr(
        "devopsagents.apps.container.build_container", lambda *a, **k: prepared(repo_root, gh)
    )
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))
    assert cli_main(["run-pr", "--repo", "o/r", "--pr", "7"]) == 0
    assert "AI agents" in out.read_text(encoding="utf-8") and len(gh.posted) == 1
    assert DIFF  # fixture sanity
