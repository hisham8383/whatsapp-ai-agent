"""SQLAlchemy ORM models, which are the data model from CLAUDE.md §7.

PDPL data minimization (§2): these tables hold only what the workflow needs,
which is the WhatsApp number, name, bookings, and conversation text for the
retention period. There are no columns for health details, national IDs, or
payment data, and none may be added without revisiting the brief.

All timestamps are timezone-aware UTC. Conversion to ``BUSINESS_TIMEZONE``
happens only at display time (§6).

Columns beyond the §7 list, and why:
    * ``conversations.handed_off_at``: needed to apply ``HANDOFF_AUTO_RELEASE_HOURS``.
    * ``messages.error_code``: status webhooks log failures with the error code (§4).
    * ``jobs.last_error``: the reason a job was retried or gave up.
    * ``conversations.pending_action``: the booking change the bot asked the
      customer to confirm. The confirmation guard (§5) only runs a booking tool
      that matches it (see ``app/conversation/tools.py``).
    * ``conversations.bot_failures``: consecutive failed bot turns. Two in a row
      hand the conversation to staff (§5).
    * ``reminders`` / ``follow_ups`` / ``staff_alerts``: ``error_code`` and
      ``sent_at`` (a failed send is retried once, §4); ``staff_alerts.send_at``
      and ``attempts`` so all three share the scheduler's claim-and-send loop.
    * ``follow_ups.service_id``: the ``lead_followup`` template names the service.
    * ``lead_interests.handled_at``: each lead is considered for exactly one
      follow-up (§5 "one per lead").
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Current time as an aware UTC datetime. Use it for every default."""
    return datetime.now(UTC)


# SQLite (used in unit tests) only supports autoincrement on INTEGER primary
# keys, so BIGINT falls back to INTEGER there.
PK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    """Declarative base for all models."""


class ConversationState(StrEnum):
    """Per-conversation state machine (CLAUDE.md §5)."""

    BOT = "BOT"  # the assistant replies
    HUMAN = "HUMAN"  # handed off, so the bot stays silent


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Sender(StrEnum):
    """Who wrote a message. Staff messages come from the inbox (§5)."""

    CUSTOMER = "customer"
    BOT = "bot"
    STAFF = "staff"


class MessageStatus(StrEnum):
    """Delivery status. Meta status webhooks move outbound messages through these."""

    RECEIVED = "received"  # inbound
    PENDING = "pending"  # outbound, not yet accepted by Meta
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class BookingSource(StrEnum):
    AGENT = "agent"  # booked through the WhatsApp agent
    EXTERNAL = "external"  # made elsewhere (phone, walk-in) and found by booking sync


class SendStatus(StrEnum):
    """Lifecycle of a scheduled template send (reminder, follow-up, staff alert)."""

    PENDING = "pending"  # waiting for send_at
    SENT = "sent"  # accepted by Meta (or the mail server); wa_message_id stored
    FAILED = "failed"  # gave up after the retry (§4: retried once)
    CANCELLED = "cancelled"  # no longer wanted (booking cancelled/moved, opt-out)
    SKIPPED = "skipped"  # due, but a rule said no at send time (slot passed, opted out)


class AlertChannel(StrEnum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"  # attempts exhausted


def _enum(cls: type[StrEnum]) -> SAEnum:
    # native_enum=False stores enums as VARCHAR + CHECK. Adding a value later is
    # then a plain migration instead of ALTER TYPE.
    return SAEnum(cls, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e])


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    wa_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    language: Mapped[str | None] = mapped_column(String(5))
    # Opt-outs (§4). Stored so they survive restarts and retention: once set, no
    # lead_followup (or reminder) template ever goes to this number again.
    opted_out_followups: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_reminders: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversations: Mapped[list[Conversation]] = relationship(back_populates="customer")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    state: Mapped[ConversationState] = mapped_column(
        _enum(ConversationState), default=ConversationState.BOT
    )
    # Starts the 24h customer-service window (§4). It is updated on every inbound
    # message, and the send client refuses any reply once it is 24h old.
    last_customer_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_reason: Mapped[str | None] = mapped_column(Text)
    # Written by the bot at handoff so staff can pick up without reading the whole thread.
    handoff_summary: Mapped[str | None] = mapped_column(Text)
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Customer messages staff haven't seen yet. Drives the inbox title count (§5).
    unread_for_staff: Mapped[int] = mapped_column(Integer, default=0)
    # Set when the bot asks "shall I book X?"; valid for the customer's next message only.
    pending_action: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    bot_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    customer: Mapped[Customer] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.created_at"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    sender: Mapped[Sender] = mapped_column(_enum(Sender))
    # Unique so a replayed webhook can never create a second row (§4 idempotency).
    # Nullable because outbound messages get their id from Meta only after sending.
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    type: Mapped[str] = mapped_column(String(20))  # text | interactive | image | audio | ...
    # Nulled by the retention task after LOG_RETENTION_DAYS (§7, PDPL).
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MessageStatus] = mapped_column(_enum(MessageStatus))
    error_code: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ProcessedEvent(Base):
    """Dedupe ledger. Inserting an id that already exists means the event is a duplicate (§4).

    Rows older than 7 days are deleted by the retention task (§7). Meta stops
    retrying long before that.
    """

    __tablename__ = "processed_events"

    wa_message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255))  # id in the provider's system
    service_id: Mapped[str] = mapped_column(String(64))
    staff_id: Mapped[str | None] = mapped_column(String(64))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus), default=BookingStatus.CONFIRMED
    )
    # "agent" = booked through WhatsApp; "external" = found by booking sync (§6).
    source: Mapped[BookingSource] = mapped_column(_enum(BookingSource), default=BookingSource.AGENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reminder(Base):
    """One ``appointment_reminder`` template send, one row per ``REMINDER_OFFSETS`` entry (§5).

    Created and replaced by ``app/scheduler/reminders.py:sync_for_booking``
    whenever a booking is created, moved or cancelled; sent exactly once by the
    scheduler (§2 "never double-send").
    """

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_due", "status", "send_at"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    template_name: Mapped[str] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(5))
    status: Mapped[SendStatus] = mapped_column(_enum(SendStatus), default=SendStatus.PENDING)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(20))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeadInterest(Base):
    """A customer asked about a service without booking it (``mark_lead_interest``, §5)."""

    __tablename__ = "lead_interests"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    service_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set once the scheduler has decided about this interest (follow-up queued or
    # skipped), so each lead gets at most one follow-up.
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FollowUp(Base):
    """One ``lead_followup`` template send (§5). At most one per customer per cooldown."""

    __tablename__ = "follow_ups"
    __table_args__ = (Index("ix_follow_ups_due", "status", "send_at"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    service_id: Mapped[str] = mapped_column(String(64))  # the template names the service
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    template_name: Mapped[str] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(5))
    status: Mapped[SendStatus] = mapped_column(_enum(SendStatus), default=SendStatus.PENDING)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(20))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StaffAlert(Base):
    """One handoff alert to one staff contact (§5): WhatsApp template or email."""

    __tablename__ = "staff_alerts"
    __table_args__ = (Index("ix_staff_alerts_due", "status", "send_at"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    # Staff WhatsApp number (digits only) or email address. Staff contacts, not
    # customers, but still masked in logs.
    contact: Mapped[str] = mapped_column(String(255))
    channel: Mapped[AlertChannel] = mapped_column(_enum(AlertChannel))
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[SendStatus] = mapped_column(_enum(SendStatus), default=SendStatus.PENDING)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(20))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    """Postgres-backed work queue (§3). See ``app/worker/queue.py``."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_ready", "status", "run_at"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus), default=JobStatus.QUEUED)
    last_error: Mapped[str | None] = mapped_column(Text)
