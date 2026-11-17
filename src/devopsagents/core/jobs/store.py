"""Durable job store (SQLAlchemy — SQLite in tests, PostgreSQL in production).

Guarantees
----------
* Idempotent enqueue: unique `key` (agent:target:revision:agent_version).
* Supersede: a new revision for the same agent+target supersedes older open jobs.
* Atomic lease: conditional UPDATE; crashed workers' leases expire and are re-leased.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Float, Integer, String, Text, create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from devopsagents.domain.jobs import TERMINAL_STATUSES, AgentKind, Job, JobStatus, idempotency_key

_OPEN = [JobStatus.QUEUED.value, JobStatus.FAILED.value, JobStatus.LEASED.value]


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    target: Mapped[str] = mapped_column(String(256), index=True)
    revision: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class AuditRow(Base):
    """Append-only audit trail for every side effect."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(256), index=True)
    data: Mapped[str] = mapped_column(Text, default="{}")


class UsageRow(Base):
    """Token usage ledger for budget enforcement."""

    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    scope: Mapped[str] = mapped_column(String(256), index=True)  # repo or "global"
    tokens: Mapped[int] = mapped_column(Integer)


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if ts is not None else None


def _to_job(r: JobRow) -> Job:
    return Job(
        id=r.id,
        key=r.key,
        agent=AgentKind(r.agent),
        target=r.target,
        revision=r.revision,
        status=JobStatus(r.status),
        attempts=r.attempts,
        max_attempts=r.max_attempts,
        lease_until=_dt(r.lease_until),
        payload=json.loads(r.payload),
        error=r.error,
        created_at=_dt(r.created_at) or datetime.now(UTC),
        updated_at=_dt(r.updated_at) or datetime.now(UTC),
    )


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        if ":memory:" in url or url.endswith("://"):
            return create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


class JobStore:
    def __init__(
        self, engine: Engine, *, clock: Callable[[], float] = time.time, create_schema: bool = True
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        if create_schema:  # dev/tests; production runs `devopsagents migrate` and sets this false
            Base.metadata.create_all(engine)

    # ---- enqueue ------------------------------------------------------------------------
    def enqueue(
        self,
        agent: AgentKind,
        target: str,
        revision: str,
        agent_version: str,
        payload: dict[str, object] | None = None,
        max_attempts: int = 3,
    ) -> tuple[Job, bool]:
        """Returns (job, created). created=False means an identical job already exists."""
        key = idempotency_key(agent, target, revision, agent_version)
        now = self._clock()
        try:
            with self._sessions.begin() as s:
                existing = s.scalar(select(JobRow).where(JobRow.key == key))
                if existing:
                    return _to_job(existing), False
                row = JobRow(
                    id=str(uuid.uuid4()),
                    key=key,
                    agent=agent.value,
                    target=target,
                    revision=revision,
                    status=JobStatus.QUEUED.value,
                    attempts=0,
                    max_attempts=max_attempts,
                    payload=json.dumps(payload or {}),
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
                s.flush()
                s.execute(
                    update(JobRow)
                    .where(
                        JobRow.agent == agent.value,
                        JobRow.target == target,
                        JobRow.id != row.id,
                        JobRow.status.in_(_OPEN),
                    )
                    .values(status=JobStatus.SUPERSEDED.value, updated_at=now)
                )
                return _to_job(row), True
        except IntegrityError:  # lost a race with a concurrent identical enqueue
            with self._sessions() as s2:
                got = s2.scalar(select(JobRow).where(JobRow.key == key))
                if got is None:
                    raise
                return _to_job(got), False

    # ---- lease / complete / fail --------------------------------------------------------
    def lease(self, job_id: str, lease_seconds: int = 300) -> Job | None:
        """Atomically claim a job. None if terminal, held by a live lease, or exhausted."""
        now = self._clock()
        with self._sessions.begin() as s:
            res = s.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.attempts < JobRow.max_attempts,
                    (JobRow.status.in_([JobStatus.QUEUED.value, JobStatus.FAILED.value]))
                    | ((JobRow.status == JobStatus.LEASED.value) & (JobRow.lease_until < now)),
                )
                .values(
                    status=JobStatus.LEASED.value,
                    attempts=JobRow.attempts + 1,
                    lease_until=now + lease_seconds,
                    updated_at=now,
                )
            )
            if res.rowcount != 1:  # type: ignore[attr-defined]
                return None
            row = s.get(JobRow, job_id)
            assert row is not None
            return _to_job(row)

    def complete(self, job_id: str, result: dict[str, object] | None = None) -> bool:
        with self._sessions.begin() as s:
            row = s.get(JobRow, job_id)
            # a superseded/cancelled job must never flip back to succeeded
            if row is None or JobStatus(row.status) != JobStatus.LEASED:
                return False
            row.status = JobStatus.SUCCEEDED.value
            row.result = json.dumps(result, default=str) if result is not None else None
            row.lease_until = None
            row.updated_at = self._clock()
            return True

    def fail(self, job_id: str, error: str, *, retryable: bool = True) -> JobStatus:
        with self._sessions.begin() as s:
            row = s.get(JobRow, job_id)
            if row is None:
                return JobStatus.DEAD
            if JobStatus(row.status) in TERMINAL_STATUSES:
                return JobStatus(row.status)
            dead = (not retryable) or row.attempts >= row.max_attempts
            row.status = (JobStatus.DEAD if dead else JobStatus.FAILED).value
            row.error = error[:2000]
            row.lease_until = None
            row.updated_at = self._clock()
            return JobStatus(row.status)

    def cancel(self, job_id: str) -> bool:
        with self._sessions.begin() as s:
            row = s.get(JobRow, job_id)
            if row is None or JobStatus(row.status) in TERMINAL_STATUSES:
                return False
            row.status = JobStatus.CANCELLED.value
            row.updated_at = self._clock()
            return True

    # ---- queries ------------------------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._sessions() as s:
            r = s.get(JobRow, job_id)
            return _to_job(r) if r else None

    def is_current(self, job_id: str) -> bool:
        """False once superseded or cancelled — workers check before every side effect."""
        j = self.get(job_id)
        return j is not None and j.status == JobStatus.LEASED

    def result(self, job_id: str) -> dict[str, Any] | None:
        with self._sessions() as s:
            r = s.get(JobRow, job_id)
            return json.loads(r.result) if r and r.result else None

    def reclaimable(self, agent: AgentKind | None = None) -> list[str]:
        """Job ids needing (re-)delivery: queued/failed, or leases that expired.

        The database is the source of truth: workers sweep this so a lost queue message can
        never strand a job.
        """
        now = self._clock()
        with self._sessions() as s:
            q = select(JobRow.id).where(
                JobRow.attempts < JobRow.max_attempts,
                (JobRow.status.in_([JobStatus.QUEUED.value, JobStatus.FAILED.value]))
                | ((JobRow.status == JobStatus.LEASED.value) & (JobRow.lease_until < now)),
            )
            if agent is not None:
                q = q.where(JobRow.agent == agent.value)
            return list(s.scalars(q.order_by(JobRow.created_at)))

    def find_latest(self, agent: AgentKind, target: str) -> tuple[Job, dict[str, Any] | None] | None:
        """Most recent job for a target together with its stored result."""
        with self._sessions() as s:
            r = s.scalars(
                select(JobRow)
                .where(
                    JobRow.agent == agent.value,
                    JobRow.target == target,
                    JobRow.status == JobStatus.SUCCEEDED.value,
                )
                .order_by(JobRow.created_at.desc())
            ).first()
            return (_to_job(r), json.loads(r.result) if r.result else None) if r else None

    def dead_letter_count(self) -> int:
        with self._sessions() as s:
            return len(list(s.scalars(select(JobRow.id).where(JobRow.status == "dead"))))

    def metrics_snapshot(self, max_scopes: int = 10) -> dict[str, Any]:
        """Aggregates for /metrics, read from the database so the API sees every worker's activity."""
        now = self._clock()
        open_states = [JobStatus.QUEUED.value, JobStatus.FAILED.value]
        with self._sessions() as s:
            jobs = {
                (a, st): int(n)
                for a, st, n in s.execute(
                    select(JobRow.agent, JobRow.status, func.count()).group_by(JobRow.agent, JobRow.status)
                )
            }
            oldest = {
                a: max(0.0, now - float(ts))
                for a, ts in s.execute(
                    select(JobRow.agent, func.min(JobRow.created_at))
                    .where(JobRow.status.in_(open_states))
                    .group_by(JobRow.agent)
                )
            }
            audit = {
                str(ev): int(n)
                for ev, n in s.execute(select(AuditRow.event, func.count()).group_by(AuditRow.event))
            }
            total = func.sum(UsageRow.tokens)
            tokens = {
                str(scope): int(n or 0)
                for scope, n in s.execute(
                    select(UsageRow.scope, total)
                    .where(UsageRow.day == self._day())
                    .group_by(UsageRow.scope)
                    .order_by(total.desc())
                    .limit(max_scopes + 1)  # +1: "global" is always included in the ledger
                )
            }
        return {"jobs": jobs, "oldest_queued_age": oldest, "audit": audit, "tokens_today": tokens}

    # ---- audit / usage ------------------------------------------------------------------
    def audit(self, actor: str, event: str, subject: str, data: dict[str, object] | None = None) -> None:
        with self._sessions.begin() as s:
            s.add(
                AuditRow(
                    ts=self._clock(),
                    actor=actor,
                    event=event,
                    subject=subject,
                    data=json.dumps(data or {}, default=str),
                )
            )

    def audit_events(self, subject: str | None = None) -> list[dict[str, Any]]:
        with self._sessions() as s:
            q = select(AuditRow).order_by(AuditRow.id)
            if subject:
                q = q.where(AuditRow.subject == subject)
            return [
                {
                    "ts": r.ts,
                    "actor": r.actor,
                    "event": r.event,
                    "subject": r.subject,
                    "data": json.loads(r.data),
                }
                for r in s.scalars(q)
            ]

    def count_audit(self, event: str, subject: str, since: float) -> int:
        with self._sessions() as s:
            rows = s.scalars(
                select(AuditRow.id).where(
                    AuditRow.event == event, AuditRow.subject == subject, AuditRow.ts >= since
                )
            )
            return len(list(rows))

    def record_usage(self, scope: str, tokens: int) -> None:
        with self._sessions.begin() as s:
            s.add(UsageRow(day=self._day(), scope=scope, tokens=tokens))

    def usage_today(self, scope: str) -> int:
        with self._sessions() as s:
            rows = s.scalars(
                select(UsageRow.tokens).where(UsageRow.day == self._day(), UsageRow.scope == scope)
            )
            return sum(rows)

    def now(self) -> float:
        return self._clock()

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")
