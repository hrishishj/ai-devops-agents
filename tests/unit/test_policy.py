from datetime import UTC, datetime, timedelta

import pytest

from devopsagents.core.policy.engine import FreezeWindow, PolicyConfig, PolicyEngine
from devopsagents.core.policy.runbooks import RunbookRegistry
from devopsagents.domain.actions import ActionRequest, Decision

GOOD = {"service_type": "ContainerApp", "service_name": "api", "resource_group": "rg1"}


@pytest.fixture
def registry(repo_root):
    return RunbookRegistry.load(repo_root / "runbooks")


def req(**kw):
    base = dict(
        runbook="restart-service",
        params=GOOD,
        environment="staging",
        target="res1",
        incident_id="i1",
        model_confidence=0.95,
    )
    base.update(kw)
    return ActionRequest(**base)


def engine(store, registry, **cfg):
    return PolicyEngine(PolicyConfig(**cfg), registry, store)


def test_registry_loads_all_runbooks(registry):
    assert {"restart-service", "scale-out", "scale-in", "clear-disk", "failover-db"} <= set(registry.names())


def test_tier1_in_auto_env_with_evidence_is_allowed(store, registry):
    r = engine(store, registry).evaluate(req(), evidence=["kql:cpu>95"])
    assert r.decision == Decision.ALLOW


def test_no_evidence_is_denied(store, registry):
    assert engine(store, registry).evaluate(req(), evidence=[]).decision == Decision.DENY


def test_unknown_runbook_denied(store, registry):
    r = engine(store, registry).evaluate(req(runbook="rm-rf"), evidence=["e"])
    assert r.decision == Decision.DENY and "unknown runbook" in r.reasons[0]


def test_invalid_or_extra_params_denied(store, registry):
    bad = engine(store, registry).evaluate(
        req(params={**GOOD, "service_name": "a; rm -rf /"}), evidence=["e"]
    )
    extra = engine(store, registry).evaluate(req(params={**GOOD, "evil": 1}), evidence=["e"])
    missing = engine(store, registry).evaluate(req(params={}), evidence=["e"])
    assert bad.decision == extra.decision == missing.decision == Decision.DENY


def test_prod_requires_approval_until_enabled(store, registry):
    e = engine(store, registry)
    assert e.evaluate(req(environment="prod"), evidence=["e"]).decision == Decision.REQUIRE_APPROVAL
    e2 = engine(store, registry, auto_environments=["dev", "staging", "prod"])
    assert e2.evaluate(req(environment="prod"), evidence=["e"]).decision == Decision.ALLOW


def test_stateful_always_needs_human_even_if_env_auto(store, registry):
    e = engine(store, registry, auto_environments=["prod", "staging"])
    r = e.evaluate(
        req(
            runbook="failover-db",
            params={"server_name": "db-prod", "resource_group": "rg"},
            environment="prod",
        ),
        evidence=["e"],
    )
    assert r.decision == Decision.REQUIRE_APPROVAL


def test_low_model_confidence_only_makes_it_stricter(store, registry):
    r = engine(store, registry).evaluate(req(model_confidence=0.3), evidence=["e"])
    assert r.decision == Decision.REQUIRE_APPROVAL
    # high confidence can never override a denial
    assert (
        engine(store, registry).evaluate(req(runbook="nope", model_confidence=1.0), evidence=["e"]).decision
        == Decision.DENY
    )


def test_cooldown_and_rate_limit(store, registry, clock):
    e = engine(store, registry)
    assert e.evaluate(req(), evidence=["e"]).decision == Decision.ALLOW
    store.audit("agent", "action.executed", "res1")
    assert e.evaluate(req(), evidence=["e"]).decision == Decision.DENY  # cooldown
    clock.advance(1000)  # past 900s cooldown
    assert e.evaluate(req(), evidence=["e"]).decision == Decision.ALLOW
    store.audit("agent", "action.executed", "res1")
    clock.advance(1000)
    store.audit("agent", "action.executed", "res1")
    clock.advance(1000)
    r = e.evaluate(req(), evidence=["e"])  # 2/hour limit for restart-service
    assert r.decision == Decision.DENY and "rate limit" in r.reasons[0]


def test_freeze_window_forces_approval(store, registry):
    now = datetime(2026, 12, 25, tzinfo=UTC)
    w = FreezeWindow(start=now - timedelta(days=1), end=now + timedelta(days=1))
    r = engine(store, registry, freeze_windows=[w]).evaluate(req(), evidence=["e"], now=now)
    assert r.decision == Decision.REQUIRE_APPROVAL


def test_kill_switch_denies_everything(store, registry):
    assert engine(store, registry, kill_switch=True).evaluate(req(), evidence=["e"]).decision == Decision.DENY


def test_environment_restriction(store, registry):
    r = engine(store, registry).evaluate(
        req(runbook="failover-db", params={"server_name": "db-1", "resource_group": "rg"}, environment="dev"),
        evidence=["e"],
    )
    assert r.decision == Decision.DENY
