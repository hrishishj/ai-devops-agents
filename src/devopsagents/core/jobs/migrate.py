"""Programmatic Alembic runner (`devopsagents migrate`)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(_config(url), revision)
