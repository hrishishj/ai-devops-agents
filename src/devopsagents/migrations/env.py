"""Alembic environment. The database URL is passed in by `devopsagents.core.jobs.migrate`."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from devopsagents.core.jobs.store import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(config.get_main_option("sqlalchemy.url") or "")
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
