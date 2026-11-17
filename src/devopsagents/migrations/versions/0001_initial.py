"""initial schema: jobs, audit_log, llm_usage

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(512), nullable=False),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("target", sa.String(256), nullable=False),
        sa.Column("revision", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("lease_until", sa.Float, nullable=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("result", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("ix_jobs_key", "jobs", ["key"], unique=True)
    op.create_index("ix_jobs_agent", "jobs", ["agent"])
    op.create_index("ix_jobs_target", "jobs", ["target"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("data", sa.Text, nullable=False),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_event", "audit_log", ["event"])
    op.create_index("ix_audit_log_subject", "audit_log", ["subject"])

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("scope", sa.String(256), nullable=False),
        sa.Column("tokens", sa.Integer, nullable=False),
    )
    op.create_index("ix_llm_usage_day", "llm_usage", ["day"])
    op.create_index("ix_llm_usage_scope", "llm_usage", ["scope"])


def downgrade() -> None:
    op.drop_table("llm_usage")
    op.drop_table("audit_log")
    op.drop_table("jobs")
