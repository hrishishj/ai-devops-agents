from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from devopsagents.apps.api.main import create_app
from devopsagents.apps.container import build_container
from devopsagents.core.config.settings import Settings
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.telemetry.metrics import CONTENT_TYPE, render_prometheus
from devopsagents.domain.jobs import AgentKind


def sample(text: str, name: str, **labels: str) -> float | None:
    """Value of one sample, e.g. sample(t, 'devopsagents_jobs', agent='review', status='dead')."""
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        head, _, value = line.rpartition(" ")
        if head == name and not labels:
            return float(value)
        if all(f'{k}="{v}"' in head for k, v in labels.items()) and head.startswith(name + "{"):
            return float(value)
    return None


# ---- store snapshot ----------------------------------------------------------------------------
def test_snapshot_counts_jobs_ages_audit_and_tokens(store, clock):
    a, _ = store.enqueue(AgentKind.REVIEW, "o/r#1", "s1", "v1")
    store.enqueue(AgentKind.SECURITY, "o/r#2", "s1", "v1")
    done, _ = store.enqueue(AgentKind.INCIDENT, "inc", "r", "v1")
    store.lease(done.id)
    store.complete(done.id, {})
    clock.advance(90)
    store.audit("agent", "review.posted", "o/r#1")
    store.audit("agent", "review.posted", "o/r#9")
    store.record_usage("o/r", 100)
    store.record_usage("global", 100)
    snap = store.metrics_snapshot()
    assert snap["jobs"][("review", "queued")] == 1 and snap["jobs"][("incident", "succeeded")] == 1
    assert snap["oldest_queued_age"]["review"] == 90 and "incident" not in snap["oldest_queued_age"]
    assert snap["audit"] == {"review.posted": 2}
    assert snap["tokens_today"] == {"o/r": 100, "global": 100}
    assert a.id  # sanity


def test_snapshot_bounds_label_cardinality(store):
    for i in range(30):
        store.record_usage(f"org/repo{i}", 10 + i)
    assert len(store.metrics_snapshot(max_scopes=5)["tokens_today"]) <= 6  # top scopes only


# ---- rendering -----------------------------------------------------------------------------------
EMPTY = {"jobs": {}, "oldest_queued_age": {}, "audit": {}, "tokens_today": {}}


def test_render_is_valid_exposition_format_with_zero_filled_series():
    text = render_prometheus(EMPTY, kill_switch=False, version="1.2.3")
    assert text.endswith("\n") and sample(text, "devopsagents_kill_switch") == 0
    assert sample(text, "devopsagents_build_info", version="1.2.3") == 1
    assert sample(text, "devopsagents_jobs", agent="incident", status="dead") == 0  # never a missing series
    types = re.findall(r"^# TYPE (\S+) (\S+)$", text, re.M)
    helps = re.findall(r"^# HELP (\S+) ", text, re.M)
    assert [n for n, _ in types] == helps and {t for _, t in types} <= {"gauge", "counter"}
    for line in text.splitlines():  # every sample line is `name{labels} number`
        if not line.startswith("#"):
            assert re.fullmatch(r"[a-z_]+(\{[^}]*\})? -?[0-9.e+-]+", line), line


def test_render_reflects_values_and_escapes_labels():
    snap = {
        "jobs": {("review", "dead"): 3},
        "oldest_queued_age": {"review": 12.5},
        "audit": {'we"ird\\ev\nent': 4},
        "tokens_today": {"o/r": 7},
    }
    text = render_prometheus(snap, kill_switch=True, version="x")
    assert sample(text, "devopsagents_jobs", agent="review", status="dead") == 3
    assert sample(text, "devopsagents_oldest_queued_job_age_seconds", agent="review") == 12.5
    assert sample(text, "devopsagents_kill_switch") == 1
    assert 'event="we\\"ird\\\\ev\\nent"' in text  # quotes, backslashes and newlines cannot break the format


# ---- endpoint --------------------------------------------------------------------------------------
@pytest.fixture
def make_client(repo_root):
    def build(**kw):
        c = build_container(
            Settings(database_url="sqlite+pysqlite://", **kw),
            backend=ScriptedBackend(replies=[]),
            base_dir=repo_root,
        )
        return c, TestClient(create_app(c))

    return build


def test_metrics_is_disabled_until_a_token_is_configured(make_client):
    _, client = make_client()
    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", headers={"authorization": "Bearer "}).status_code == 404


def test_metrics_requires_the_bearer_token(make_client):
    _, client = make_client(metrics_token=SecretStr("m-tok"))
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.get("/metrics", headers={"x-metrics-token": "m-tok"}).status_code == 401  # bearer only


def test_metrics_endpoint_reports_live_state(make_client):
    c, client = make_client(metrics_token=SecretStr("m-tok"), kill_switch=True)
    j, _ = c.store.enqueue(AgentKind.REVIEW, "o/r#1", "s", "v1")
    c.store.lease(j.id)
    c.store.fail(j.id, "boom", retryable=False)
    r = client.get("/metrics", headers={"authorization": "Bearer m-tok"})
    assert r.status_code == 200 and r.headers["content-type"] == CONTENT_TYPE
    assert sample(r.text, "devopsagents_jobs", agent="review", status="dead") == 1
    assert sample(r.text, "devopsagents_kill_switch") == 1
