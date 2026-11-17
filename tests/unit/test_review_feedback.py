from __future__ import annotations

import re

import httpx
import pytest

from devopsagents.core.jobs.runner import run_job
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.client import GitHubClient, GitHubError, ReviewThread
from tests.unit.test_review import FakeGH, make_agent

MARKER = re.compile(r"<!-- da:fp=([0-9a-f]{16}) -->")
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


def thread(fp, *, resolved=False, outdated=False, by=None, down=0, tid="T1", extra=""):
    body = f"finding body {extra}\n\n<!-- da:fp={fp} -->" if fp else "a human wrote this"
    return ReviewThread(tid, resolved, outdated, by, body, down)


def run(store, repo_root, gh, rev="sha1", target="o/r#7"):
    """One review run; returns (fp posted or None, result). Threads in `gh` are what GitHub reports."""
    gh.head = rev
    agent, _ = make_agent(store, gh, [GOOD], repo_root)
    job, _ = store.enqueue(
        AgentKind.REVIEW,
        target,
        rev,
        "v1",
        {"repo": "o/r", "number": int(target.split("#")[1]), "head_sha": rev, "installation_id": 1},
    )
    run_job(store, job.id, {AgentKind.REVIEW: agent.handle})
    res = store.result(job.id)
    fps = [m.group(1) for _, _, cs in gh.posted[-1:] for c in cs for m in [MARKER.search(c.body)] if m]
    return (fps[0] if fps else None), res


@pytest.fixture
def fp(store, repo_root):
    """The fingerprint of the finding the scripted model produces (learned from a clean first run)."""
    got, _ = run(store, repo_root, FakeGH(), target="o/r#1")
    assert got
    return got


# ---- marker -------------------------------------------------------------------------------------------
def test_every_posted_comment_carries_a_stable_hidden_marker(store, repo_root):
    first, _ = run(store, repo_root, FakeGH(), rev="sha1", target="o/r#1")
    second, _ = run(store, repo_root, FakeGH(), rev="sha2", target="o/r#2")
    assert first and first == second  # same finding on different PRs/commits -> same fingerprint


# ---- duplicates and dismissals ----------------------------------------------------------------------------
def test_an_open_thread_for_the_same_finding_is_not_posted_again(store, repo_root, fp):
    gh = FakeGH()
    gh.threads = [thread(fp)]
    _, res = run(store, repo_root, gh, rev="sha2", target="o/r#2")
    assert gh.posted[-1][2] == [] and res["to_post"] == 0 and res["suppressed_by_feedback"] == 1


def test_a_thread_resolved_by_a_human_suppresses_the_finding_everywhere(store, repo_root, fp):
    gh = FakeGH()
    gh.threads = [thread(fp, resolved=True, by="alice")]
    _, res = run(store, repo_root, gh, rev="sha2", target="o/r#2")
    assert res["to_post"] == 0 and res["suppressed_by_feedback"] == 1
    assert fp in store.dismissed_fingerprints("o/r")  # remembered
    other = FakeGH()  # a different PR with no threads at all: still suppressed from memory
    _, res2 = run(store, repo_root, other, rev="sha3", target="o/r#3")
    assert res2["to_post"] == 0 and other.posted[-1][2] == []
    assert store.dismissed_fingerprints("o/other-repo") == set()  # per repository


def test_a_thumbs_down_counts_as_a_dismissal(store, repo_root, fp):
    gh = FakeGH()
    gh.threads = [thread(fp, down=1)]
    _, res = run(store, repo_root, gh, rev="sha2", target="o/r#2")
    assert res["to_post"] == 0 and fp in store.dismissed_fingerprints("o/r")


def test_a_thread_resolved_by_the_bot_itself_is_not_a_human_dismissal(store, repo_root, fp):
    gh = FakeGH()
    gh.threads = [thread(fp, resolved=True, by="da-bot")]  # we resolved it (e.g. it was outdated)
    posted, res = run(store, repo_root, gh, rev="sha2", target="o/r#2")
    assert posted == fp and res["to_post"] == 1  # the issue is still present, so it is reported again
    assert store.dismissed_fingerprints("o/r") == set()


def test_threads_that_are_not_ours_are_ignored(store, repo_root):
    gh = FakeGH()
    gh.threads = [thread(None, resolved=True, by="alice"), thread(None, down=3)]
    posted, res = run(store, repo_root, gh)
    assert posted and res["suppressed_by_feedback"] == 0


# ---- resolving stale comments ----------------------------------------------------------------------------
def test_outdated_open_threads_are_resolved_after_the_new_review_posts(store, repo_root, fp):
    gh = FakeGH()
    gh.threads = [thread(fp, outdated=True, tid="OLD")]
    posted, res = run(store, repo_root, gh, rev="sha2", target="o/r#2")
    assert posted == fp  # the code moved, so the finding is not treated as already reported
    assert gh.resolved == ["OLD"] and res["threads_resolved"] == 1
    assert any(e["event"] == "review.threads_resolved" for e in store.audit_events("o/r#2"))


def test_shadow_mode_never_touches_the_pull_request(store, repo_root, fp):
    gh = FakeGH(cfg=None)  # no config -> shadow
    gh.threads = [thread(fp, outdated=True, tid="OLD")]
    _, res = run(store, repo_root, gh, rev="sha2", target="o/r#2")
    assert res["status"] == "shadow" and gh.resolved == [] and res["would_resolve"] == 1


# ---- resilience --------------------------------------------------------------------------------------------
def test_failing_to_read_threads_does_not_stop_the_review(store, repo_root):
    gh = FakeGH()
    gh.threads_error = GitHubError(403, "Resource not accessible by integration")
    posted, res = run(store, repo_root, gh)
    assert posted and res["status"] == "posted"


def test_failing_to_resolve_a_thread_does_not_fail_the_job(store, repo_root, fp):
    gh = FakeGH()
    gh.threads = [thread(fp, outdated=True)]
    gh.resolve_error = GitHubError(403, "forbidden")
    posted, res = run(store, repo_root, gh, rev="sha2", target="o/r#2")
    assert posted and res["status"] == "posted" and res["threads_resolved"] == 0


# ---- store memory ---------------------------------------------------------------------------------------------
def test_feedback_is_idempotent_and_expires(store, clock):
    assert store.record_feedback("o/r", "abc") is True
    assert store.record_feedback("o/r", "abc") is False  # no duplicates
    assert store.dismissed_fingerprints("o/r") == {"abc"}
    clock.advance(91 * 86400)
    assert store.dismissed_fingerprints("o/r") == set()  # old dismissals stop applying
    assert store.dismissed_fingerprints("o/r", max_age_days=365) == {"abc"}


# ---- GitHub GraphQL contract ----------------------------------------------------------------------------------
def gql_client(handler):
    return GitHubClient(token="ghs_t", transport=httpx.MockTransport(handler))


THREADS_RESPONSE = {
    "data": {
        "viewer": {"login": "da-bot"},
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [
                        {
                            "id": "T1",
                            "isResolved": True,
                            "isOutdated": False,
                            "resolvedBy": {"login": "alice"},
                            "comments": {"nodes": [{"body": "hello", "reactions": {"totalCount": 2}}]},
                        },
                        {
                            "id": "T2",
                            "isResolved": False,
                            "isOutdated": True,
                            "resolvedBy": None,
                            "comments": {"nodes": []},
                        },
                    ]
                }
            }
        },
    }
}


def test_review_threads_query_and_parsing():
    seen = {}

    def handler(req):
        seen.update(
            path=req.url.path, auth=req.headers["authorization"], body=__import__("json").loads(req.content)
        )
        return httpx.Response(200, json=THREADS_RESPONSE)

    viewer, threads = gql_client(handler).review_threads(0, "o/r", 7)
    assert seen["path"] == "/graphql" and seen["auth"] == "Bearer ghs_t"
    assert seen["body"]["variables"] == {"owner": "o", "name": "r", "number": 7}
    assert "reviewThreads" in seen["body"]["query"] and "THUMBS_DOWN" in seen["body"]["query"]
    assert viewer == "da-bot"
    assert threads[0] == ReviewThread("T1", True, False, "alice", "hello", 2)
    assert threads[1] == ReviewThread("T2", False, True, None, "", 0)  # empty comment list is handled


def test_resolve_mutation_and_graphql_errors():
    sent = []

    def ok(req):
        sent.append(__import__("json").loads(req.content))
        return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1"}}}})

    gql_client(ok).resolve_review_thread(0, "T1")
    assert sent[0]["variables"] == {"id": "T1"} and "resolveReviewThread" in sent[0]["query"]
    err = httpx.Response(200, json={"errors": [{"message": "Resource not accessible"}]})
    with pytest.raises(GitHubError, match="not accessible"):
        gql_client(lambda r: err).review_threads(0, "o/r", 1)
    with pytest.raises(GitHubError):
        gql_client(lambda r: httpx.Response(401, text="bad creds")).resolve_review_thread(0, "T1")
