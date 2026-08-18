"""merchant_categories table (per-merchant remembered category)

Revision ID: 0008_merchant_categories
Revises: 0007_audit_alerts_sent
Create Date: 2026-08-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_merchant_categories"
down_revision: Union[str, Sequence[str], None] = "0007_audit_alerts_sent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merchant_categories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("merchant", sa.Text(), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_merchant_categories_user_merchant", "merchant_categories", ["user_id", "merchant"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_merchant_categories_user_merchant", table_name="merchant_categories")
    op.drop_table("merchant_categories")
