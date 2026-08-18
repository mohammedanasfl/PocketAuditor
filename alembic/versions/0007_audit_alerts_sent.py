"""audit_alerts_sent table (Phase 4 — mid-month salary alert dedup)

Revision ID: 0007_audit_alerts_sent
Revises: 0006_audit_runs
Create Date: 2026-08-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_audit_alerts_sent"
down_revision: Union[str, Sequence[str], None] = "0006_audit_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_alerts_sent",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("alert_type", sa.String(20), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_alerts_sent_user_month_type", "audit_alerts_sent", ["user_id", "month", "alert_type"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_audit_alerts_sent_user_month_type", table_name="audit_alerts_sent")
    op.drop_table("audit_alerts_sent")
