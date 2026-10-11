"""confirmation guard and bot failure count on conversations

Revision ID: 5d1e8a4b7c21
Revises: 72cb02a054a7
Create Date: 2026-09-27 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5d1e8a4b7c21"
down_revision: str | None = "72cb02a054a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("pending_action", sa.JSON(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("bot_failures", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "bot_failures")
    op.drop_column("conversations", "pending_action")
