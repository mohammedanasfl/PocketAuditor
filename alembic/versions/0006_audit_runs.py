"""audit_runs table (Phase 4 — monthly salary-audit trail + monthly dedup)

Revision ID: 0006_audit_runs
Revises: 0005_salary_profiles
Create Date: 2026-08-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_audit_runs"
down_revision: Union[str, Sequence[str], None] = "0005_salary_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("total_income", sa.Numeric(10, 2), nullable=False),
        sa.Column("total_spend", sa.Numeric(10, 2), nullable=False),
        sa.Column("net_saved", sa.Numeric(10, 2), nullable=False),
        sa.Column("savings_rate", sa.Numeric(7, 2), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_audit_runs_user_month", "audit_runs", ["user_id", "period_month"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_audit_runs_user_month", table_name="audit_runs")
    op.drop_table("audit_runs")
