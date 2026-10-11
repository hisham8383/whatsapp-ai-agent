"""Run the eval set against the real reply path: ``make eval``.

Each case is a scripted WhatsApp conversation. Every customer message goes
through the production code: ``webhook.service.ingest`` → job queue →
``Worker`` → ``InboundHandler`` → ``AgentReplier`` (real LLM calls) → booking
tools on ``InternalProvider``. Only two things are swapped:

* the database is a fresh in-memory SQLite per attempt, seeded from the case;
* the Meta Graph API is a recorder, so nothing is sent to WhatsApp.

The booking clock is pinned by the case file's ``now``, so "tomorrow" and the
free slots are the same on every run.

Writes to ``evals/results/<variant>/``:
    results.jsonl     one row per scored attempt (checks, usage, latency)
    traces/<id>_rep<k>.json   the conversation that was graded
    errors.jsonl      attempts that never produced a gradable result
                      (API error, wrong model served, harness or grader failure)

Exit code 0 only if every attempt of every case passed and nothing errored:
that is the delivery gate from CLAUDE.md §9.

    python -m evals.runner --cases examples/demo-clinic/eval-cases.yaml
    python -m evals.runner --cases ... --reps 1 --only book-
    python -m evals.runner                       # evals/cases.yaml (this client)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import shutil
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.booking.base import as_utc
from app.booking.internal import InternalProvider
from app.config import Settings, get_settings
from app.conversation.agent import AgentReplier
from app.conversation.handler import InboundHandler
from app.conversation.timefmt import for_model, parse_local
from app.db.models import (
    Base,
    Booking,
    BookingStatus,
    Conversation,
    Customer,
    Job,
    JobStatus,
    LeadInterest,
    Reminder,
    SendStatus,
)
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase, load_knowledge, render_knowledge
from app.llm.base import LLMClient, LLMReply, ToolSpec, Turn, build_llm_client
from app.webhook.parse import InboundMessage, ParsedWebhook
from app.webhook.service import ingest
from app.whatsapp.client import WhatsAppClient
from app.worker.queue import JOB_HANDLE_INBOUND
from app.worker.runner import Worker
from evals.grading import DIALECT_RUBRIC, Grade, in_language, judge, missing_facts

ROOT = Path(__file__).resolve().parent.parent
CASES_FILE = ROOT / "evals" / "cases.yaml"
RESULTS_DIR = ROOT / "evals" / "results"
EVAL_WA_ID = "966500000000"  # never a real number; nothing is sent to WhatsApp
OTHER_WA_ID = "966500000001"

# $ per million tokens (first-party API). Used only to print the run's cost.
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-5": (2.0, 10.0)}


# ---------------------------------------------------------------- plumbing


class Pacer:
    """Spaces out API calls across threads to stay under the account's RPM limit."""

    def __init__(self, rpm: int) -> None:
        self._gap = 60.0 / rpm
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._gap
        time.sleep(max(0.0, start - now))


class RecordingLLM:
    """Pass-through wrapper that sums usage and remembers errors and served models."""

    def __init__(self, inner: LLMClient, pacer: Pacer) -> None:
        self._inner, self._pacer = inner, pacer
        self.replies: list[LLMReply] = []
        self.errors: list[str] = []
        self.seconds = 0.0

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMReply:
        self._pacer.wait()
        started = time.monotonic()
        try:
            reply = self._inner.complete(system, history, tools)
        except ExternalServiceError as exc:
            self.errors.append(str(exc))
            raise
        finally:
            self.seconds += time.monotonic() - started
        self.replies.append(reply)
        return reply

    def usage(self) -> dict[str, int]:
        return {
            "input_tokens": sum(r.input_tokens for r in self.replies),
            "output_tokens": sum(r.output_tokens for r in self.replies),
            "cache_read_input_tokens": sum(r.cache_read_tokens for r in self.replies),
            "cache_creation_input_tokens": sum(r.cache_write_tokens for r in self.replies),
        }


class MetaRecorder:
    """Stands in for the Graph API: records sends and answers with a fresh wamid."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.OUT{len(self.sent)}"}]})


def describe_send(
    body: dict[str, Any],
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """(text, list rows, reply buttons) of one recorded send."""
    if body.get("type") == "interactive":
        inter = body["interactive"]
        if inter["type"] == "button":
            buttons = [b["reply"] for b in inter["action"]["buttons"]]
            return inter["body"]["text"], [], buttons
        rows = [r for sec in inter["action"]["sections"] for r in sec["rows"]]
        return inter["body"]["text"], rows, []
    return body["text"]["body"], [], []


# ---------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    id: str
    language: str
    # text, {"pick": n} (row n of the last list), {"tap": n} (button n),
    # {"tap_reminder": "confirm" | "reschedule"} (quick reply on the seeded reminder)
    turns: list[Any]
    seed: list[dict[str, Any]]
    fill_day: dict[str, str] | None
    profile_name: str | None
    expect: dict[str, Any]


@dataclass(frozen=True)
class Suite:
    now: datetime
    settings: dict[str, Any]
    knowledge: tuple[Path, Path]
    style_guide: Path | None
    cases: list[Case]


class CasesError(ValueError):
    """The case file is malformed or hasn't been filled in for this client."""


def load_suite(path: Path, only: str | None) -> Suite:
    """Load and sanity-check the case file.

    Raises:
        CasesError: Missing keys or no cases (e.g. the unfilled template).
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        knowledge = (ROOT / raw["knowledge"]["business"], ROOT / raw["knowledge"]["faqs"])
        style = raw["knowledge"].get("style_guide")
        now_text = str(raw["now"])
        items = raw["cases"] or []
    except (KeyError, TypeError) as exc:
        raise CasesError(f"{path}: needs knowledge, now and cases ({exc})") from exc
    if not items:
        raise CasesError(
            f"{path}: no cases. Write them from this client's knowledge files "
            "(worked example: examples/demo-clinic/eval-cases.yaml)."
        )
    cases = [
        Case(
            id=c["id"],
            language=c["language"],
            turns=c["turns"],
            seed=c.get("seed", []),
            fill_day=c.get("fill_day"),
            profile_name=c.get("profile_name"),
            expect=c.get("expect", {}),
        )
        for c in items
    ]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise CasesError(f"{path}: duplicate case ids")
    return Suite(
        now=_now(now_text, raw.get("settings", {}).get("business_timezone")),
        settings=raw.get("settings", {}),
        knowledge=knowledge,
        style_guide=ROOT / style if style else None,
        cases=[c for c in cases if not only or c.id.startswith(only)],
    )


def _now(text: str, tz_name: str | None) -> datetime:
    from zoneinfo import ZoneInfo

    return parse_local(text, ZoneInfo(tz_name or "Asia/Riyadh"))


def price(model: str, usage: dict[str, int]) -> float:
    rates = next((p for m, p in PRICES.items() if model.startswith(m)), None)
    if rates is None:
        return 0.0
    rin, rout = rates
    return (
        usage.get("input_tokens", 0) * rin
        + usage.get("cache_creation_input_tokens", 0) * rin * 1.25
        + usage.get("cache_read_input_tokens", 0) * rin * 0.1
        + usage.get("output_tokens", 0) * rout
    ) / 1e6


# ---------------------------------------------------------------- one attempt


@dataclass
class Outcome:
    case: Case
    rep: int
    row: dict[str, Any] | None = None  # scored attempt
    error: dict[str, Any] | None = None  # attempt that could not be scored


@dataclass
class Transcript:
    lines: list[str] = field(default_factory=list)
    bot_texts: list[str] = field(default_factory=list)
    last_rows: list[dict[str, str]] = field(default_factory=list)
    last_buttons: list[dict[str, str]] = field(default_factory=list)
    all_rows: list[dict[str, str]] = field(default_factory=list)
    lists_sent: int = 0
    final_reply: str = ""


REMINDER_TAP_TEXT = {
    ("confirm", "ar"): "تأكيد",
    ("confirm", "en"): "Confirm",
    ("reschedule", "ar"): "تغيير الموعد",
    ("reschedule", "en"): "Reschedule",
}


def _seed(
    session: Session, case: Case, kb: KnowledgeBase, settings: Settings, now: datetime
) -> int | None:
    """Seed the case's database. Returns the id of the reminder sent for a
    ``reminded: true`` booking, if any (its quick replies carry that id)."""
    reminder_id: int | None = None
    me = Customer(wa_id=EVAL_WA_ID, name=case.profile_name)
    other = Customer(wa_id=OTHER_WA_ID, name="Other customer")
    session.add_all([me, other])
    session.flush()
    for s in case.seed:
        service = kb.service(s["service"])
        if service is None:
            raise CasesError(f"{case.id}: unknown service {s['service']}")
        start = parse_local(s["start"], settings.tz)
        booking = Booking(
            customer_id=me.id if s.get("mine", True) else other.id,
            service_id=service.id,
            staff_id=s.get("staff") or (kb.staff_for(service)[0]),
            start_at=start,
            end_at=start + timedelta(minutes=service.duration_minutes),
            status=BookingStatus.CONFIRMED,
        )
        session.add(booking)
        if s.get("reminded"):
            session.flush()
            reminder = Reminder(
                booking_id=booking.id,
                send_at=now - timedelta(hours=1),
                template_name=settings.template_reminder,
                language=case.language,
                status=SendStatus.SENT,
                wa_message_id="wamid.EVAL_REMINDER",
                sent_at=now - timedelta(hours=1),
            )
            session.add(reminder)
            session.flush()
            reminder_id = reminder.id
    if case.fill_day:
        # Book every free slot of that service on that day for someone else.
        service = kb.service(case.fill_day["service"])
        if service is None:
            raise CasesError(f"{case.id}: unknown service in fill_day")
        provider = InternalProvider(kb, settings.tz)
        day = datetime.fromisoformat(case.fill_day["date"]).date()
        while slots := provider.get_availability(session, service, day, day, None, now):
            provider.create(session, other, service, slots[0].start, None, None, now)
    session.flush()
    return reminder_id


def _my_bookings(session: Session, settings: Settings) -> list[dict[str, str]]:
    rows = session.execute(
        select(Booking)
        .join(Customer, Customer.id == Booking.customer_id)
        .where(Customer.wa_id == EVAL_WA_ID)
        .order_by(Booking.start_at)
    ).scalars()
    return [
        {
            "service": b.service_id,
            "start": for_model(as_utc(b.start_at), settings.tz),
            "status": b.status.value,
            **({"staff": b.staff_id} if b.staff_id else {}),
        }
        for b in rows
    ]


def run_attempt(
    case: Case,
    rep: int,
    suite: Suite,
    settings: Settings,
    kb: KnowledgeBase,
    pacer: Pacer,
    judge_client: anthropic.Anthropic,
    knowledge_text: str,
) -> Outcome:
    base = {"prompt_id": case.id, "rep": rep}
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    meta = MetaRecorder()
    wa = WhatsAppClient(settings, http=httpx.Client(transport=httpx.MockTransport(meta.handler)))
    llm = RecordingLLM(build_llm_client(settings), pacer)
    replier = AgentReplier(
        settings, kb, llm, InternalProvider(kb, settings.tz), clock=lambda: suite.now
    )
    handler = InboundHandler(settings, wa, replier, clock=lambda: suite.now, kb=kb)
    worker = Worker({JOB_HANDLE_INBOUND: handler.handle})
    with factory() as s:
        reminder_id = _seed(s, case, kb, settings, suite.now)
        seeded = _my_bookings(s, settings)
        s.commit()

    tr = Transcript()
    for i, turn in enumerate(case.turns):
        if isinstance(turn, dict) and "tap_reminder" in turn:
            action = str(turn["tap_reminder"])
            if reminder_id is None:
                raise CasesError(f"{case.id}: tap_reminder needs a seed with reminded: true")
            label = REMINDER_TAP_TEXT[(action, case.language)]
            msg = InboundMessage(
                f"wamid.EVAL{i}",
                EVAL_WA_ID,
                case.profile_name,
                "button",
                label,
                f"rem:{reminder_id}:{action}",
                "button",
                datetime.now(UTC),
            )
            tr.lines.append(f"Customer (tapped reminder button): {label}")
        elif isinstance(turn, dict) and ("pick" in turn or "tap" in turn):
            kind = "pick" if "pick" in turn else "tap"
            options = tr.last_rows if kind == "pick" else tr.last_buttons
            n = int(turn[kind])
            if len(options) < n:
                tr.lines.append(f"[harness: no {kind} option {n}]")
                break
            chosen = options[n - 1]
            msg = InboundMessage(
                f"wamid.EVAL{i}",
                EVAL_WA_ID,
                case.profile_name,
                "interactive",
                chosen["title"],
                chosen["id"],
                "interactive",
                datetime.now(UTC),
            )
            verb = "picked from list" if kind == "pick" else "tapped button"
            tr.lines.append(f"Customer ({verb}): {chosen['title']}")
        else:
            msg = InboundMessage(
                f"wamid.EVAL{i}",
                EVAL_WA_ID,
                case.profile_name,
                "text",
                str(turn),
                None,
                "text",
                datetime.now(UTC),
            )
            tr.lines.append(f"Customer: {turn}")
        with factory() as s:
            ingest(s, ParsedWebhook(messages=[msg], statuses=[]))
            s.commit()
        before = len(meta.sent)
        worker.run_once(factory)
        with factory() as s:
            job = (
                s.execute(
                    select(Job)
                    .where(Job.type == JOB_HANDLE_INBOUND)  # alert jobs aren't run in evals
                    .order_by(Job.id.desc())
                )
                .scalars()
                .first()
            )
            if job is None or job.status is not JobStatus.DONE:
                detail = job.last_error if job else "no job"
                return Outcome(
                    case, rep, error={**base, "class": "harness_error", "detail": detail}
                )
        for body in meta.sent[before:]:
            text, rows, buttons = describe_send(body)
            tr.bot_texts.append(text)
            tr.final_reply = text
            tr.last_buttons = buttons
            if buttons:
                labels = " | ".join(b["title"] for b in buttons)
                tr.lines.append(f"Assistant: {text}\n  [buttons: {labels}]")
            elif rows:
                tr.lists_sent += 1
                tr.last_rows = rows
                tr.all_rows += rows
                titles = "; ".join(r["title"] for r in rows)
                tr.lines.append(f"Assistant: {text}\n  [tappable list: {titles}]")
            else:
                tr.lines.append(f"Assistant: {text}")

    if llm.errors:
        return Outcome(case, rep, error={**base, "class": "api_error", "detail": llm.errors[0]})
    served = {r.model for r in llm.replies}
    wanted = settings.llm_model
    if any(not (m.startswith(wanted) or wanted.startswith(m)) for m in served):
        detail = f"served {sorted(served)}, requested {wanted}"
        return Outcome(case, rep, error={**base, "class": "model_mismatch", "detail": detail})

    with factory() as s:
        conv = s.execute(select(Conversation)).scalars().first()
        state = conv.state.value if conv else "none"
        bookings = _my_bookings(s, settings)
        me = s.execute(select(Customer).where(Customer.wa_id == EVAL_WA_ID)).scalar_one()
        opted = {"followups": me.opted_out_followups, "reminders": me.opted_out_reminders}
        interests = sorted(
            s.execute(
                select(LeadInterest.service_id).where(LeadInterest.customer_id == me.id)
            ).scalars()
        )

    grade = Grade(passed=True)
    exp = case.expect
    if "state" in exp:
        grade.checks["state"] = state == exp["state"]
        if not grade.checks["state"]:
            grade.notes.append(f"state {state}, expected {exp['state']}")
    if "bookings" in exp:
        want = [{k: str(v) for k, v in b.items()} for b in exp["bookings"]]
        got = [
            {k: v for k, v in b.items() if k in ("service", "start", "status", "staff")}
            for b in bookings
        ]
        got_cmp = [{k: v for k, v in g.items() if k in w} for g, w in zip(got, want, strict=False)]
        ok = len(got) == len(want) and got_cmp == want
        grade.checks["bookings"] = ok
        if not ok:
            grade.notes.append(f"bookings {bookings}, expected {want}")
    if exp.get("no_booking_changes"):
        grade.checks["no_booking_changes"] = bookings == seeded
        if bookings != seeded:
            grade.notes.append(f"bookings changed: {bookings} (seeded {seeded})")
    if "facts" in exp:
        miss = missing_facts(tr.final_reply, exp["facts"])
        grade.checks["facts"] = not miss
        if miss:
            grade.notes.append(f"missing facts {miss}")
    if "no_offer_on" in exp:
        day = str(exp["no_offer_on"])
        offered_day = any(day in r["id"] for r in tr.all_rows)
        grade.checks["no_offer_on"] = not offered_day
        if offered_day:
            grade.notes.append(f"offered a time on {day}")
    if "opted_out" in exp:
        want_opt = str(exp["opted_out"])  # followups | reminders | none
        ok = (
            not any(opted.values())
            if want_opt == "none"
            else opted.get(want_opt, False) and sum(opted.values()) == 1
        )
        grade.checks["opted_out"] = ok
        if not ok:
            grade.notes.append(f"opt-outs {opted}, expected {want_opt}")
    if "lead_interest" in exp:
        want_lead = [] if exp["lead_interest"] in (None, "none") else [str(exp["lead_interest"])]
        grade.checks["lead_interest"] = interests == want_lead
        if interests != want_lead:
            grade.notes.append(f"lead interests {interests}, expected {want_lead}")
    if exp.get("list_offered"):
        grade.checks["list_offered"] = tr.lists_sent > 0
        if not tr.lists_sent:
            grade.notes.append("no interactive slot list was sent")
    grade.checks["replied"] = bool(tr.bot_texts)
    grade.checks["language"] = all(in_language(t, case.language) for t in tr.bot_texts)
    if not grade.checks["language"]:
        grade.notes.append(f"a reply isn't in {case.language}")

    transcript = "\n".join(tr.lines)
    judge_note: dict[str, Any] = {}
    criteria = "\n".join(
        c for c in (str(exp.get("judge") or ""), DIALECT_RUBRIC if exp.get("dialect") else "") if c
    )
    if criteria:
        try:
            pacer.wait()
            verdict = judge(judge_client, knowledge_text, transcript, criteria)
        except (anthropic.APIError, ValueError, KeyError, StopIteration) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            return Outcome(case, rep, error={**base, "class": "grader_error", "detail": detail})
        grade.checks.update(verdict.checks)
        grade.notes += verdict.notes
        judge_note = {"judge_model": verdict.judge_model, "judge_usage": verdict.judge_usage}
    grade.passed = all(grade.checks.values())

    model = next(iter(served), wanted)
    row: dict[str, Any] = {
        **base,
        "tags": [case.id.split("-")[0], case.language],
        "grade": {"pass": 1.0 if grade.passed else 0.0},
        "explanation": {"pass": "; ".join(grade.notes) or "ok"},
        "checks": grade.checks,
        "model": model,
        "usage": llm.usage(),
        "llm_calls": len(llm.replies),
        "latency_s": round(llm.seconds, 2),
        "meta": {"state": state, "bookings": bookings, "final_reply": tr.final_reply},
        **judge_note,
        "trace": transcript,
    }
    return Outcome(case, rep, row=row)


def _guarded(case: Case, rep: int, *args: Any) -> Outcome:
    """Any unexpected crash becomes a recorded harness error instead of killing the run."""
    try:
        return run_attempt(case, rep, *args)
    except Exception as exc:  # one bad case must not end the run
        return Outcome(
            case,
            rep,
            error={
                "prompt_id": case.id,
                "rep": rep,
                "class": "harness_error",
                "detail": f"{type(exc).__name__}: {exc}",
            },
        )


# ---------------------------------------------------------------- whole run


def write_outputs(out: Path, outcomes: list[Outcome]) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "traces").mkdir(parents=True)
    with (
        (out / "results.jsonl").open("w", encoding="utf-8") as results,
        (out / "errors.jsonl").open("w", encoding="utf-8") as errors,
    ):
        for o in outcomes:
            if o.error:
                errors.write(json.dumps(o.error, ensure_ascii=False) + "\n")
                continue
            assert o.row is not None
            trace = o.row.pop("trace")
            (out / "traces" / f"{o.case.id}_rep{o.rep}.txt").write_text(trace, encoding="utf-8")
            results.write(json.dumps(o.row, ensure_ascii=False) + "\n")


def summarize(outcomes: list[Outcome], reps: int, show_traces: bool) -> bool:
    by_case: dict[str, list[Outcome]] = {}
    for o in outcomes:
        by_case.setdefault(o.case.id, []).append(o)

    all_ok = True
    print(f"\n{'case':<24} {'lang':<5} result")
    for cid, attempts in by_case.items():
        passed = sum(1 for o in attempts if o.row and o.row["grade"]["pass"] == 1.0)
        errors = sum(1 for o in attempts if o.error)
        ok = passed == reps and errors == 0
        all_ok &= ok
        extra = f"  ({errors} error)" if errors else ""
        mark = "PASS" if ok else "FAIL"
        print(f"{cid:<24} {attempts[0].case.language:<5} {mark} {passed}/{reps}{extra}")
        for o in attempts:
            if o.row and o.row["grade"]["pass"] != 1.0:
                print(f"    rep{o.rep}: {o.row['explanation']['pass']}")
                if show_traces:
                    print("    " + o.row.get("trace", "").replace("\n", "\n    "))
            if o.error:
                print(f"    rep{o.rep}: {o.error['class']}: {o.error['detail']}")

    rows = [o.row for o in outcomes if o.row]
    cost = sum(price(r["model"], r["usage"]) for r in rows) + sum(
        price(r["judge_model"], r["judge_usage"]) for r in rows if r.get("judge_model")
    )
    lat = sorted(r["latency_s"] for r in rows)
    n_pass = sum(
        1
        for a in by_case.values()
        if len(a) == reps and all(o.row and o.row["grade"]["pass"] == 1.0 for o in a)
    )
    turns = sum(len(o.case.turns) for o in outcomes if o.row)
    bot_cost = sum(price(r["model"], r["usage"]) for r in rows)
    print(
        f"\n{n_pass}/{len(by_case)} cases passed all {reps} reps | "
        f"{sum(1 for o in outcomes if o.error)} errors | cost ${cost:.3f} "
        f"(bot ${bot_cost:.3f}, ≈${bot_cost / max(turns, 1):.4f} per customer message)"
    )
    if lat:
        print(
            f"LLM time per conversation: median {statistics.median(lat):.1f}s, max {lat[-1]:.1f}s"
        )
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--cases", type=Path, default=CASES_FILE, help="case file (YAML)")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--only", help="run only case ids starting with this prefix")
    parser.add_argument("--variant", default="baseline", help="results subdirectory")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rpm", type=int, default=45, help="max API calls per minute")
    parser.add_argument("--traces", action="store_true", help="print failing conversations")
    args = parser.parse_args()

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Arabic output on Windows consoles
    logging.disable(logging.CRITICAL)  # the app's own logs would drown the summary

    try:
        suite = load_suite(args.cases, args.only)
    except CasesError as exc:
        print(exc)
        return 1
    if not suite.cases:
        print("no cases match", args.only)
        return 1
    settings = get_settings().model_copy(
        update={
            **suite.settings,
            "business_config_path": str(suite.knowledge[0]),
            "faq_path": str(suite.knowledge[1]),
            **({"style_guide_path": str(suite.style_guide)} if suite.style_guide else {}),
        }
    )
    kb = load_knowledge(settings.business_config_path, settings.faq_path, settings.style_guide_path)
    pacer = Pacer(args.rpm)
    judge_client = anthropic.Anthropic(api_key=settings.llm_api_key.get_secret_value())
    knowledge_text = render_knowledge(kb)

    print(f"{len(suite.cases)} conversations x {args.reps} reps on {settings.llm_model}")
    started = time.monotonic()
    jobs = [(c, r) for c in suite.cases for r in range(args.reps)]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        outcomes = list(
            pool.map(
                lambda job: _guarded(
                    job[0], job[1], suite, settings, kb, pacer, judge_client, knowledge_text
                ),
                jobs,
            )
        )
    ok = summarize(outcomes, args.reps, args.traces)
    write_outputs(RESULTS_DIR / args.variant, outcomes)
    print(f"wall clock {time.monotonic() - started:.0f}s; results in evals/results/{args.variant}/")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
