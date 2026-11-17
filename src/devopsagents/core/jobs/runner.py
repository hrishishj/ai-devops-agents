"""Runs a leased job through its handler with correct state transitions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum

from devopsagents.core.jobs.store import JobStore
from devopsagents.domain.jobs import AgentKind, Job, JobStatus

log = logging.getLogger(__name__)

Handler = Callable[[Job], dict[str, object]]


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"  # could not lease: duplicate delivery, terminal, or live lease
    RETRY = "retry"
    DEAD = "dead"
    DROPPED = "dropped"  # superseded/cancelled mid-flight; result discarded


class NonRetryableError(Exception):
    """Raise from a handler to dead-letter immediately (bad input, forbidden, ...)."""


def run_job(
    store: JobStore, job_id: str, handlers: dict[AgentKind, Handler], lease_seconds: int = 300
) -> Outcome:
    job = store.lease(job_id, lease_seconds)
    if job is None:
        return Outcome.SKIPPED
    try:
        result = handlers[job.agent](job)
    except NonRetryableError as exc:
        store.fail(job.id, str(exc), retryable=False)
        return Outcome.DEAD
    except Exception as exc:
        log.exception("job %s failed", job.id)
        status = store.fail(job.id, repr(exc))
        return Outcome.DEAD if status == JobStatus.DEAD else Outcome.RETRY
    return Outcome.SUCCEEDED if store.complete(job.id, result) else Outcome.DROPPED
