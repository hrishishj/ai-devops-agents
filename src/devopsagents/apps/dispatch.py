"""Persist-then-enqueue. The DB row is the commitment; the queue message is just a nudge."""

from __future__ import annotations

import logging

from devopsagents.apps.container import Container, queue_for
from devopsagents.domain.jobs import AgentKind, Job

log = logging.getLogger(__name__)


def dispatch(
    c: Container,
    agent: AgentKind,
    target: str,
    revision: str,
    agent_version: str,
    payload: dict[str, object],
) -> tuple[Job, bool]:
    job, created = c.store.enqueue(
        agent, target, revision, agent_version, payload, max_attempts=c.settings.max_attempts
    )
    if created:
        try:
            c.queue.send(queue_for(c.settings, agent), job.id, session_id=target)
        except Exception:  # queue outage must not fail ingestion: the worker sweeps the DB
            log.exception("queue send failed for job %s; relying on DB sweep", job.id)
    return job, created
