"""Live smoke test on a real Postgres: ``python -m scripts.smoke_postgres [--api URL]``.

Unit tests run on SQLite, which can't prove the Postgres-only guarantees. This
script checks them against ``DATABASE_URL`` (run ``make migrate`` first):

1. **No double booking:** 6 threads, each with its own transaction, book the
   same slot through ``InternalProvider``. Exactly one must win (advisory lock).
2. **SKIP LOCKED queue:** 30 jobs, 5 threads claiming at once. Every job must
   be claimed exactly once.
3. **Exactly-once reminders (§2):** 40 due reminders, two scheduler
   runners draining at the same time (Meta replaced by a slow recorder).
   Every reminder must be sent exactly once.
   Same for staff alerts: 20 handoffs, the worker's immediate alert jobs and the
   scheduler's fallback pass running at the same time.
4. With ``--api`` (the API running, e.g. ``make dev``; **don't run the worker**):
   a signed webhook POST is stored and queued, one worker step answers it with
   the real LLM (Meta replaced by a recorder, so nothing is sent), and the
   staff inbox login works.

It uses the fake WhatsApp number 999000000001 (999 is not a country code) and
deletes everything it created. Exit code 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import logging
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select, text

from app.booking.base import BookingError, Slot
from app.booking.internal import InternalProvider
from app.config import get_settings
from app.db.models import (
    Booking,
    Conversation,
    Customer,
    Job,
    JobStatus,
    Message,
    Reminder,
    SendStatus,
    StaffAlert,
)
from app.db.session import get_engine, session_scope
from app.kb.loader import load_knowledge
from app.worker import queue

SMOKE_WA_ID = "999000000001"
SMOKE_JOB = "smoke_test"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")


def cleanup() -> None:
    with session_scope() as s:
        ids = select(Customer.id).where(Customer.wa_id.like("999%")).scalar_subquery()
        conv_ids = (
            select(Conversation.id).where(Conversation.customer_id.in_(ids)).scalar_subquery()
        )
        s.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        booking_ids = select(Booking.id).where(Booking.customer_id.in_(ids)).scalar_subquery()
        s.execute(delete(Reminder).where(Reminder.booking_id.in_(booking_ids)))
        s.execute(delete(StaffAlert).where(StaffAlert.conversation_id.in_(conv_ids)))
        s.execute(delete(Job).where(Job.type == "send_staff_alerts"))
        s.execute(delete(Booking).where(Booking.customer_id.in_(ids)))
        s.execute(delete(Conversation).where(Conversation.customer_id.in_(ids)))
        s.execute(delete(Customer).where(Customer.wa_id.like("999%")))
        s.execute(delete(Job).where(Job.type == SMOKE_JOB))
        s.execute(text("DELETE FROM processed_events WHERE wa_message_id LIKE 'wamid.SMOKE%'"))


def double_booking() -> None:
    settings = get_settings()
    kb = load_knowledge(settings.business_config_path, settings.faq_path, settings.style_guide_path)
    provider = InternalProvider(kb, settings.tz)
    # A service with a single staff member, so only one booking fits a slot.
    service = next(s for s in kb.services if len(kb.staff_for(s)) == 1)
    now = datetime.now(UTC)
    with session_scope() as s:
        customer = Customer(wa_id=SMOKE_WA_ID, name="Smoke test")
        s.add(customer)
        s.flush()
        customer_id = customer.id
        day = (now + timedelta(days=7)).astimezone(settings.tz).date()
        slots: list[Slot] = []
        while not slots:
            slots = provider.get_availability(s, service, day, day, None, now)
            day += timedelta(days=1)
        start = slots[0].start

    barrier = threading.Barrier(6)
    outcomes: list[str] = []

    def attempt() -> None:
        try:
            with session_scope() as s:
                cust = s.get(Customer, customer_id)
                assert cust is not None
                barrier.wait()
                provider.create(s, cust, service, start, None, None, now)
            outcomes.append("booked")
        except BookingError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with session_scope() as s:
        stored = (
            s.execute(select(Booking).where(Booking.customer_id == customer_id)).scalars().all()
        )
    check(
        "6 concurrent bookings of one slot -> exactly 1 stored",
        outcomes.count("booked") == 1 and len(stored) == 1,
        f"outcomes {sorted(outcomes)}, rows {len(stored)}",
    )


def skip_locked() -> None:
    with session_scope() as s:
        for i in range(30):
            queue.enqueue(s, SMOKE_JOB, {"n": i})
    claimed: list[int] = []
    lock = threading.Lock()

    def claimer() -> None:
        while True:
            with session_scope() as s:
                job = queue.claim_next(s, [SMOKE_JOB])
                if job is None:
                    return
                time.sleep(0.01)  # hold the row lock a moment, like real work
                queue.complete(s, job)
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=claimer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with session_scope() as s:
        done = (
            s.execute(select(Job).where(Job.type == SMOKE_JOB, Job.status == JobStatus.DONE))
            .scalars()
            .all()
        )
    check(
        "30 jobs, 5 claimers -> each claimed once",
        len(claimed) == 30 and len(set(claimed)) == 30 and len(done) == 30,
        f"claimed {len(claimed)}, unique {len(set(claimed))}",
    )


def reminders_exactly_once() -> None:
    from app.scheduler.reminders import ReminderSender
    from app.scheduler.sending import drain
    from app.whatsapp.client import WhatsAppClient
    from app.whatsapp.templates import load_catalog

    settings = get_settings()
    kb = load_knowledge(settings.business_config_path, settings.faq_path, settings.style_guide_path)
    now = datetime.now(UTC)
    with session_scope() as s:
        customer = Customer(wa_id="999000000002", name="Smoke reminders", language="en")
        s.add(customer)
        s.flush()
        start = now + timedelta(days=2)
        booking = Booking(
            customer_id=customer.id,
            service_id=kb.services[0].id,
            start_at=start,
            end_at=start + timedelta(minutes=30),
        )
        s.add(booking)
        s.flush()
        for i in range(40):
            s.add(
                Reminder(
                    booking_id=booking.id,
                    send_at=now - timedelta(seconds=i),
                    template_name=settings.template_reminder,
                    language="en",
                )
            )

    sent: list[str] = []
    lock = threading.Lock()

    def meta(request: httpx.Request) -> httpx.Response:
        time.sleep(0.02)  # a slow Meta keeps each row locked while the other runner looks
        body = json.loads(request.content)
        payload = body["template"]["components"][1]["parameters"][0]["payload"]
        with lock:
            sent.append(payload)
            n = len(sent)
        return httpx.Response(200, json={"messages": [{"id": f"wamid.SMOKE_REM{n}"}]})

    client = WhatsAppClient(settings, http=httpx.Client(transport=httpx.MockTransport(meta)))
    sender = ReminderSender(settings, kb, client, load_catalog(settings))
    barrier = threading.Barrier(2)

    def runner() -> None:
        barrier.wait()
        drain(Reminder, sender, lambda: datetime.now(UTC))

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with session_scope() as s:
        statuses = (
            s.execute(select(Reminder.status).where(Reminder.booking_id == booking.id))
            .scalars()
            .all()
        )
    check(
        "40 due reminders, 2 concurrent schedulers -> each sent exactly once",
        len(sent) == 40 and len(set(sent)) == 40 and set(statuses) == {SendStatus.SENT},
        f"sends {len(sent)}, unique {len(set(sent))}, statuses {sorted(set(statuses))}, "
        f"dupes {sorted({p for p in sent if sent.count(p) > 1})}",
    )


def alerts_exactly_once() -> None:
    from app.alerts.service import AlertJob, AlertSender, queue_handoff_alerts
    from app.conversation.tools import hand_off
    from app.scheduler.sending import drain
    from app.whatsapp.client import WhatsAppClient
    from app.worker.runner import Worker

    settings = get_settings()
    now = datetime.now(UTC)
    with session_scope() as s:
        for i in range(20):
            customer = Customer(wa_id=f"99900100{i:04d}", name="Smoke alert")
            s.add(customer)
            s.flush()
            conv = Conversation(customer_id=customer.id, last_customer_msg_at=now)
            s.add(conv)
            s.flush()
            hand_off(conv, "other", "smoke")
            queue_handoff_alerts(s, settings, conv, now)  # rows + one send job each

    sent: list[str] = []
    lock = threading.Lock()

    def meta(request: httpx.Request) -> httpx.Response:
        time.sleep(0.02)
        with lock:
            sent.append(request.content.decode())
            n = len(sent)
        return httpx.Response(200, json={"messages": [{"id": f"wamid.SMOKE_ALERT{n}"}]})

    client = WhatsAppClient(settings, http=httpx.Client(transport=httpx.MockTransport(meta)))
    sender = AlertSender(settings, client)
    worker = Worker({"send_staff_alerts": AlertJob(sender).handle})
    barrier = threading.Barrier(2)

    def run_worker() -> None:
        barrier.wait()
        while worker.run_once():
            pass

    def run_scheduler() -> None:
        barrier.wait()
        drain(StaffAlert, sender, lambda: datetime.now(UTC))

    threads = [threading.Thread(target=run_worker), threading.Thread(target=run_scheduler)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with session_scope() as s:
        statuses = (
            s.execute(
                select(StaffAlert.status)
                .join(Conversation, Conversation.id == StaffAlert.conversation_id)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(Customer.name == "Smoke alert")
            )
            .scalars()
            .all()
        )
    check(
        "20 handoff alerts, worker + scheduler at once -> each sent exactly once",
        len(sent) == 20 and len(statuses) == 20 and set(statuses) == {SendStatus.SENT},
        f"sends {len(sent)}, rows {len(statuses)}, statuses {sorted(set(statuses))}",
    )


def api_pipeline(api: str) -> None:
    from app.booking.base import build_booking_provider
    from app.conversation.agent import AgentReplier
    from app.conversation.handler import InboundHandler
    from app.llm.base import build_llm_client
    from app.whatsapp.client import WhatsAppClient
    from app.worker.runner import Worker

    settings = get_settings()
    http = httpx.Client(base_url=api, timeout=10, follow_redirects=False)
    check("API /healthz", http.get("/healthz").status_code == 200)

    payload: dict[str, Any] = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": SMOKE_WA_ID, "profile": {"name": "Smoke"}}],
                            "messages": [
                                {
                                    "id": "wamid.SMOKE1",
                                    "from": SMOKE_WA_ID,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": "What are your opening hours on Saturday?"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode()
    secret = settings.whatsapp_app_secret.get_secret_value().encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    r = http.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    check("signed webhook accepted", r.status_code == 200, f"HTTP {r.status_code}")
    r = http.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"})
    check("bad signature rejected", r.status_code == 401, f"HTTP {r.status_code}")

    sent: list[dict[str, Any]] = []

    def fake_meta(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.SMOKEOUT{len(sent)}"}]})

    kb = load_knowledge(settings.business_config_path, settings.faq_path, settings.style_guide_path)
    wa = WhatsAppClient(settings, http=httpx.Client(transport=httpx.MockTransport(fake_meta)))
    replier = AgentReplier(
        settings, kb, build_llm_client(settings), build_booking_provider(settings, kb)
    )
    worker = Worker({queue.JOB_HANDLE_INBOUND: InboundHandler(settings, wa, replier, kb=kb).handle})
    worker.run_once()
    reply = sent[0]["text"]["body"] if sent and sent[0].get("type") == "text" else ""
    check("worker answered with the real LLM", bool(reply), reply[:120].replace("\n", " "))

    password = settings.inbox_auth_secret.get_secret_value()
    r = http.post(
        "/inbox/login",
        content=f"password={password}".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    cookie = r.cookies.get("inbox_session")
    check("inbox login", r.status_code == 303 and bool(cookie), f"HTTP {r.status_code}")
    page = http.get("/inbox", cookies={"inbox_session": cookie or ""})
    check("inbox page renders", page.status_code == 200 and "Staff inbox" in page.text)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--api", help="base URL of the running API, e.g. http://localhost:8080")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)

    dialect = get_engine().dialect.name
    if dialect != "postgresql":
        print(f"DATABASE_URL points at {dialect}; this smoke test needs Postgres.")
        return 2
    cleanup()
    try:
        double_booking()
        skip_locked()
        reminders_exactly_once()
        alerts_exactly_once()
        if args.api:
            api_pipeline(args.api)
    finally:
        cleanup()
    failed = [n for n, ok, _ in results if not ok]
    print("RESULT:", "PASS" if not failed else f"FAIL ({len(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
