"""Scheduler schema: opt-outs, booking source, reminders, lead interests, follow-ups, staff alerts

Revision ID: 8c3f2d7e9a14
Revises: 5d1e8a4b7c21
Create Date: 2026-09-28 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8c3f2d7e9a14"
down_revision: str | None = "5d1e8a4b7c21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEND_STATUS = ("pending", "sent", "failed", "cancelled", "skipped")


def _status() -> sa.Enum:
    # Same VARCHAR + CHECK storage as the other enums (see app/db/models.py:_enum).
    return sa.Enum(*_SEND_STATUS, name="sendstatus", native_enum=False, length=20)


def _send_columns() -> list[sa.Column[object]]:
    """Columns shared by the three scheduled-send tables."""
    return [
        sa.Column("status", _status(), nullable=False),
        sa.Column("wa_message_id", sa.String(length=128), nullable=True, unique=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=20), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("opted_out_followups", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "customers",
        sa.Column("opted_out_reminders", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "source",
            sa.Enum("agent", "external", name="bookingsource", native_enum=False, length=20),
            nullable=False,
            server_default="agent",
        ),
    )

    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("booking_id", sa.BigInteger(), sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("template_name", sa.String(length=128), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False),
        *_send_columns(),
    )
    op.create_index("ix_reminders_booking_id", "reminders", ["booking_id"])
    op.create_index("ix_reminders_due", "reminders", ["status", "send_at"])

    op.create_table(
        "lead_interests",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("customer_id", sa.BigInteger(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_lead_interests_customer_id", "lead_interests", ["customer_id"])

    op.create_table(
        "follow_ups",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("customer_id", sa.BigInteger(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("template_name", sa.String(length=128), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False),
        *_send_columns(),
    )
    op.create_index("ix_follow_ups_customer_id", "follow_ups", ["customer_id"])
    op.create_index("ix_follow_ups_due", "follow_ups", ["status", "send_at"])

    op.create_table(
        "staff_alerts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column(
            "conversation_id", sa.BigInteger(), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column("contact", sa.String(length=255), nullable=False),
        sa.Column(
            "channel",
            sa.Enum("whatsapp", "email", name="alertchannel", native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        *_send_columns(),
    )
    op.create_index("ix_staff_alerts_conversation_id", "staff_alerts", ["conversation_id"])
    op.create_index("ix_staff_alerts_due", "staff_alerts", ["status", "send_at"])


def downgrade() -> None:
    op.drop_table("staff_alerts")
    op.drop_table("follow_ups")
    op.drop_table("lead_interests")
    op.drop_table("reminders")
    op.drop_column("bookings", "source")
    op.drop_column("customers", "opted_out_reminders")
    op.drop_column("customers", "opted_out_followups")
