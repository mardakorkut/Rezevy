"""WhatsApp webhook router for appointment booking workflow."""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from threading import Lock
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from app.api import calendar_routes as _calendar_routes


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")
MESSAGE_SID_TTL_MINUTES = 10
INTERACTIVE_REST_TIMEOUT_SEC = 3.0
TRACKING_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "gclid",
    "fbclid",
)


MESSAGE_TEMPLATES = {
    "main_menu": (
        "Merhabalar! 🌸 Salonumuza hos geldiniz. Size nasil yardimci olabilirim?"
    ),
    "appointment_created": (
        "Harika! {date} saat {time} için {service} randevunuzu oluşturdum. "
        "Randevunuz oluşturuldu. Onaylıyor musunuz? (Evet/Hayır)"
    ),
    "reminder_24h": (
        "Hatırlatma: {date} saat {time} için {service} randevunuz yarın. "
        "Uygunsanız bu mesajı Evet yazarak teyit edebilirsiniz."
    ),
    "reminder_2h": (
        "Hatırlatma: {date} saat {time} için {service} randevunuza 2 saat kaldı. "
        "Görüşmek üzere."
    ),
    "post_appointment_feedback": (
        "{date} saat {time} için {service} randevunuzdan sonra deneyiminizi "
        "1-5 arası puanlayarak paylaşır mısınız?"
    ),
}

GREETING_WORDS = {
    "selam",
    "selamlar",
    "merhaba",
    "merhabalar",
    "iyi gunler",
    "iyi akşamlar",
    "iyi aksamlar",
    "gunaydin",
    "gunaydinlar",
}

MAIN_MENU_BUTTONS = [
    {"id": "menu:book", "title": "Randevu Al"},
    {"id": "menu:cancel", "title": "Randevu İptal"},
    {"id": "menu:info", "title": "Fiyat Listesi / Bilgi Al"},
]

PROCESSED_MESSAGE_SIDS: dict[str, datetime] = {}
PROCESSED_MESSAGE_SIDS_LOCK = Lock()



def _format_message(template_key: str, **kwargs: str) -> str:
    """Render centralized reply templates with safe fallback behavior."""
    template = MESSAGE_TEMPLATES.get(template_key, "")
    if not template:
        return ""

    try:
        return template.format(**kwargs)
    except KeyError:
        return template


def _twilio_value_candidates(raw_value: str) -> list[str]:
    """Extract possible interactive selection values from raw form field value."""
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return []

    candidates = [cleaned]
    if not cleaned.startswith("{"):
        return candidates

    try:
        parsed_json = json.loads(cleaned)
    except Exception:
        return candidates

    json_candidates: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"id", "rowid", "row_id", "title", "text", "payload"} and isinstance(value, str):
                    json_candidates.append(value)
                _walk(value)
            return

        if isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(parsed_json)
    return candidates + json_candidates


def _extract_interactive_choice(form_payload: dict[str, str], user_message: str) -> str | None:
    """Extract a normalized choice token from Twilio interactive inbound fields."""
    fields = [
        "ButtonPayload",
        "ButtonText",
        "RowID",
        "RowId",
        "ListResponse",
        "InteractiveResponse",
        "Body",
    ]
    for field_name in fields:
        raw_value = form_payload.get(field_name, "")
        for candidate in _twilio_value_candidates(raw_value):
            normalized = candidate.strip()
            if normalized:
                return normalized

    user_choice = (user_message or "").strip()
    return user_choice or None


def _extract_tracking_from_form_payload(
    form_payload: dict[str, str],
    user_message: str,
) -> dict[str, str]:
    """Extract UTM/click identifiers from webhook payload and free-text message."""
    tracking: dict[str, str] = {}
    lower_payload = {str(k).lower(): str(v).strip() for k, v in form_payload.items()}

    for field_name in TRACKING_FIELDS:
        value = lower_payload.get(field_name, "")
        if not value:
            value = lower_payload.get(field_name.replace("_", ""), "")
        if value:
            tracking[field_name] = value[:120]

    for token in re.split(r"[\s&?]", user_message or ""):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        normalized_key = key.strip().lower()
        if normalized_key in TRACKING_FIELDS and value.strip():
            tracking[normalized_key] = value.strip()[:120]

    referral_url = lower_payload.get("referralsourceurl", "") or lower_payload.get("referral_source_url", "")
    if referral_url and "?" in referral_url:
        try:
            parsed_query = parse_qs(referral_url.split("?", 1)[1], keep_blank_values=False)
            for field_name in TRACKING_FIELDS:
                if field_name in tracking:
                    continue
                values = parsed_query.get(field_name, [])
                if values and values[0].strip():
                    tracking[field_name] = values[0].strip()[:120]
        except Exception:
            logger.debug("tracking_parse_failed referral_url=%s", referral_url)

    return tracking


def _send_whatsapp_interactive_buttons(
    twilio_client: Client | None,
    from_whatsapp: str | None,
    to_whatsapp: str,
    fallback_text: str,
    payload: dict[str, Any],
) -> bool:
    """Send interactive buttons via Twilio REST with content_sid first, raw interactive second."""
    if twilio_client is None or not from_whatsapp or not to_whatsapp:
        return False

    content_sid = os.getenv("TWILIO_CONTENT_SID_QUICK_BUTTONS", "").strip()
    if content_sid:
        content_variables = {"body": payload.get("body", {}).get("text", fallback_text)}
        sent = _run_with_timeout_logged(
            lambda: twilio_client.messages.create(
                from_=from_whatsapp,
                to=to_whatsapp,
                content_sid=content_sid,
                content_variables=json.dumps(content_variables),
            ),
            timeout_sec=INTERACTIVE_REST_TIMEOUT_SEC,
            context_label="interactive_buttons_content_sid",
        )
        if sent is not None:
            return True

    try:
        sent_raw = _run_with_timeout_logged(
            lambda: twilio_client.messages.create(
                from_=from_whatsapp,
                to=to_whatsapp,
                body=fallback_text,
                interactive=json.dumps(payload),
            ),
            timeout_sec=INTERACTIVE_REST_TIMEOUT_SEC,
            context_label="interactive_buttons_raw",
        )
        return sent_raw is not None
    except TwilioRestException:
        logger.exception("Twilio REST interactive buttons send failed.")
        return False
    except TypeError:
        logger.exception("Twilio SDK does not support raw interactive argument in this version.")
        return False
    except Exception:
        logger.exception("Unexpected error while sending interactive buttons.")
        return False


def _build_quick_reply_buttons_payload(prompt: str, options: list[dict[str, str]]) -> dict[str, Any]:
    """Build WhatsApp quick reply button payload."""
    buttons = [
        {
            "type": "reply",
            "reply": {
                "id": option["id"],
                "title": option["title"][:20],
            },
        }
        for option in options
    ]
    return {
        "type": "button",
        "body": {"text": prompt},
        "action": {"buttons": buttons},
    }


def _build_main_menu_text() -> str:
    """Build text fallback for main menu."""
    return "\n".join(
        [
            _format_message("main_menu"),
            "1) Randevu Al",
            "2) Randevu İptal",
            "3) Fiyat Listesi / Bilgi Al",
        ]
    )


def _build_main_menu_payload() -> dict[str, Any]:
    """Build quick-reply payload for top-level main menu."""
    return _build_quick_reply_buttons_payload(_format_message("main_menu"), MAIN_MENU_BUTTONS)


def _extract_main_menu_choice(message: str, interactive_choice: str | None) -> str | None:
    """Extract menu selection token from message or interactive choice."""
    choice = _normalize_text(interactive_choice or message)
    if not choice:
        return None

    mapping = {
        "1": "menu:book",
        "randevu al": "menu:book",
        "menu:book": "menu:book",
        "2": "menu:cancel",
        "randevu iptal": "menu:cancel",
        "randevu iptal et": "menu:cancel",
        "randevu iptal etmek": "menu:cancel",
        "randevu iptal etmek istiyorum": "menu:cancel",
        "iptal": "menu:cancel",
        "iptal et": "menu:cancel",
        "menu:cancel": "menu:cancel",
        "3": "menu:info",
        "fiyat listesi / bilgi al": "menu:info",
        "fiyat listesi bilgi al": "menu:info",
        "menu:info": "menu:info",
    }
    return mapping.get(choice)


def _is_booking_request_message(message: str) -> bool:
    """Return True for explicit booking requests without relying on LLM parsing."""
    normalized = _normalize_text(message)
    if not normalized:
        return False
    booking_hints = (
        "randevu al",
        "randevu almak",
        "randevu almak istiyorum",
        "randevu istiyorum",
        "randevu alabilir",
    )
    return any(hint in normalized for hint in booking_hints)


def _render_cancel_menu(events: list[dict[str, Any]]) -> str:
    """Render numbered cancellation menu for upcoming events."""
    lines = ["Iptal etmek istediginiz randevunun numarasini yazin:"]
    for idx, event in enumerate(events, start=1):
        summary = str(event.get("summary") or "Randevu").strip()
        when = str(event.get("start_human") or "").strip()
        lines.append(f"{idx}) {summary} - {when}")
    lines.append("Vazgecmek icin 'vazgec' yazabilirsiniz.")
    return "\n".join(lines)


def _is_unclear_non_booking(intent: str | None, parsed: dict[str, Any]) -> bool:
    """Return True when message is non-booking and lacks actionable details."""
    non_booking_intents = {None, "", "diger", "unknown", "greeting"}
    if intent not in non_booking_intents:
        return False
    return not (parsed.get("date") or parsed.get("time") or parsed.get("service"))


def _normalize_text(text: str) -> str:
    """Normalize incoming free text for lightweight matching."""
    return " ".join((text or "").strip().lower().split())


def _is_greeting_message(message: str) -> bool:
    """Return True for short greeting-only messages."""
    normalized = _normalize_text(message)
    if not normalized:
        return False

    # Keep strict matching to avoid capturing booking messages containing greetings.
    return normalized in GREETING_WORDS


def _session_to_dict(session: Any) -> dict[str, Any]:
    """Convert session object to lightweight dict for reply generation."""
    return {
        "phone": getattr(session, "phone", None),
        "current_step": getattr(session, "current_step", "idle"),
        "selected_service": getattr(session, "selected_service", None),
        "selected_service_locked": getattr(session, "selected_service_locked", False),
        "service_menu_repeat_count": getattr(session, "service_menu_repeat_count", 0),
        "pending_change_action": getattr(session, "pending_change_action", None),
        "pending_change_event_ids": getattr(session, "pending_change_event_ids", []),
        "requested_date": getattr(session, "requested_date", None),
        "requested_time": getattr(session, "requested_time", None),
        "last_intent": getattr(session, "last_intent", None),
        "alternative_slots": getattr(session, "alternative_slots", []),
        "alternative_slots_iso": getattr(session, "alternative_slots_iso", []),
        "alternative_slots_date": getattr(session, "alternative_slots_date", None),
        "awaiting_alternative_pick": getattr(session, "awaiting_alternative_pick", False),
        "confirmation_pending": getattr(session, "confirmation_pending", False),
        "confirmed": getattr(session, "confirmed", None),
        "awaiting_followup": getattr(session, "awaiting_followup", False),
        "last_reference_type": getattr(session, "last_reference_type", None),
        "last_reference_date": getattr(session, "last_reference_date", None),
        "last_reference_month": getattr(session, "last_reference_month", None),
        "last_reference_year": getattr(session, "last_reference_year", None),
        "message_count": getattr(session, "message_count", 0),
    }


def _build_twiml_response(message: str, interactive_payload: dict[str, Any] | None = None) -> Response:
    """Build XML response for Twilio WhatsApp webhook.

    TwiML does not natively carry WhatsApp list payload in this integration shape,
    so interactive payloads are logged and plain-text fallback is returned.
    """
    twiml = MessagingResponse()
    twiml.message(message)
    if interactive_payload is not None:
        logger.info("interactive_payload_fallback=%s", interactive_payload)
    return Response(content=str(twiml), media_type="application/xml")


def _build_empty_ok_response() -> Response:
    """Return empty 200 response when outbound REST message is sent successfully."""
    return Response(status_code=200, content="")


def _run_with_timeout_logged(callable_obj: Any, timeout_sec: float, context_label: str) -> Any | None:
    """Execute callable with timeout and log errors for observability."""
    if timeout_sec <= 0:
        return None

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(callable_obj)
        return future.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        try:
            future.cancel()
        except Exception:
            pass
        logger.exception("Timed out while executing %s", context_label)
        return None
    except Exception:
        logger.exception("Error while executing %s", context_label)
        return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _check_idempotency(message_sid: str) -> bool:
    """Return True if message SID is already processed; otherwise store it."""
    if not message_sid:
        return False

    now = datetime.now(ISTANBUL_TZ)
    ttl = timedelta(minutes=MESSAGE_SID_TTL_MINUTES)

    with PROCESSED_MESSAGE_SIDS_LOCK:
        expired_sids = [
            sid
            for sid, seen_at in PROCESSED_MESSAGE_SIDS.items()
            if now - seen_at > ttl
        ]
        for sid in expired_sids:
            del PROCESSED_MESSAGE_SIDS[sid]

        if message_sid in PROCESSED_MESSAGE_SIDS:
            return True

        PROCESSED_MESSAGE_SIDS[message_sid] = now
        return False


def _validate_twilio_signature(
    request: Request,
    form_payload: dict[str, str],
    auth_token: str,
) -> bool:
    """Validate Twilio request signature for webhook security."""
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("Missing X-Twilio-Signature header.")
        return False

    validator = RequestValidator(auth_token)
    return validator.validate(str(request.url), form_payload, signature)


@router.post("/whatsapp", response_class=Response)
async def whatsapp_webhook(request: Request) -> Response:
    """Handle incoming WhatsApp message and route booking logic.

    Workflow:
    - Parse Twilio form fields (`Body`, `From`)
    - Parse intent with `IntentParser`
    - If booking intent, validate date/time and check availability
    - Create calendar event when slot is available
    - Always return TwiML XML response
    """
    try:
        form_data = await request.form()
        user_message = str(form_data.get("Body", "")).strip()
        from_number = str(form_data.get("From", "")).strip()
        message_sid = str(form_data.get("MessageSid", "")).strip()
        form_payload = {key: str(value) for key, value in form_data.items()}

        app_env = str(getattr(request.app.state, "app_env", "dev")).lower()
        twilio_auth_token = str(getattr(request.app.state, "twilio_auth_token", ""))

        if app_env == "dev":
            logger.warning(
                "Twilio signature validation is skipped in dev mode. from=%s sid=%s",
                from_number,
                message_sid,
            )
        else:
            if not twilio_auth_token:
                logger.error("TWILIO_AUTH_TOKEN is missing in non-dev environment.")
                raise HTTPException(status_code=403, detail="Webhook signature validation failed")

            is_valid = _validate_twilio_signature(
                request=request,
                form_payload=form_payload,
                auth_token=twilio_auth_token,
            )
            if not is_valid:
                logger.warning(
                    "Invalid Twilio signature. from=%s sid=%s env=%s",
                    from_number,
                    message_sid,
                    app_env,
                )
                raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        logger.info(
            "Incoming WhatsApp message from=%s sid=%s body=%s",
            from_number,
            message_sid,
            user_message,
        )

        current_datetime = datetime.now(ISTANBUL_TZ).strftime("%Y-%m-%d %H:%M %Z")

        intent_parser = getattr(request.app.state, "intent_parser", None)
        session_manager = getattr(request.app.state, "session_manager", None)
        calendar_manager = getattr(request.app.state, "calendar_manager", None)
        twilio_rest_client = getattr(request.app.state, "twilio_rest_client", None)
        twilio_whatsapp_from = getattr(request.app.state, "twilio_whatsapp_from", None)

        if intent_parser is None or session_manager is None:
            logger.error("App dependencies are missing in app.state.")
            return _build_twiml_response(
                "Sistem şu an geçici olarak hazır değil. Lütfen birazdan tekrar deneyin. 🙏"
            )

        idempotent_hit = _check_idempotency(message_sid)
        logger.info(
            "idempotent_hit=%s message_sid=%s from=%s",
            idempotent_hit,
            message_sid,
            from_number,
        )
        if idempotent_hit:
            return _build_twiml_response("Mesajınız alındı, işlem tekrar edilmedi ✅")

        session = session_manager.get_or_create(from_number)
        interactive_choice = _extract_interactive_choice(form_payload, user_message)
        tracking_context = _extract_tracking_from_form_payload(form_payload, user_message)

        if (
            getattr(session, "pending_change_action", None) == "cancel"
            and getattr(session, "pending_change_event_ids", None)
        ):
            choice = _normalize_text(interactive_choice or user_message)
            if choice in {"vazgec", "vazgeç", "iptal etme", "hayir", "hayır"}:
                session_manager.update(
                    from_number,
                    {
                        "pending_change_action": None,
                        "pending_change_event_ids": [],
                        "current_step": "idle",
                    },
                )
                return _build_twiml_response("Tamamdir, iptal islemi durduruldu.")

            if choice.isdigit():
                index = int(choice) - 1
                event_ids = list(getattr(session, "pending_change_event_ids", []))
                if 0 <= index < len(event_ids) and calendar_manager is not None:
                    event_id = event_ids[index]
                    cancelled = calendar_manager.cancel_event_if_owned(event_id, from_number)
                    session_manager.update(
                        from_number,
                        {
                            "pending_change_action": None,
                            "pending_change_event_ids": [],
                            "current_step": "idle",
                        },
                    )
                    if cancelled:
                        return _build_twiml_response("Randevunuz iptal edildi ✅")
                    return _build_twiml_response(
                        "Randevu iptali yapilamadi. Lutfen tekrar deneyin."
                    )

            return _build_twiml_response(
                "Lutfen listeden numarayi yazin veya vazgec icin 'vazgec' yazin."
            )

        if getattr(session, "current_step", "idle") == "awaiting_booking_link":
            return _build_twiml_response(
                "Rezervasyon linkiniz hâlâ aktif. "
                "Lütfen yukarıdaki linkten işleminizi tamamlayın.\n"
                "Yeni link almak için hizmet adını tekrar yazabilirsiniz."
            )

        parsed = intent_parser.parse_message(user_message, current_datetime)
        menu_choice = _extract_main_menu_choice(user_message, interactive_choice)
        if menu_choice == "menu:book":
            parsed = {"intent": "randevu_al", "date": None, "time": None, "service": None}
        elif menu_choice == "menu:cancel":
            if calendar_manager is None:
                return _build_twiml_response(
                    "Randevu iptal servisi su anda kullanilamiyor."
                )
            upcoming = calendar_manager.list_upcoming_events_by_phone(from_number, max_results=3)
            if not upcoming:
                return _build_twiml_response(
                    "Gorunen aktif bir randevunuz bulunamadi."
                )
            session_manager.update(
                from_number,
                {
                    "pending_change_action": "cancel",
                    "pending_change_event_ids": [event["id"] for event in upcoming],
                    "current_step": "awaiting_cancel_selection",
                },
            )
            return _build_twiml_response(_render_cancel_menu(upcoming))
        elif menu_choice == "menu:info":
            return _build_twiml_response(
                "Bilgi/Fiyat icin kisa not: detayli fiyat listemizi paylasmamizi isterseniz "
                "islem adini yazin, hemen yardimci olalim."
            )

        if parsed.get("intent") != "randevu_al" and _is_booking_request_message(user_message):
            parsed = {"intent": "randevu_al", "date": None, "time": None, "service": None}

        if getattr(session, "current_step", "idle") == "idle" and (
            _is_greeting_message(user_message) or _is_unclear_non_booking(parsed.get("intent"), parsed)
        ):
            main_menu_payload = _build_main_menu_payload()
            main_menu_text = _build_main_menu_text()
            sent_menu = _send_whatsapp_interactive_buttons(
                twilio_client=twilio_rest_client,
                from_whatsapp=twilio_whatsapp_from,
                to_whatsapp=from_number,
                fallback_text=main_menu_text,
                payload=main_menu_payload,
            )
            if sent_menu:
                return _build_empty_ok_response()
            return _build_twiml_response(main_menu_text, interactive_payload=main_menu_payload)

        intent = parsed.get("intent")
        if intent == "randevu_al":
            service_hint = parsed.get("service")
            try:
                booking_link = _calendar_routes.make_booking_link(
                    request,
                    from_number,
                    tracking=tracking_context,
                    service=service_hint,
                )
                _calendar_routes.log_funnel_event(
                    "lead_created",
                    from_number,
                    {
                        "channel": "whatsapp",
                        "service_hint": service_hint,
                        **tracking_context,
                    },
                )
                session_manager.update(
                    from_number,
                    {
                        "current_step": "awaiting_booking_link",
                        "selected_service": None,
                        "selected_service_locked": False,
                        "service_menu_repeat_count": 0,
                        "booking_link_sent": True,
                        "requested_date": None,
                        "requested_time": None,
                        "alternative_slots": [],
                        "alternative_slots_iso": [],
                        "alternative_slots_date": None,
                        "awaiting_followup": True,
                    },
                )
                return _build_twiml_response(
                    "Harika! Randevu olusturmak icin hazirsiniz 🌸\n\n"
                    "Asagidaki linkten once isleminizi, sonra gun ve saatinizi secin:\n"
                    f"{booking_link}\n\n"
                    "Link 30 dakika gecerlidir."
                )
            except RuntimeError:
                logger.warning("BOOKING_LINK_SECRET missing; cannot generate booking link.")
                return _build_twiml_response(
                    "Rezervasyon servisi su an hazir degil. Lutfen daha sonra tekrar deneyin."
                )

        logger.info(
            "Non-booking intent from=%s sid=%s intent=%s result=fallback_info",
            from_number,
            message_sid,
            intent,
        )
        response_text = intent_parser.generate_contextual_reply(
            user_message=user_message,
            parsed_intent=parsed,
            session_state=_session_to_dict(session),
            alternative_slots=None,
        )
        return _build_twiml_response(response_text)

    except HTTPException:
        raise

    except Exception as exc:  # noqa: BLE001
        logger.exception("Webhook processing failed with unexpected error: %s", exc)
        return _build_twiml_response(
            "Şu anda teknik bir sorun yaşıyoruz, lütfen birazdan tekrar dener misiniz? 🙏"
        )
