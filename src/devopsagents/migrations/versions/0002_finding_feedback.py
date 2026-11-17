"""finding_feedback: findings a human rejected

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finding_feedback",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("fp", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
    )
    op.create_index("ix_finding_feedback_repo", "finding_feedback", ["repo"])
    op.create_index("ix_finding_feedback_fp", "finding_feedback", ["fp"])
    op.create_index("ix_finding_feedback_ts", "finding_feedback", ["ts"])


def downgrade() -> None:
    op.drop_table("finding_feedback")
