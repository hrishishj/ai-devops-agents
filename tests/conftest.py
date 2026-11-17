from __future__ import annotations

from pathlib import Path

import pytest

from devopsagents.core.jobs.store import JobStore, make_engine

ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> JobStore:
    return JobStore(make_engine("sqlite+pysqlite://"), clock=clock)


@pytest.fixture
def repo_root() -> Path:
    return ROOT
