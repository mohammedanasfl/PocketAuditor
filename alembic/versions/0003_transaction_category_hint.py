"""transactions.category_hint — user's explicit category choice (e.g. photo caption)

Revision ID: 0003_category_hint
Revises: 0002_budgets
Create Date: 2026-08-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_category_hint"
down_revision: Union[str, Sequence[str], None] = "0002_budgets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions", sa.Column("category_hint", sa.String(50), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("transactions", "category_hint")
