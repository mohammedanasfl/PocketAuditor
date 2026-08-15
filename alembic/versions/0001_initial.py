"""initial schema: users, transactions, expenses, reconciliation_runs

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_users_telegram_chat_id", "users", ["telegram_chat_id"], unique=True
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("merchant", sa.Text(), nullable=True),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column(
            "source", sa.String(20), nullable=False, server_default="sms"
        ),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_transactions_user_status", "transactions", ["user_id", "status"]
    )

    op.create_table(
        "expenses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("merchant", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column(
            "linked_transaction_id",
            sa.Uuid(),
            sa.ForeignKey("transactions.id"),
            nullable=True,
        ),
        sa.Column(
            "created_via", sa.String(20), nullable=False, server_default="manual"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_expenses_user_date_amount", "expenses", ["user_id", "txn_date", "amount"]
    )
    op.create_index(
        "ix_expenses_linked_transaction_id", "expenses", ["linked_transaction_id"]
    )

    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "transaction_id",
            sa.Uuid(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="resolved"
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_reconciliation_runs_user_status",
        "reconciliation_runs",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("reconciliation_runs")
    op.drop_index("ix_expenses_linked_transaction_id", table_name="expenses")
    op.drop_index("ix_expenses_user_date_amount", table_name="expenses")
    op.drop_table("expenses")
    op.drop_index("ix_transactions_user_status", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_users_telegram_chat_id", table_name="users")
    op.drop_table("users")
