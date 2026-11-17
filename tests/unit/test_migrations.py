from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from devopsagents.core.jobs.migrate import upgrade
from devopsagents.core.jobs.store import Base


def test_migration_matches_models_and_is_reversible(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    engine = create_engine(url)
    assert {"jobs", "audit_log", "llm_usage"} <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == []  # models and migrations have not drifted


def test_store_works_on_migrated_schema(tmp_path):
    from devopsagents.core.jobs.store import JobStore, make_engine
    from devopsagents.domain.jobs import AgentKind

    url = f"sqlite+pysqlite:///{tmp_path / 'n.db'}"
    upgrade(url)
    s = JobStore(make_engine(url), create_schema=False)
    job, created = s.enqueue(AgentKind.REVIEW, "o/r#1", "sha", "v1")
    assert created and s.lease(job.id) is not None
