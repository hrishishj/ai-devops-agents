"""Queue port + in-memory adapter. The Service Bus adapter lives in integrations/azure."""

from __future__ import annotations

from collections import deque
from typing import Protocol


class JobQueue(Protocol):
    def send(self, queue: str, job_id: str, session_id: str) -> None: ...

    def receive(self, queue: str, max_messages: int = 1) -> list[str]: ...


class InMemoryQueue:
    def __init__(self) -> None:
        self._q: dict[str, deque[tuple[str, str]]] = {}

    def send(self, queue: str, job_id: str, session_id: str) -> None:
        self._q.setdefault(queue, deque()).append((job_id, session_id))

    def receive(self, queue: str, max_messages: int = 1) -> list[str]:
        q = self._q.get(queue)
        out: list[str] = []
        while q and len(out) < max_messages:
            out.append(q.popleft()[0])
        return out
