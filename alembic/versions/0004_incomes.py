"""incomes table (Phase 4 — capture money-in from SMS credits)

Revision ID: 0004_incomes
Revises: 0003_category_hint
Create Date: 2026-08-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_incomes"
down_revision: Union[str, Sequence[str], None] = "0003_category_hint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "incomes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("created_via", sa.String(20), server_default="auto", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_incomes_user_date", "incomes", ["user_id", "txn_date"])


def downgrade() -> None:
    op.drop_index("ix_incomes_user_date", table_name="incomes")
    op.drop_table("incomes")
