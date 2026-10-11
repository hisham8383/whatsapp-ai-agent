"""Process entrypoints: one image, three roles (CLAUDE.md §3).

python -m app api        # FastAPI via uvicorn (PORT env, default 8080)
python -m app worker     # job loop: inbound replies (any number of replicas)
python -m app scheduler  # reminders, follow-ups, staff alerts, sync, retention (ONE replica)
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    """Start the role named in ``argv[1]``. Returns a process exit code."""
    role = argv[1] if len(argv) > 1 else ""
    if role == "api":
        import uvicorn

        uvicorn.run(
            "app.main:create_app",
            factory=True,
            host="0.0.0.0",  # noqa: S104  # container must listen on all interfaces
            port=int(os.environ.get("PORT", "8080")),
            proxy_headers=True,
            # The access log would print ?hub.verify_token=... (a secret, CLAUDE.md §2).
            # Our own JSON logs cover requests instead.
            access_log=False,
        )
        return 0

    if role == "worker":
        from app.alerts.email import EmailSender
        from app.alerts.service import AlertJob, AlertSender
        from app.booking.base import build_booking_provider
        from app.config import get_settings
        from app.conversation.agent import AgentReplier
        from app.conversation.handler import InboundHandler
        from app.kb.loader import load_knowledge
        from app.llm.base import build_llm_client
        from app.logging import configure_logging
        from app.whatsapp.client import WhatsAppClient
        from app.whatsapp.templates import load_catalog
        from app.worker.queue import JOB_HANDLE_INBOUND, JOB_SEND_ALERTS
        from app.worker.runner import Worker

        configure_logging()
        if os.environ.get("PORT"):
            # Cloud Run services must answer on $PORT; see app/worker/health.py.
            from app.worker.health import start_health_server

            start_health_server(int(os.environ["PORT"]))
        settings = get_settings()
        # Everything below fails fast on bad config: knowledge files, LLM and booking adapters.
        kb = load_knowledge(
            settings.business_config_path, settings.faq_path, settings.style_guide_path
        )
        load_catalog(settings)  # a template name typo stops every process at boot (§8)
        replier = AgentReplier(
            settings, kb, build_llm_client(settings), build_booking_provider(settings, kb)
        )
        client = WhatsAppClient(settings)
        handler = InboundHandler(settings, client, replier, kb=kb)
        email = EmailSender(settings) if settings.alert_email_list else None
        alerts = AlertJob(AlertSender(settings, client, email))
        Worker({JOB_HANDLE_INBOUND: handler.handle, JOB_SEND_ALERTS: alerts.handle}).run_forever()
        return 0

    if role == "scheduler":
        from app.alerts.email import EmailSender
        from app.alerts.service import AlertSender
        from app.booking.base import build_booking_provider
        from app.config import get_settings
        from app.kb.loader import load_knowledge
        from app.logging import configure_logging
        from app.scheduler.booking_sync import BookingSync
        from app.scheduler.followups import FollowUpSender
        from app.scheduler.reminders import ReminderSender, warn_offsets_inside_notice
        from app.scheduler.runner import Scheduler
        from app.whatsapp.client import WhatsAppClient
        from app.whatsapp.templates import load_catalog

        configure_logging()
        if os.environ.get("PORT"):
            from app.worker.health import start_health_server

            start_health_server(int(os.environ["PORT"]))
        settings = get_settings()
        kb = load_knowledge(
            settings.business_config_path, settings.faq_path, settings.style_guide_path
        )
        catalog = load_catalog(settings)
        warn_offsets_inside_notice(settings, kb)
        client = WhatsAppClient(settings)
        provider = build_booking_provider(settings, kb)
        email = EmailSender(settings) if settings.alert_email_list else None
        Scheduler(
            settings,
            alerts=AlertSender(settings, client, email),
            follow_ups=FollowUpSender(settings, kb, client, catalog),
            reminders=ReminderSender(settings, kb, client, catalog),
            sync=BookingSync(settings, kb, provider, client, catalog),
        ).run_forever()
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
