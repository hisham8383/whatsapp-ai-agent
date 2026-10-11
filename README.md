# WhatsApp AI Front-Desk Agent (Arabic / English)

A production-grade AI agent that runs a business's WhatsApp Business number like a
front desk. It answers questions, books, reschedules and cancels appointments,
sends reminders, follows up with leads, and hands conversations to staff when a
human is needed. It speaks English and Arabic, and its Arabic is tuned to the
Gulf/Saudi dialect.

I built it as the top tier of a WhatsApp-agent service I deliver to small
businesses (clinics, salons, law firms). This repo is the portfolio edition. The
code is the same, and the business data is a fictional dental clinic
([examples/demo-clinic/](examples/demo-clinic/)).

| | |
|---|---|
| **Agent model** | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) with tool use and prompt caching |
| **Eval gate** | 46 scripted conversations (23 Arabic / 23 English), run 3× each: **138/138 passed** ([results](evals/results/baseline/)) |
| **Cost** | about **$0.003 per conversation** on the eval set (≈2 LLM calls each, ~5k-token cached system prompt) |
| **Latency** | median **2.8 s** per conversation turn in evals |
| **Tests** | 292 unit/integration tests; `ruff` + `mypy --strict` clean |
| **Live-tested** | end to end on a real WhatsApp Business test number (Meta Cloud API), with approved templates |

---

## What it does

- **Answers FAQs** from the business's knowledge base in the customer's
  language. It mirrors formal Arabic (MSA) when the customer uses it, and
  otherwise replies in natural Gulf phrasing ("حياك الله", "ابشر").
- **Books, reschedules and cancels** through a pluggable booking provider
  (an internal Postgres calendar, or Google Calendar). Time slots appear as a
  WhatsApp interactive list, and only slots that are really free are offered.
- **Confirms before acting.** Every change gets a code-written summary with
  **Confirm / Change** buttons. Nothing changes until the customer taps Confirm.
- **Sends appointment reminders** as Meta-approved templates with
  **Confirm / Reschedule** quick replies. "Reschedule" drops the customer straight
  back into the conversation with the agent.
- **Follows up once** with leads who asked about a service but didn't book. It
  respects quiet hours, a cooldown period and opt-outs (STOP / إيقاف / الغاء …).
- **Hands off to humans** when a customer is upset, asks for a person or a
  discount, or asks something out of scope. Staff get an alert by WhatsApp
  template (and optionally email). A web **staff inbox** lets them reply, re-open
  closed conversations with a template, and return the chat to the bot.
- **Keeps in sync** with the calendar. If staff move or delete a booking in
  Google Calendar, reminders are re-planned and the customer is told.

## Architecture

```
Customer (WhatsApp)
      │
Meta WhatsApp Cloud API ──POST /webhook──► API (FastAPI)
      ▲                                     1. verify X-Hub-Signature-256 (HMAC of raw body)
      │                                     2. dedupe on message id
      │                                     3. store + enqueue job → 200 in milliseconds
      │                                     │
      │                         Worker ◄────┘  claim job: SELECT … FOR UPDATE SKIP LOCKED
      │                           │  opt-out / quick replies / HUMAN state handled in code
      │                           ▼
      │                         Agent loop: Claude ⇄ tools
      │                           get_availability · create/reschedule/cancel_booking
      │                           list_customer_bookings · handoff_to_human · mark_lead_interest
      │                           │
      ├──── reply (24h-window guard) ◄─┘
      │
      └──── templates ◄── Scheduler (one replica, every minute)
                          staff alerts → lead follow-ups → reminders → calendar sync → retention

PostgreSQL: all state (messages, bookings, jobs, reminders, alerts) · Staff inbox at /inbox
```

The system runs as one Docker image with three roles (`api`, `worker`,
`scheduler`). It needs no Redis or message broker, because Postgres is both the
database and the job queue.

## Engineering highlights

**Guardrails in code, not only in the prompt.** Early evals showed the model
sometimes said "you're booked" before any booking existed. The fix wasn't a
longer prompt:
- A booking tool is **rejected** unless the previous assistant turn asked the
  customer to confirm.
- Confirmation and "done" messages are **written by code** from the actual
  database result, never by the LLM.
- A guard catches the model asking for confirmation in free text and replaces it
  with the button flow.

**Exactly-once messaging.** Reminders and follow-ups must never be sent twice,
even with several workers or after a crash. Each scheduler row is claimed with
`FOR UPDATE SKIP LOCKED` under a customer-row lock and a savepoint, then marked
sent along with the WhatsApp message id in the same transaction. A crash in
mid-send marks the row as failed rather than retrying blindly.
[`scripts/smoke_postgres.py`](scripts/smoke_postgres.py) runs **two concurrent
schedulers** against real Postgres to prove it. That test caught real
double-sends, which are now fixed.

**WhatsApp platform rules enforced by the send layer.** HMAC signature checks
and idempotent webhook handling. A 24-hour customer-service window guard, so
free-form text can't go out after the window closes and only approved templates
can. Template names are validated at startup. Late-redelivered webhooks are
detected so they don't trigger stale replies.

**Evaluation-driven development.** [`evals/`](evals/) replays scripted
conversations through the **real production path** (webhook ingest → queue →
worker → agent → booking tools). Only the database (in-memory) and the Meta API
(a recorder) are swapped out.
- Grading is **programmatic first**: the resulting bookings, conversation
  state, facts in the reply and reply language.
- An **LLM judge on a different, stronger model** handles only judgment calls,
  such as prompt-injection resistance and the **Gulf-dialect rubric**.
- Every case runs 3 times, and delivery is gated on 3/3.
- The cases cover FAQs, booking, rescheduling from a reminder, cancellation,
  fully booked days, handoff triggers, opt-outs, out-of-scope questions and
  prompt-injection attempts, in both languages.

**Cost control.** The whole knowledge base and an Arabic style guide sit in a
**cached system prompt**, history is capped, and the cheapest model that passes
the eval gate is used. Retrieval (pgvector) is designed in but switched off
until a knowledge base outgrows the prompt.

**Privacy by design (Saudi PDPL).** The system stores only what the workflow
needs, redacts phone numbers in logs, runs a daily retention job, never asks for
sensitive data, and keeps all secrets in environment variables or a secret store.

## Tech stack

| Area | Used |
|---|---|
| Language | Python 3.12, fully typed (`mypy --strict`) |
| LLM | Anthropic Claude API: tool use, prompt caching, structured-output LLM judge; behind a provider-neutral interface ([`app/llm/`](app/llm/)) |
| Web / API | FastAPI, Uvicorn, httpx |
| Data | PostgreSQL, SQLAlchemy 2, Alembic migrations; Postgres-backed job queue (`SKIP LOCKED`) |
| Messaging | Meta WhatsApp Cloud API: webhooks, interactive lists/buttons, message templates |
| Integrations | Google Calendar API (service account), SMTP email alerts |
| Staff UI | Server-rendered Jinja2 inbox with password auth |
| Config | pydantic-settings; refuses to start on missing or invalid config or knowledge files |
| Quality | pytest (292 tests), ruff, mypy, an eval harness with an LLM judge, live smoke tests (Postgres, Google Calendar) |
| Deployment | Docker; Google Cloud Run + Cloud SQL + Secret Manager ([deploy/gcp](deploy/gcp/README.md)); Kubernetes manifests ([deploy/k8s](deploy/k8s/)) |

## How it was built

I designed and engineered this system end to end: the architecture, the
conversation and tool design, the safety guardrails, the data model and the
evaluation strategy.

1. **Spec first.** I wrote [CLAUDE.md](CLAUDE.md) as the full design spec. It
   covers scope, WhatsApp policy rules, the conversation state machine, tool
   contracts, the data model, config, testing requirements and a milestone
   plan. Code comments cite its sections (e.g. `CLAUDE.md §4`), so each rule
   traces back to a design decision.
2. **Vertical slice, then layers.** I started with webhook → echo, then added
   the LLM and knowledge base, booking tools, handoff and inbox, the scheduler,
   and finally dialect tuning. Each milestone had to pass lint, tests and evals
   before the next one started.
3. **Evals as the acceptance test.** Eval failures drove prompt and code
   changes. Several of the guardrails above exist because an eval caught the
   model misbehaving.
4. **Live testing on WhatsApp.** I ran two rounds on a real test number with
   staff phones and approved templates. They led to fixes for late
   redeliveries, the reminder→reschedule flow and Gulf Arabic phrasing.

I used Claude Code as a coding assistant to speed up implementation and cut
development time.

## Repository tour

| Path | What's there |
|---|---|
| [`app/webhook/`](app/webhook/) | signature verification, parsing, dedupe, enqueue |
| [`app/conversation/`](app/conversation/) | per-message handler, agent tool loop, tools + confirmation guard, prompt builder (dialect + style guide) |
| [`app/llm/`](app/llm/) | provider-neutral LLM interface + Anthropic adapter with prompt caching |
| [`app/booking/`](app/booking/) | `BookingProvider` interface, internal and Google Calendar adapters, external-change sync |
| [`app/scheduler/`](app/scheduler/) | exactly-once sending loop, reminders, follow-ups, quiet hours, calendar sync, retention |
| [`app/whatsapp/`](app/whatsapp/) | Cloud API client, 24h window guard, template catalog, opt-out detection |
| [`app/inbox/`](app/inbox/), [`app/alerts/`](app/alerts/) | staff inbox and handoff alerts |
| [`app/worker/`](app/worker/), [`app/db/`](app/db/) | job queue + loop; models and session |
| [`evals/`](evals/) | eval runner, graders, LLM judge, [baseline results + traces](evals/results/baseline/) |
| [`examples/demo-clinic/`](examples/demo-clinic/) | fictional clinic: business config, bilingual FAQs, Arabic style guide, 46-case eval set |
| [`templates/whatsapp/`](templates/whatsapp/) | the five message templates (ar + en) submitted to Meta |
| [`docs/`](docs/) | handover guide and ops runbook |

## Running it locally

Requires Python 3.12. Postgres is needed to run the app; the tests use in-memory SQLite.

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"      # Windows: .venv\Scripts\python
cp .env.example .env                              # fill in your own keys; never commit .env
make test lint                                    # Windows: ./tasks.ps1 test / lint
```

To run against the demo clinic, point `BUSINESS_CONFIG_PATH`, `FAQ_PATH` and
`STYLE_GUIDE_PATH` in `.env` at `examples/demo-clinic/` and set
`BOOKING_PROVIDER=internal`.

| Task | Linux/macOS | Windows |
|---|---|---|
| API (webhook + `/inbox`, port 8080) | `make dev` | `./tasks.ps1 dev` |
| Worker | `make worker` | `./tasks.ps1 worker` |
| Scheduler (exactly one) | `make scheduler` | `./tasks.ps1 scheduler` |
| DB migrations | `make migrate` | `./tasks.ps1 migrate` |
| Eval gate on the demo clinic (paid API, ≈$1) | `make eval-demo` | `./tasks.ps1 eval-demo` |
| Docker image | `make build` | `./tasks.ps1 build` |

A live run also needs a Meta developer app with a WhatsApp test number and a
public HTTPS URL for the webhook (for example a Cloudflare tunnel). See
[docs/RUNBOOK.md](docs/RUNBOOK.md).

## Sample conversation (from the eval traces)

```
Customer:  السلام عليكم
Assistant: وعليكم السلام، حياك الله! 🌟
           كيف أقدر أخدمك اليوم؟ تبي تحجز موعد أم عندك سؤال عن العيادة؟
```

More, in both languages, in [evals/results/baseline/traces/](evals/results/baseline/traces/).

---

Built by **Hisham Alhussain**. Released under the [MIT License](LICENSE).
