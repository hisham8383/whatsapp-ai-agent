# Runbook

## Processes

One image (`deploy/docker/Dockerfile`), three roles:

| Role | Command | Scale |
|---|---|---|
| API | `python -m app api` (listens on `$PORT`, default 8080): Meta webhook + staff inbox | 1+ replicas (stateless) |
| Worker | `python -m app worker`: replies, bookings, opt-outs, reminder quick replies, staff alerts right after a handoff | 1+ replicas. Jobs are claimed with `SKIP LOCKED`; bookings take a Postgres advisory lock per staff member |
| Scheduler | `python -m app scheduler`: every minute sends due reminders, lead follow-ups, and any staff alert the worker couldn't send; syncs the booking calendar every `BOOKING_SYNC_MINUTES`; daily retention | **Exactly 1 replica** (k8s `deployment-scheduler.yaml`: `replicas: 1`, `Recreate`). Each send is claimed `FOR UPDATE SKIP LOCKED` and marked sent in the same transaction, so even an accidental second copy never double-sends (`python -m scripts.smoke_postgres` proves it) |

Google Cloud step by step (Cloud Run + Cloud SQL + Secret Manager): [deploy/gcp/README.md](../deploy/gcp/README.md).

Before starting a new version, run migrations: `alembic upgrade head`
(`deploy/k8s/cronjob-migrate.yaml` on Kubernetes).

Every process refuses to start if a required setting is missing (including
the five `TEMPLATE_*` names), if a knowledge file or template definition is
invalid, or if `BOOKING_PROVIDER` / `LLM_PROVIDER` names an adapter that isn't
built. The error names the problem. See `.env.example` for
every key and CLAUDE.md §8 for which are secrets.

## Local development

```sh
# one-time: a role and database (use your Postgres superuser)
psql -U postgres -h localhost -c "CREATE ROLE agent LOGIN PASSWORD 'agent'" \
                              -c "CREATE DATABASE wa_agent OWNER agent"
# .env: DATABASE_URL=postgresql+psycopg://agent:agent@localhost:5432/wa_agent
make migrate
make dev        # terminal 1: API on :8080 (inbox at http://localhost:8080/inbox)
make worker     # terminal 2
make scheduler  # terminal 3 (sends templates! use the Meta test number only)
```

To reach the webhook from Meta, expose port 8080 with a tunnel (e.g.
`cloudflared tunnel --url http://localhost:8080`) and use that URL below.

## Message templates (day 1)

Submit the five templates in `templates/whatsapp/` on day 1 (steps in
`templates/whatsapp/README.md`). The `TEMPLATE_*` settings must match the
approved names. Status webhooks mark a failed template send `failed` with
Meta's error code (shown in the inbox); definite failures are retried once,
timeouts never (they may have been delivered).

Changing a template's wording needs Meta's re-approval: submit it under a new
name, switch the `TEMPLATE_*` key once approved, and update the JSON file.

Common template errors: **132000** parameter count doesn't match (the app
checks this at startup); **132001** name/language not approved yet;
**131026** recipient can't receive (not on WhatsApp); **131049** Meta
throttled a MARKETING follow-up (expected occasionally).

## Meta webhook setup

1. Callback URL: `https://<api-host>/webhook`
2. Verify token: the value of `WEBHOOK_VERIFY_TOKEN`
3. Subscribe to the `messages` field, and subscribe the app to the WABA.

Every POST must carry a valid `X-Hub-Signature-256` made with
`WHATSAPP_APP_SECRET`. Wrong or missing signatures get 401 and are logged as
`"event": "bad_signature"`.

## Booking providers

| `BOOKING_PROVIDER` | State |
|---|---|
| `internal` | Built. Bookings live in the `bookings` table; staff see them in the inbox |
| `google_calendar` | Built. One calendar (`CALENDAR_ID`); events the assistant creates are tagged with their staff member, events added by hand block everyone, "free" events are ignored |
| `calendly`, `generic_api` | Not built yet. Implement `BookingProvider` (`app/booking/base.py`) for the client's system, register it in `build_booking_provider`, and test each method with mocked HTTP. For Calendly, check the plan and API first (CLAUDE.md §6) |

### Google Calendar setup (per client)

1. In the client's Google Cloud project: enable the **Google Calendar API**;
   IAM & Admin → Service Accounts → Create (no roles needed) → Keys → Add key →
   JSON. The key goes straight into the secret store as `CALENDAR_CREDENTIALS`.
2. The client shares the booking calendar with the service account's email:
   Google Calendar → the calendar's **Settings and sharing** → **Share with
   specific people** → "Make changes to events".
3. `CALENDAR_ID`: the same settings page → **Integrate calendar** → Calendar ID.
4. `BOOKING_PROVIDER=google_calendar`. The app refuses to start if the key or
   the calendar id is missing or malformed.

Locally, `CALENDAR_CREDENTIALS` may be the path to the downloaded key file
(keep it outside the repo).

**Google Workspace clients: two admin settings block this by default.** The
client's Workspace admin changes them (never do it for them):

| Symptom | Setting (client's admin) |
|---|---|
| "Service account key creation is disabled" when creating the key | Cloud console → IAM & Admin → Organization Policies → **Disable service account key creation** → Manage policy → Override parent's policy → add a rule, Enforcement **Off**. Scope it to the booking project only |
| "Make changes to events" is greyed out when sharing ("Some sharing options may have been turned off by your administrator") | admin.google.com → Apps → Google Workspace → Calendar → **Sharing settings** → External sharing options for secondary calendars (and/or primary) → **Share all information, and outsiders can change calendars**. The service account counts as outside the organization |

If a client's admin won't allow outside editing, the alternative is
domain-wide delegation (the service account acts as one staff user); the
adapter doesn't support that yet.

Verified live on 2026-09-28 against a Workspace test calendar
(`python -m scripts.smoke_gcal`, 7/7).

## Live smoke tests

Unit tests use SQLite and mocked HTTP. Before a client goes live, run these
against the real services:

| Command | Checks |
|---|---|
| `python -m scripts.smoke_postgres` | Postgres: 6 concurrent bookings of one slot store exactly one (advisory lock); 30 jobs / 5 claimers each claimed once (`SKIP LOCKED`) |
| `python -m scripts.smoke_postgres --api http://localhost:8080` | Also, with `make dev` running (and the worker **stopped**): signed webhook stored, bad signature rejected, one worker step answered by the real LLM (Meta send is recorded, not sent), inbox login |
| `python -m scripts.smoke_gcal` | Google Calendar (a **test** calendar): availability, create, list, reschedule, cancel against the real API; the test event is always deleted |

Both use the fake number 999000000001 and remove everything they create.

Booking rules (hours, lead time, horizon, buffer, slot interval, cancellation
notice) come from `business.yaml` and are applied in `app/booking/base.py` for
every adapter.

## Eval gate (before delivery and after knowledge changes)

1. Write `evals/cases.yaml` from the client's knowledge files (copy
   `examples/demo-clinic/eval-cases.yaml` and adapt; ≥30 conversations, half
   Arabic, half English).
2. `make eval` (3 reps each; calls the paid API, about $1–1.50 per run).
3. It must print `RESULT: PASS`. Failing conversations are printed with
   `--traces`; full results are in `evals/results/<variant>/`.

Reference: the demo clinic passes 32/32 × 3 on `claude-haiku-4-5-20251001`,
≈$0.0075 of LLM cost per customer message. Move `LLM_MODEL` up only if a
client's evals fail on Haiku (CLAUDE.md §3).

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| Handshake returns 403 | `WEBHOOK_VERIFY_TOKEN` differs from the Meta dashboard | Make them match |
| Every POST returns 401 | `WHATSAPP_APP_SECRET` is wrong or was rotated | Update the secret, restart the API |
| App won't start: `KnowledgeError` | A knowledge file is invalid (message says what) | Fix the file, redeploy |
| App won't start: `has no adapter yet` | `BOOKING_PROVIDER` names an adapter that isn't built | Build it, or use `internal` |
| Log: `reply skipped: 24h window closed` | The customer's last message is over 24h old | Expected. The customer must write again |
| Log: `false_done_claim` | The model said "booked" without booking; the customer was asked to confirm again instead | Nothing to do; frequent occurrences mean the prompt needs attention (check with the eval set) |
| Log: `llm_failed` | AI provider down or timing out | Customers get "please try again"; two failures in a row hand the chat to staff |
| Log: `booking provider failed` | Booking system unreachable | The assistant apologizes and offers the team; check the provider |
| Log: `outbound message failed` with `error_code` 131047 | Sent outside the window (shouldn't happen, since the guard runs first) | Check server clock / timezone |
| Jobs piling up in `jobs` with `status='queued'` | Worker not running, or Meta unreachable | Check worker logs. Failed jobs retry with backoff, then `status='failed'` with `last_error` |
| Staff can't log in: "Too many attempts" | 5 wrong passwords in 15 min from one address | Wait 15 minutes |

## Rotating secrets

All secrets live in the client's secret store. After changing one, restart
the affected processes.

| Secret | Where to rotate | Restart |
|---|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Meta Business Settings → System Users → generate a new token (never expires) | API + worker |
| `WHATSAPP_APP_SECRET` | Meta app → App settings → Basic → Reset app secret | API |
| `WEBHOOK_VERIFY_TOKEN` | Generate a new random value; update it in the Meta webhook settings too | API |
| `LLM_API_KEY` | Anthropic console → API keys (create new, then delete old) | worker |
| `INBOX_AUTH_SECRET` | Generate a new random value (logs every staff member out); tell the client where to read it | API |
| `CALENDAR_CREDENTIALS` | The booking system's console (service-account key or API token) | worker |
| `DATABASE_URL` | Cloud SQL / RDS user password | API + worker |
