import pytest
from pydantic import BaseModel

from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import (
    BudgetExceeded,
    KillSwitchActive,
    LLMGateway,
    LLMOutputError,
    ModelTier,
    TransientLLMError,
)
from devopsagents.core.llm.redact import find_secrets, redact


class Out(BaseModel):
    answer: str


def gw(store, backend, **kw):
    return LLMGateway(backend, store, sleep=lambda s: None, **kw)


def ask(g, user="hi", scope="o/r", **kw):
    return g.structured(tier=ModelTier.SMALL, system="sys", user=user, response_model=Out, scope=scope, **kw)


def test_returns_validated_model_and_records_usage(store):
    g = gw(store, ScriptedBackend(replies=[{"answer": "a"}]))
    assert ask(g).answer == "a"
    assert store.usage_today("o/r") > 0 and store.usage_today("global") > 0


def test_repairs_invalid_output_once(store):
    b = ScriptedBackend(replies=["not json", {"answer": "fixed"}])
    assert ask(gw(store, b)).answer == "fixed"
    assert "invalid" in b.calls[1]["user"]


def test_fails_after_failed_repair(store):
    g = gw(store, ScriptedBackend(replies=["x", "y"]))
    with pytest.raises(LLMOutputError):
        ask(g)


def test_retries_transient_errors_with_backoff(store):
    sleeps = []
    b = ScriptedBackend(replies=[TransientLLMError("429"), TransientLLMError("429"), {"answer": "ok"}])
    g = LLMGateway(b, store, sleep=sleeps.append, backoff_base=1.0)
    assert ask(g).answer == "ok"
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_max_retries(store):
    b = ScriptedBackend(replies=[TransientLLMError("x")] * 3)
    g = LLMGateway(b, store, sleep=lambda s: None, max_retries=2)
    with pytest.raises(TransientLLMError):
        ask(g)


def test_budget_enforced_per_scope_and_global(store):
    store.record_usage("o/r", 1000)
    with pytest.raises(BudgetExceeded):
        ask(gw(store, ScriptedBackend(replies=[{"answer": "a"}]), per_scope_budget=1000))
    # other repos are unaffected
    assert (
        ask(
            gw(store, ScriptedBackend(replies=[{"answer": "a"}]), per_scope_budget=1000), scope="o/other"
        ).answer
        == "a"
    )
    store.record_usage("global", 10**7)
    with pytest.raises(BudgetExceeded):
        ask(gw(store, ScriptedBackend(replies=[{"answer": "a"}]), global_budget=10**6), scope="x")


def test_cache_avoids_second_backend_call(store):
    b = ScriptedBackend(replies=[{"answer": "a"}])
    g = gw(store, b)
    ask(g, prompt_id="p1")
    ask(g, prompt_id="p1")
    assert len(b.calls) == 1


def test_kill_switch(store):
    g = gw(store, ScriptedBackend(replies=[{"answer": "a"}]), kill_switch=lambda: True)
    with pytest.raises(KillSwitchActive):
        ask(g)


def test_secrets_are_redacted_before_leaving(store):
    b = ScriptedBackend(replies=[{"answer": "a"}])
    ask(gw(store, b), user="key=AKIAIOSFODNN7EXAMPLE and ghp_" + "a" * 30)
    sent = b.calls[0]["user"]
    assert "AKIAIOSFODNN7EXAMPLE" not in sent and "ghp_" not in sent


def test_output_cap_is_enforced(store):
    b = ScriptedBackend(replies=[{"answer": "a"}])
    ask(gw(store, b, max_output_tokens=100), max_tokens=99999)
    assert b.calls[0]["max_tokens"] == 100


def test_find_secrets_flags_and_ignores_placeholders():
    assert find_secrets('password = "S3cr3tValue99"')
    assert not find_secrets('password = "changeme-example"')
    assert find_secrets("AWS_KEY=AKIAIOSFODNN7EXAMPLE")
    assert find_secrets("-----BEGIN RSA PRIVATE KEY-----")
    assert redact("x AKIAIOSFODNN7EXAMPLE y") == "x [REDACTED] y"
