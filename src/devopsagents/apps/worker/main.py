"""Worker: drains queue messages and sweeps the DB for stranded jobs; runs each under a lease."""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

from devopsagents.apps.container import Container, build_container, queue_for
from devopsagents.core.config.settings import get_settings
from devopsagents.core.jobs.runner import Outcome, run_job
from devopsagents.core.telemetry.tracing import configure_logging
from devopsagents.domain.jobs import AgentKind

log = logging.getLogger(__name__)

# Incident jobs may wait on runbook completion + verification, so they get a longer lease.
_LEASE = {AgentKind.INCIDENT: 1200}


class Worker:
    def __init__(self, c: Container, agents: list[AgentKind] | None = None) -> None:
        self._c = c
        self._agents = [a for a in (agents or list(AgentKind)) if a in c.handlers]
        self._stop = False

    def tick(self) -> dict[str, int]:
        """One scheduling pass. Returns outcome counts (handy for tests and metrics)."""
        counts: dict[str, int] = {}
        for agent in self._agents:
            ids = list(
                dict.fromkeys(
                    self._c.queue.receive(queue_for(self._c.settings, agent), max_messages=5)
                    + self._c.store.reclaimable(agent)
                )
            )
            for job_id in ids:
                job = self._c.store.get(job_id)
                if job is None or job.agent != agent:
                    continue
                outcome = run_job(
                    self._c.store, job_id, self._c.handlers, _LEASE.get(agent, self._c.settings.lease_seconds)
                )
                counts[outcome.value] = counts.get(outcome.value, 0) + 1
                if outcome in (Outcome.RETRY, Outcome.DEAD):
                    log.error("job %s (%s) -> %s", job_id, agent.value, outcome.value)
        return counts

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        log.info("worker started for %s", [a.value for a in self._agents])
        while not self._stop:
            if not any(self.tick().values()):
                time.sleep(poll_seconds)


def main(agents: list[str] | None = None) -> None:  # pragma: no cover - process entrypoint
    configure_logging()
    c = build_container(get_settings(), base_dir=Path.cwd())
    Worker(c, [AgentKind(a) for a in agents] if agents else None).run_forever()
