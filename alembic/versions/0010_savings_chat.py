"""savings flow + chat: incomes.kind, own_accounts, pending_actions, conversation_messages

Revision ID: 0010_savings_chat
Revises: 0009_agent_traces
Create Date: 2026-11-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_savings_chat"
down_revision: Union[str, Sequence[str], None] = "0009_agent_traces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("incomes", sa.Column("kind", sa.String(20), nullable=False, server_default="other"))
    op.create_table(
        "own_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_own_accounts_user_last4", "own_accounts", ["user_id", "last4"], unique=True)
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tool_name", sa.String(60), nullable=False),
        sa.Column("args", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pending_actions_user_status", "pending_actions", ["user_id", "status"])
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_conversation_messages_user", "conversation_messages", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_user", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_pending_actions_user_status", table_name="pending_actions")
    op.drop_table("pending_actions")
    op.drop_index("ix_own_accounts_user_last4", table_name="own_accounts")
    op.drop_table("own_accounts")
    op.drop_column("incomes", "kind")
