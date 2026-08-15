"""budgets + budget_alerts_sent tables (Phase 3a)

Revision ID: 0002_budgets
Revises: 0001_initial
Create Date: 2026-08-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_budgets"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("monthly_limit", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_budgets_user_category", "budgets", ["user_id", "category"], unique=True
    )

    op.create_table(
        "budget_alerts_sent",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_budget_alerts_sent_user_category_month",
        "budget_alerts_sent",
        ["user_id", "category", "month"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_budget_alerts_sent_user_category_month", table_name="budget_alerts_sent"
    )
    op.drop_table("budget_alerts_sent")
    op.drop_index("ix_budgets_user_category", table_name="budgets")
    op.drop_table("budgets")
