"""Application entrypoint for WhatsApp booking bot."""

from __future__ import annotations

import logging
import os
from dotenv import load_dotenv
from fastapi import FastAPI
from twilio.rest import Client

from app.api.webhook import router as webhook_router
from app.api.calendar_routes import _get_twilio_whatsapp_sender, router as calendar_router
from app.services.calendar_service import GoogleCalendarManager
from app.services.llm_service import IntentParser
from app.services.session_service import SessionManager

load_dotenv()


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def _init_intent_parser() -> IntentParser:
    """Initialize and return IntentParser instance."""
    return IntentParser()


def _init_calendar_manager() -> GoogleCalendarManager:
    """Initialize and return GoogleCalendarManager instance."""
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
    if not calendar_id:
        raise ValueError("GOOGLE_CALENDAR_ID environment variable is required.")

    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    return GoogleCalendarManager(
        calendar_id=calendar_id,
        credentials_path=credentials_path,
        timezone="Europe/Istanbul",
    )


def _init_session_manager() -> SessionManager:
    """Initialize and return SessionManager instance."""
    ttl_minutes = int(os.getenv("SESSION_TTL_MINUTES", "30"))
    return SessionManager(ttl_minutes=ttl_minutes, timezone="Europe/Istanbul")


def _init_twilio_rest_client() -> tuple[Client | None, str | None]:
    """Initialize Twilio REST client and WhatsApp sender from env vars."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    whatsapp_from = _get_twilio_whatsapp_sender()

    if not (account_sid and auth_token and whatsapp_from):
        logger.warning(
            "Twilio warm-init skipped: missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_WHATSAPP_FROM or TWILIO_WHATSAPP_NUMBER."
        )
        return None, None

    try:
        return Client(account_sid, auth_token), whatsapp_from
    except Exception:
        logger.exception("Twilio warm-init failed during startup.")
        return None, None


app = FastAPI(title="Beauty Center WhatsApp Bot", version="0.1.0")


@app.on_event("startup")
def on_startup() -> None:
    """Initialize application dependencies and store them safely in app.state."""
    logger.info("Application startup: initializing dependencies.")
    app.state.app_env = os.getenv("APP_ENV", "dev").strip().lower()
    app.state.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    app.state.intent_parser = _init_intent_parser()
    app.state.calendar_manager = _init_calendar_manager()
    app.state.session_manager = _init_session_manager()
    twilio_client, twilio_whatsapp_from = _init_twilio_rest_client()
    app.state.twilio_rest_client = twilio_client
    app.state.twilio_whatsapp_from = twilio_whatsapp_from
    logger.info("Dependencies initialized successfully for env=%s.", app.state.app_env)


@app.get("/health")
def health() -> dict[str, str]:
    """Simple health endpoint."""
    return {"status": "ok"}


app.include_router(webhook_router)
app.include_router(calendar_router)
