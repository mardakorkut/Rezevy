"""Session management service for conversational WhatsApp booking flows.

This module provides an in-memory session store with TTL cleanup for
tracking per-user conversation state by phone number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Literal
from zoneinfo import ZoneInfo


SessionStep = Literal["idle", "awaiting_service", "awaiting_datetime", "awaiting_booking_link", "confirming"]
ReferenceType = Literal["time", "date"]


@dataclass(slots=True)
class SessionState:
    """Represents current conversational state for one customer session.

    Attributes:
        phone: Customer phone identifier (e.g., WhatsApp sender number).
        current_step: Current state machine step in booking flow.
        selected_service: Service selected by user.
        requested_date: Requested booking date in YYYY-MM-DD.
        requested_time: Requested booking time in HH:MM.
        last_intent: Last parsed intent.
        alternative_slots: Suggested alternative slot strings.
        handoff_requested: Whether user requested human support handoff.
        message_count: Number of messages seen in this session.
        created_at: Session creation timestamp.
        last_seen_at: Last activity timestamp.
    """

    phone: str
    current_step: SessionStep = "idle"
    selected_service: str | None = None
    selected_service_locked: bool = False
    service_menu_repeat_count: int = 0
    pending_change_event_ids: list[str] = field(default_factory=list)
    pending_change_action: str | None = None
    requested_date: str | None = None
    requested_time: str | None = None
    last_intent: str | None = None
    alternative_slots: list[str] = field(default_factory=list)
    alternative_slots_iso: list[str] = field(default_factory=list)
    alternative_slots_date: str | None = None
    awaiting_alternative_pick: bool = False
    booking_link_sent: bool = False
    handoff_requested: bool = False
    confirmation_pending: bool = False
    confirmed: bool | None = None
    reminder_schedule: list[str] = field(default_factory=list)
    awaiting_followup: bool = False
    last_reference_type: ReferenceType | None = None
    last_reference_date: str | None = None
    last_reference_month: int | None = None
    last_reference_year: int | None = None
    message_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo("Europe/Istanbul")))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo("Europe/Istanbul")))


class SessionManager:
    """Thread-safe in-memory session manager with TTL-based expiration.

    Notes:
        - Designed for MVP/single-instance deployments.
        - For horizontal scaling, migrate this abstraction to Redis.
    """

    def __init__(self, ttl_minutes: int = 30, timezone: str = "Europe/Istanbul") -> None:
        """Initialize session manager.

        Args:
            ttl_minutes: Session expiration duration after last activity.
            timezone: Timezone used for session timestamps.
        """
        if ttl_minutes <= 0:
            raise ValueError("ttl_minutes must be greater than 0")

        self._ttl = timedelta(minutes=ttl_minutes)
        self._timezone = ZoneInfo(timezone)
        self._sessions: dict[str, SessionState] = {}
        self._lock = RLock()

    def get_or_create(self, phone: str) -> SessionState:
        """Get existing session by phone or create a new one.

        Args:
            phone: Customer phone identifier.

        Returns:
            Active session state.
        """
        normalized_phone = self._normalize_phone(phone)
        now = datetime.now(self._timezone)

        with self._lock:
            self.cleanup_expired(now)

            session = self._sessions.get(normalized_phone)
            if session is None:
                session = SessionState(phone=normalized_phone)
                self._sessions[normalized_phone] = session

            session.message_count += 1
            session.last_seen_at = now
            return session

    def update(self, phone: str, patch: dict[str, Any]) -> SessionState:
        """Apply partial updates to an existing/new session.

        Args:
            phone: Customer phone identifier.
            patch: Dictionary of fields to update.

        Returns:
            Updated session state.
        """
        allowed_fields = {
            "current_step",
            "selected_service",
            "selected_service_locked",
            "service_menu_repeat_count",
            "pending_change_event_ids",
            "pending_change_action",
            "requested_date",
            "requested_time",
            "last_intent",
            "alternative_slots",
            "alternative_slots_iso",
            "alternative_slots_date",
            "awaiting_alternative_pick",
            "booking_link_sent",
            "handoff_requested",
            "confirmation_pending",
            "confirmed",
            "reminder_schedule",
            "awaiting_followup",
            "last_reference_type",
            "last_reference_date",
            "last_reference_month",
            "last_reference_year",
            "message_count",
        }

        session = self.get_or_create(phone)
        now = datetime.now(self._timezone)

        with self._lock:
            for key, value in patch.items():
                if key in allowed_fields:
                    setattr(session, key, value)
            session.last_seen_at = now
            return session

    def clear(self, phone: str) -> None:
        """Delete a session by phone.

        Args:
            phone: Customer phone identifier.
        """
        normalized_phone = self._normalize_phone(phone)
        with self._lock:
            self._sessions.pop(normalized_phone, None)

    def cleanup_expired(self, now: datetime | None = None) -> int:
        """Remove expired sessions and return number of deletions.

        Args:
            now: Current timestamp override (primarily for testing).

        Returns:
            Number of expired sessions removed.
        """
        current_time = now or datetime.now(self._timezone)

        with self._lock:
            expired_keys = [
                phone
                for phone, session in self._sessions.items()
                if current_time - session.last_seen_at > self._ttl
            ]

            for phone in expired_keys:
                del self._sessions[phone]

            return len(expired_keys)

    def stats(self) -> dict[str, int]:
        """Return lightweight manager statistics."""
        with self._lock:
            return {"active_sessions": len(self._sessions)}

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Normalize phone key used for session indexing."""
        return (phone or "").strip().lower()
