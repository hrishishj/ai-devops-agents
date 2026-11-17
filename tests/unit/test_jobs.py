from devopsagents.core.jobs.runner import NonRetryableError, Outcome, run_job
from devopsagents.domain.jobs import AgentKind, JobStatus


def enq(store, rev="sha1", target="o/r#1", agent=AgentKind.REVIEW):
    return store.enqueue(agent, target, rev, "v1", {"x": 1})


def test_enqueue_is_idempotent(store):
    j1, c1 = enq(store)
    j2, c2 = enq(store)
    assert c1 and not c2
    assert j1.id == j2.id


def test_new_revision_supersedes_old_open_job(store):
    old, _ = enq(store, "sha1")
    new, created = enq(store, "sha2")
    assert created
    assert store.get(old.id).status == JobStatus.SUPERSEDED
    assert store.get(new.id).status == JobStatus.QUEUED


def test_supersede_is_scoped_to_agent_and_target(store):
    a, _ = enq(store, "s", "o/r#1")
    b, _ = enq(store, "s", "o/r#2")
    c, _ = enq(store, "s", "o/r#1", AgentKind.SECURITY)
    enq(store, "s2", "o/r#1")
    assert store.get(a.id).status == JobStatus.SUPERSEDED
    assert store.get(b.id).status == JobStatus.QUEUED
    assert store.get(c.id).status == JobStatus.QUEUED


def test_lease_is_exclusive_until_expiry(store, clock):
    j, _ = enq(store)
    assert store.lease(j.id, 60) is not None
    assert store.lease(j.id, 60) is None  # live lease
    clock.advance(61)
    again = store.lease(j.id, 60)  # crashed worker: lease expired
    assert again is not None and again.attempts == 2


def test_success_path(store):
    j, _ = enq(store)
    out = run_job(store, j.id, {AgentKind.REVIEW: lambda job: {"ok": True}})
    assert out == Outcome.SUCCEEDED
    assert store.get(j.id).status == JobStatus.SUCCEEDED
    assert store.result(j.id) == {"ok": True}
    assert run_job(store, j.id, {AgentKind.REVIEW: lambda job: {}}) == Outcome.SKIPPED


def test_retry_then_dead_letter(store):
    j, _ = store.enqueue(AgentKind.REVIEW, "o/r#1", "s", "v1", max_attempts=2)

    def boom(job):
        raise RuntimeError("nope")

    assert run_job(store, j.id, {AgentKind.REVIEW: boom}) == Outcome.RETRY
    assert run_job(store, j.id, {AgentKind.REVIEW: boom}) == Outcome.DEAD
    assert store.get(j.id).status == JobStatus.DEAD
    assert store.dead_letter_count() == 1


def test_non_retryable_goes_straight_to_dead(store):
    j, _ = enq(store)

    def bad(job):
        raise NonRetryableError("bad input")

    assert run_job(store, j.id, {AgentKind.REVIEW: bad}) == Outcome.DEAD


def test_superseded_while_running_drops_result(store):
    j, _ = enq(store, "sha1")

    def handler(job):
        enq(store, "sha2")  # a new push arrives mid-run
        assert not store.is_current(job.id)
        return {"posted": True}

    assert run_job(store, j.id, {AgentKind.REVIEW: handler}) == Outcome.DROPPED
    assert store.get(j.id).status == JobStatus.SUPERSEDED


def test_reclaimable_lists_stuck_jobs(store, clock):
    j, _ = enq(store)
    assert j.id in store.reclaimable()
    store.lease(j.id, 30)
    assert j.id not in store.reclaimable()
    clock.advance(31)
    assert j.id in store.reclaimable()


def test_usage_and_audit(store):
    store.record_usage("o/r", 100)
    store.record_usage("o/r", 50)
    assert store.usage_today("o/r") == 150
    store.audit("agent", "action.executed", "res1", {"a": 1})
    assert store.audit_events("res1")[0]["data"] == {"a": 1}
