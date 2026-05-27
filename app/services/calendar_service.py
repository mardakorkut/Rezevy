"""Google Calendar service layer for appointment automation.

This module provides a focused, OOP-style manager class to:
- check slot availability via Google Calendar freebusy API
- create appointment events via Google Calendar events.insert API
"""

from __future__ import annotations

import logging
from datetime import date as date_type, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httplib2
import google_auth_httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)


class GoogleCalendarManager:
    """Encapsulates Google Calendar operations for booking workflows.

    Notes:
        - Uses Service Account credentials from a JSON file.
        - Enforces Europe/Istanbul timezone.
        - Validates business rules (business hours, not-in-the-past).
    """

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    REQUIRED_TIMEZONE = "Europe/Istanbul"
    BUSINESS_START_HOUR = 9
    BUSINESS_END_HOUR = 20
    DEFAULT_SLOT_INTERVAL_MIN = 60
    SLOT_GRANULARITY_MIN = 15
    DISPLAY_SLOT_MIN = 30
    DEFAULT_SERVICE_DURATION_MIN = 60
    GOOGLE_API_TIMEOUT_SEC = 5
    SERVICE_DURATIONS = {
        "Erkek Saç Kesimi": 30,
        "Kadın Saç Kesimi": 45,
        "Fön": 30,
        "Kesim ve Fön": 60,
        "Dip Boya": 90,
        "Tüm Boya": 120,
        "Röfle / Balyaj": 210,
        "Keratin Bakımı": 180,
        "Gelin Saçı": 180,
        "Makyaj": 45,
        "Genel İşlem": 60,
    }
    SERVICE_ALIASES = {
        "erkek sac kesimi": "Erkek Saç Kesimi",
        "erkek saç kesimi": "Erkek Saç Kesimi",
        "kadin sac kesimi": "Kadın Saç Kesimi",
        "kadın saç kesimi": "Kadın Saç Kesimi",
        "fon": "Fön",
        "fön": "Fön",
        "kesim ve fon": "Kesim ve Fön",
        "kesim ve fön": "Kesim ve Fön",
        "dip boya": "Dip Boya",
        "tum boya": "Tüm Boya",
        "tüm boya": "Tüm Boya",
        "rofle": "Röfle / Balyaj",
        "röfle": "Röfle / Balyaj",
        "balyaj": "Röfle / Balyaj",
        "röfle / balyaj": "Röfle / Balyaj",
        "keratin": "Keratin Bakımı",
        "keratin bakimi": "Keratin Bakımı",
        "keratin bakımı": "Keratin Bakımı",
        "gelin saci": "Gelin Saçı",
        "gelin saçı": "Gelin Saçı",
        "makyaj": "Makyaj",
        "genel islem": "Genel İşlem",
        "genel işlem": "Genel İşlem",
    }
    MONTH_NAMES_TR = {
        1: "Ocak",
        2: "Şubat",
        3: "Mart",
        4: "Nisan",
        5: "Mayıs",
        6: "Haziran",
        7: "Temmuz",
        8: "Ağustos",
        9: "Eylül",
        10: "Ekim",
        11: "Kasım",
        12: "Aralık",
    }

    def __init__(
        self,
        calendar_id: str,
        credentials_path: str = "credentials.json",
        timezone: str = "Europe/Istanbul",
    ) -> None:
        """Initialize the manager and create a Google Calendar client.

        Args:
            calendar_id: Target Google Calendar ID where events are managed.
            credentials_path: Path to Service Account credentials JSON.
            timezone: Must be `Europe/Istanbul`.

        Raises:
            ValueError: If timezone is not Europe/Istanbul or calendar_id is empty.
            RuntimeError: If Google Calendar client creation fails.
        """
        if not calendar_id.strip():
            raise ValueError("calendar_id is required and cannot be empty.")

        if timezone != self.REQUIRED_TIMEZONE:
            raise ValueError(
                f"Timezone must be '{self.REQUIRED_TIMEZONE}', got '{timezone}'."
            )

        self.calendar_id = calendar_id
        self.credentials_path = credentials_path
        self.timezone = ZoneInfo(self.REQUIRED_TIMEZONE)
        self.service = self._build_service()

        logger.info(
            "GoogleCalendarManager initialized for calendar_id=%s timezone=%s",
            self.calendar_id,
            self.REQUIRED_TIMEZONE,
        )

    def _build_service(self) -> Resource:
        """Create and return an authenticated Google Calendar API client.

        Returns:
            Authenticated Google Calendar service resource.

        Raises:
            RuntimeError: When credentials loading or client build fails.
        """
        try:
            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=self.SCOPES,
            )
            authed_http = google_auth_httplib2.AuthorizedHttp(
                credentials,
                http=httplib2.Http(timeout=self.GOOGLE_API_TIMEOUT_SEC),
            )
            return build("calendar", "v3", http=authed_http, cache_discovery=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to build Google Calendar service (credentials_path=%s).",
                self.credentials_path,
            )
            raise RuntimeError("Google Calendar service initialization failed.") from exc

    def _parse_datetime(self, date: str, time: str) -> datetime:
        """Parse `YYYY-MM-DD` and `HH:MM` into timezone-aware datetime.

        Args:
            date: Date string in `YYYY-MM-DD` format.
            time: Time string in `HH:MM` format (24h).

        Returns:
            Timezone-aware datetime in Europe/Istanbul.

        Raises:
            ValueError: If input format is invalid.
        """
        try:
            naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            return naive.replace(tzinfo=self.timezone)
        except ValueError as exc:
            logger.exception("Invalid date/time format. date=%s time=%s", date, time)
            raise ValueError(
                "Invalid date/time format. Expected date=YYYY-MM-DD and time=HH:MM."
            ) from exc

    @staticmethod
    def _validate_slot_granularity(slot_granularity_min: int) -> int:
        """Validate slot granularity and fallback to supported values."""
        if slot_granularity_min in (15, 30):
            return slot_granularity_min
        return 30

    def _round_up_to_slot_boundary(
        self,
        dt: datetime,
        slot_granularity_min: int | None = None,
    ) -> datetime:
        """Round datetime up to nearest slot boundary (15 or 30 minutes)."""
        granularity = self._validate_slot_granularity(
            slot_granularity_min or self.SLOT_GRANULARITY_MIN
        )

        floored = dt.replace(second=0, microsecond=0)
        remainder = floored.minute % granularity
        if remainder == 0:
            return floored

        minutes_to_add = granularity - remainder
        return floored + timedelta(minutes=minutes_to_add)

    def normalize_slot(
        self,
        date: str,
        time: str,
        slot_granularity_min: int | None = None,
    ) -> tuple[str, str]:
        """Normalize requested date/time to nearest valid booking block.

        Args:
            date: Requested date in YYYY-MM-DD format.
            time: Requested time in HH:MM format.
            slot_granularity_min: Supported values are 15 or 30.

        Returns:
            Tuple of normalized (date, time) in string format.
        """
        start_dt = self._parse_datetime(date, time)
        normalized_dt = self._round_up_to_slot_boundary(start_dt, slot_granularity_min)
        return normalized_dt.strftime("%Y-%m-%d"), normalized_dt.strftime("%H:%M")

    def normalize_to_15min(self, date: str, time: str) -> tuple[str, str]:
        """Normalize requested date/time to nearest 15-minute booking boundary."""
        return self.normalize_slot(date, time, slot_granularity_min=15)

    def get_service_duration(self, service_name: str | None) -> int:
        """Return duration in minutes for given service name with default fallback."""
        if not service_name:
            return self.DEFAULT_SERVICE_DURATION_MIN

        normalized_name = service_name.strip()
        if normalized_name in self.SERVICE_DURATIONS:
            return self.SERVICE_DURATIONS[normalized_name]

        lowered = normalized_name.lower()
        canonical_name = self.SERVICE_ALIASES.get(lowered)
        if canonical_name:
            return self.SERVICE_DURATIONS.get(canonical_name, self.DEFAULT_SERVICE_DURATION_MIN)

        return self.DEFAULT_SERVICE_DURATION_MIN

    def to_display_30min_slots(self, slots: list[datetime]) -> list[str]:
        """Convert slot datetimes to user-facing 30-minute display strings.

        Returned format: YYYY-MM-DD HH:MM
        """
        if not slots:
            return []

        display_slots: list[str] = []
        seen: set[str] = set()
        for slot in slots:
            rounded_slot = self._round_up_to_slot_boundary(slot, slot_granularity_min=30)
            rendered = rounded_slot.strftime("%Y-%m-%d %H:%M")
            if rendered not in seen:
                seen.add(rendered)
                display_slots.append(rendered)

        return display_slots

    def to_slot_iso_list(self, slots: list[datetime]) -> list[str]:
        """Convert datetime slot list to ISO strings."""
        return [slot.isoformat() for slot in slots]

    def parse_slot_iso(self, slot_iso: str) -> datetime:
        """Parse slot text into timezone-aware datetime.

        Supported formats:
            - ISO datetime string
            - `YYYY-MM-DD HH:MM`
        """
        try:
            dt = datetime.fromisoformat(slot_iso)
        except ValueError:
            dt = datetime.strptime(slot_iso, "%Y-%m-%d %H:%M")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.timezone)
        return dt.astimezone(self.timezone)

    def format_slot_human_tr(
        self,
        slot_dt: datetime,
        reference_dt: datetime | None = None,
    ) -> str:
        """Format a slot as natural Turkish date text.

        Always returns explicit date text: `D Month HH:MM` (e.g., `6 Mart 14:00`).
        """
        slot_local = slot_dt.astimezone(self.timezone)

        month_name = self.MONTH_NAMES_TR.get(slot_local.month, str(slot_local.month))
        return f"{slot_local.day} {month_name} {slot_local.strftime('%H:%M')}"

    def format_iso_slots_human_tr(self, slots_iso: list[str]) -> list[str]:
        """Format ISO slot list into natural Turkish display list."""
        formatted: list[str] = []
        for slot_iso in slots_iso:
            try:
                dt = self.parse_slot_iso(slot_iso)
                formatted.append(self.format_slot_human_tr(dt))
            except ValueError:
                continue
        return formatted

    def _validate_business_hours(self, start_dt: datetime, end_dt: datetime) -> None:
        """Validate that appointment stays within business hours.

        Business hours are inclusive start at 09:00 and inclusive end boundary at 20:00.

        Args:
            start_dt: Appointment start datetime.
            end_dt: Appointment end datetime.

        Raises:
            ValueError: If appointment is outside 09:00-20:00.
        """
        business_start = start_dt.replace(
            hour=self.BUSINESS_START_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        business_end = start_dt.replace(
            hour=self.BUSINESS_END_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        if start_dt < business_start or end_dt > business_end:
            raise ValueError("Requested slot is outside business hours (09:00-20:00).")

    def _validate_not_past(self, start_dt: datetime) -> None:
        """Validate appointment start is not in the past.

        Args:
            start_dt: Appointment start datetime.

        Raises:
            ValueError: If appointment time is older than now.
        """
        now = datetime.now(self.timezone)
        if start_dt < now:
            raise ValueError("Requested slot is in the past.")

    def _is_slot_free(self, start_dt: datetime, end_dt: datetime) -> bool:
        """Call Google Calendar freebusy API to detect conflicts.

        Args:
            start_dt: Appointment start datetime.
            end_dt: Appointment end datetime.

        Returns:
            True if no conflict exists, False otherwise.

        Raises:
            HttpError: If Google API request fails.
        """
        busy_intervals = self._get_busy_intervals(start_dt, end_dt)
        return self._is_interval_free_from_busy(start_dt, end_dt, busy_intervals)

    def _get_busy_intervals(
        self,
        time_min: datetime,
        time_max: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """Fetch busy intervals once for a requested window."""
        request_body: dict[str, Any] = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "timeZone": self.REQUIRED_TIMEZONE,
            "items": [{"id": self.calendar_id}],
        }

        result = self.service.freebusy().query(body=request_body).execute(num_retries=0)
        busy_slots = (
            result.get("calendars", {})
            .get(self.calendar_id, {})
            .get("busy", [])
        )

        intervals: list[tuple[datetime, datetime]] = []
        for slot in busy_slots:
            try:
                busy_start = datetime.fromisoformat(str(slot.get("start", "")).replace("Z", "+00:00"))
                busy_end = datetime.fromisoformat(str(slot.get("end", "")).replace("Z", "+00:00"))
                intervals.append(
                    (
                        busy_start.astimezone(self.timezone),
                        busy_end.astimezone(self.timezone),
                    )
                )
            except Exception:
                continue

        intervals.sort(key=lambda item: item[0])
        return intervals

    @staticmethod
    def _is_interval_free_from_busy(
        start_dt: datetime,
        end_dt: datetime,
        busy_intervals: list[tuple[datetime, datetime]],
    ) -> bool:
        """Check whether [start_dt, end_dt) overlaps any busy interval."""
        for busy_start, busy_end in busy_intervals:
            if start_dt < busy_end and end_dt > busy_start:
                return False
        return True

    def check_availability(
        self,
        date: str,
        time: str,
        duration_min: int,
    ) -> bool:
        """Check whether a given appointment slot is available.

        Uses Google Calendar freebusy endpoint after validating format and rules.

        Args:
            date: Appointment date (`YYYY-MM-DD`).
            time: Appointment time (`HH:MM`).
            duration_min: Slot duration in minutes.

        Returns:
            True if slot is free, False if unavailable or if any error occurs.
        """
        logger.info(
            "Checking availability. date=%s time=%s duration_min=%s",
            date,
            time,
            duration_min,
        )

        try:
            if duration_min <= 0:
                raise ValueError("duration_min must be greater than 0.")

            normalized_date, normalized_time = self.normalize_to_15min(date, time)
            start_dt = self._parse_datetime(normalized_date, normalized_time)
            end_dt = start_dt + timedelta(minutes=duration_min)

            self._validate_not_past(start_dt)
            self._validate_business_hours(start_dt, end_dt)

            is_free = self._is_slot_free(start_dt, end_dt)
            logger.info(
                "Availability result. date=%s time=%s available=%s",
                normalized_date,
                normalized_time,
                is_free,
            )
            return is_free

        except (ValueError, HttpError) as exc:
            logger.exception(
                "Availability check failed for date=%s time=%s: %s",
                date,
                time,
                exc,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Unexpected error during availability check for date=%s time=%s: %s",
                date,
                time,
                exc,
            )
            return False

    def create_event(
        self,
        date: str,
        time: str,
        user_name: str,
        service_name: str | None,
        duration_min: int | None = None,
    ) -> str:
        """Create a Google Calendar appointment event and return event_id.

        Args:
            date: Appointment date (`YYYY-MM-DD`).
            time: Appointment time (`HH:MM`).
            user_name: Customer full name.
            service_name: Requested service name.
            duration_min: Slot duration in minutes.

        Returns:
            Created Google Calendar event ID.

        Raises:
            ValueError: For invalid input or unavailable slot.
            RuntimeError: If event creation fails unexpectedly.
            HttpError: If Google API call fails.
        """
        logger.info(
            "Creating event. date=%s time=%s user_name=%s service_name=%s duration_min=%s",
            date,
            time,
            user_name,
            service_name,
            duration_min,
        )

        try:
            effective_duration_min = duration_min or self.get_service_duration(service_name)
            if effective_duration_min <= 0:
                raise ValueError("duration_min must be greater than 0.")
            if not user_name.strip():
                raise ValueError("user_name cannot be empty.")
            normalized_service_name = (service_name or "Genel İşlem").strip() or "Genel İşlem"

            normalized_date, normalized_time = self.normalize_to_15min(date, time)
            start_dt = self._parse_datetime(normalized_date, normalized_time)
            end_dt = start_dt + timedelta(minutes=effective_duration_min)

            self._validate_not_past(start_dt)
            self._validate_business_hours(start_dt, end_dt)

            if not self._is_slot_free(start_dt, end_dt):
                raise ValueError("Requested slot is not available.")

            event_body: dict[str, Any] = {
                "summary": f"{normalized_service_name} - {user_name}",
                "description": (
                    "WhatsApp bot üzerinden oluşturulan randevu. "
                    f"Müşteri: {user_name}, Hizmet: {normalized_service_name}"
                ),
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": self.REQUIRED_TIMEZONE,
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": self.REQUIRED_TIMEZONE,
                },
            }

            created_event = (
                self.service.events()
                .insert(calendarId=self.calendar_id, body=event_body)
                .execute(num_retries=0)
            )

            event_id = created_event.get("id")
            if not event_id:
                raise RuntimeError("Google Calendar API returned no event id.")

            logger.info(
                "Event created successfully. event_id=%s date=%s time=%s",
                event_id,
                normalized_date,
                normalized_time,
            )
            return event_id

        except (ValueError, HttpError) as exc:
            logger.exception(
                "Event creation failed for date=%s time=%s user_name=%s: %s",
                date,
                time,
                user_name,
                exc,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Unexpected error during event creation for date=%s time=%s user_name=%s: %s",
                date,
                time,
                user_name,
                exc,
            )
            raise RuntimeError("Unexpected error while creating calendar event.") from exc

    @staticmethod
    def _normalize_phone_for_match(phone: str) -> str:
        """Normalize phone text for robust phone ownership checks."""
        return "".join(ch for ch in (phone or "") if ch.isdigit())

    def _event_belongs_to_phone(self, event_item: dict[str, Any], phone: str) -> bool:
        """Check whether calendar event appears to belong to a WhatsApp phone number."""
        target = self._normalize_phone_for_match(phone)
        if not target:
            return False

        summary = str(event_item.get("summary", ""))
        description = str(event_item.get("description", ""))
        haystack = self._normalize_phone_for_match(f"{summary} {description}")
        return bool(target and target in haystack)

    def list_upcoming_events_by_phone(self, phone: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Return upcoming events owned by given phone number (zero-trust filter)."""
        try:
            now_iso = datetime.now(self.timezone).isoformat()
            events_result = (
                self.service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=now_iso,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=max(1, max_results * 5),
                )
                .execute(num_retries=0)
            )
            items = events_result.get("items", []) or []

            owned_events: list[dict[str, Any]] = []
            for event in items:
                if not self._event_belongs_to_phone(event, phone):
                    continue

                start_text = str((event.get("start") or {}).get("dateTime", "") or (event.get("start") or {}).get("date", ""))
                try:
                    start_dt = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
                    start_local = start_dt.astimezone(self.timezone)
                except Exception:
                    continue

                owned_events.append(
                    {
                        "id": str(event.get("id", "")),
                        "summary": str(event.get("summary", "Randevu")),
                        "start_iso": start_local.isoformat(),
                        "start_human": self.format_slot_human_tr(start_local),
                    }
                )
                if len(owned_events) >= max_results:
                    break

            return owned_events
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to list phone-owned upcoming events for phone=%s: %s", phone, exc)
            return []

    def cancel_event_if_owned(self, event_id: str, phone: str) -> bool:
        """Cancel event only if it belongs to requesting phone number."""
        try:
            event_item = (
                self.service.events()
                .get(calendarId=self.calendar_id, eventId=event_id)
                .execute(num_retries=0)
            )
            if not self._event_belongs_to_phone(event_item, phone):
                return False

            self.service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute(num_retries=0)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to cancel owned event event_id=%s phone=%s: %s", event_id, phone, exc)
            return False

    def find_all_available_slots_same_day(
        self,
        date: str,
        duration_min: int,
    ) -> list[str]:
        """Return all same-day available slot ISO values in 30-minute display cadence."""
        if duration_min <= 0:
            return []

        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return []

        slots = self._collect_available_slots_for_day(day=day, duration_min=duration_min)
        return self.to_slot_iso_list(slots)

    def _collect_available_slots_for_day(
        self,
        day: date_type,
        duration_min: int,
    ) -> list[datetime]:
        """Collect all available slots for a given day in 30-minute display cadence."""
        if duration_min <= 0:
            return []

        now = datetime.now(self.timezone)
        day_start = datetime(
            year=day.year,
            month=day.month,
            day=day.day,
            hour=self.BUSINESS_START_HOUR,
            minute=0,
            tzinfo=self.timezone,
        )
        day_end = datetime(
            year=day.year,
            month=day.month,
            day=day.day,
            hour=self.BUSINESS_END_HOUR,
            minute=0,
            tzinfo=self.timezone,
        )

        # Start from "now" for today to avoid suggesting past times.
        cursor_start = max(day_start, now) if day == now.date() else day_start
        cursor = self._round_up_to_slot_boundary(cursor_start, slot_granularity_min=30)
        interval = timedelta(minutes=self.DISPLAY_SLOT_MIN)
        busy_intervals = self._get_busy_intervals(day_start, day_end)

        available_slots: list[datetime] = []
        while cursor + timedelta(minutes=duration_min) <= day_end:
            end_dt = cursor + timedelta(minutes=duration_min)
            if self._is_interval_free_from_busy(cursor, end_dt, busy_intervals):
                available_slots.append(cursor)
            cursor += interval

        return available_slots

    def find_next_available_day_slots(
        self,
        start_date: str,
        duration_min: int,
        max_days_ahead: int = 7,
    ) -> tuple[str, list[str]] | None:
        """Find first available day within +1..max_days_ahead from start_date.

        Returns:
            Tuple of (YYYY-MM-DD date, list[slot_iso]) for the first day that has at least
            one available slot. Returns None if no availability is found.
        """
        if duration_min <= 0 or max_days_ahead <= 0:
            return None

        try:
            base_day = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            return None

        for offset in range(1, max_days_ahead + 1):
            candidate_day = base_day + timedelta(days=offset)
            candidate_slots = self._collect_available_slots_for_day(
                day=candidate_day,
                duration_min=duration_min,
            )
            if candidate_slots:
                return candidate_day.strftime("%Y-%m-%d"), self.to_slot_iso_list(candidate_slots)

        return None
