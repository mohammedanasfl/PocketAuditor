"""salary_profiles table (Phase 4 — expected salary / savings target / payday)

Revision ID: 0005_salary_profiles
Revises: 0004_incomes
Create Date: 2026-08-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_salary_profiles"
down_revision: Union[str, Sequence[str], None] = "0004_incomes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "salary_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expected_salary", sa.Numeric(10, 2), nullable=False),
        sa.Column("savings_target", sa.Numeric(10, 2), nullable=True),
        sa.Column("payday_day", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_salary_profiles_user", "salary_profiles", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_salary_profiles_user", table_name="salary_profiles")
    op.drop_table("salary_profiles")
