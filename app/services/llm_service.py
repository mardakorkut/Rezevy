"""LLM intent parsing service for WhatsApp appointment messages."""


# Updated: Multi-provider LLM intent parser (OpenAI & Gemini)
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

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
    "keratin": "Keratin Bakımı",
    "keratin bakimi": "Keratin Bakımı",
    "keratin bakımı": "Keratin Bakımı",
    "gelin saci": "Gelin Saçı",
    "gelin saçı": "Gelin Saçı",
    "makyaj": "Makyaj",
    "genel islem": "Genel İşlem",
    "genel işlem": "Genel İşlem",
}

CANONICAL_SERVICE_NAMES = tuple(SERVICE_DURATIONS.keys())
GENERIC_SERVICE_HINTS = {
    "işlem",
    "islem",
    "bakım",
    "bakim",
    "hizmet",
    "uygulama",
}

class IntentParser:
    """
    LLM tabanlı intent parser. .env'deki LLM_PROVIDER'a göre OpenAI veya Gemini API ile çalışır.
    Çıktı şeması ve prompt iki provider'da da aynıdır.
    """
    ALLOWED_INTENTS = {"randevu_al", "bilgi_sor", "diger"}

    def __init__(self) -> None:
        """
        Provider ve model bilgilerini .env'den okur. Gerekli API client'ını hazırlar.
        """
        self.provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
        if self.provider == "openai":
            from openai import OpenAI
            self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
            self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
            if not self.api_key:
                raise ValueError("OPENAI_API_KEY is required in environment variables.")
            self.client = OpenAI(api_key=self.api_key)
        elif self.provider == "gemini":
            try:
                from google import genai
            except ImportError as e:
                raise ImportError("google-genai paketi eksik. 'pip install google-genai' ile kur.") from e
            self.api_key = os.getenv("GEMINI_API_KEY", "").strip()
            self.model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY is required in environment variables.")
            self.client = genai.Client(api_key=self.api_key)
        else:
            raise ValueError(f"Desteklenmeyen LLM_PROVIDER: {self.provider}")

    def parse_message(self, user_message: str, current_datetime: str) -> dict[str, Any]:
        """
        Kullanıcı mesajını LLM ile parse edip intent, date, time, service döndürür.
        Hangi provider seçiliyse ona göre API çağrısı yapar.
        """
        if not user_message or not user_message.strip():
            return self._default_response()

        try:
            system_prompt = self._build_system_prompt(current_datetime=current_datetime)
            if self.provider == "openai":
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                )
                raw_content = response.choices[0].message.content or "{}"
            elif self.provider == "gemini":
                prompt = f"{system_prompt}\nKullanıcı mesajı: {user_message}"
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt
                )
                raw_content = (getattr(resp, "text", None) or "{}").strip()
                raw_content = raw_content.replace("```json", "").replace("```", "").strip()
            else:
                return self._default_response()

            parsed = self._safe_parse_json(raw_content)
            normalized = self._normalize_output(parsed, user_message=user_message)
            return normalized

        except Exception as exc:
            logger.exception("Intent parsing failed (provider=%s): %s", self.provider, exc)
            return self._default_response()

    def generate_contextual_reply(
        self,
        user_message: str,
        parsed_intent: dict[str, Any],
        session_state: dict[str, Any],
        alternative_slots: list[str] | None = None,
    ) -> str:
        """Generate context-aware assistant reply using session and slot context.

        This method is deterministic and does not call external LLM APIs. It is
        designed for webhook response composition after `parse_message` is done.

        Args:
            user_message: Raw user message text.
            parsed_intent: Parsed payload returned by `parse_message`.
            session_state: Current session dictionary.
            alternative_slots: Optional list of suggested alternative slots.

        Returns:
            Turkish response string suitable for WhatsApp output.
        """
        intent = str(parsed_intent.get("intent") or "diger")
        current_step = str(session_state.get("current_step") or "idle")

        if intent == "randevu_al":
            return (
                "Randevu icin lutfen size gonderilen linkten islem yapin. "
                "Link uzerinden gun ve saati secmeniz yeterli."
            )

        if current_step == "awaiting_datetime":
            return (
                "Randevu icin lutfen size gonderilen linkten islem yapin. "
                "Link uzerinden gun ve saati secmeniz yeterli."
            )

        if any(word in (user_message or "").lower() for word in ["fiyat", "ücret", "konum", "adres"]):
            return (
                "Memnuniyetle yardimci olurum 💆‍♀️ "
                "Randevu icin size gonderilen linkten islem yapabilirsiniz."
            )

        return (
            "Su an icin randevu planlama konusunda yardimci olabiliyorum. "
            "Randevu icin size gonderilen linki kullanabilirsiniz."
        )

    def _build_system_prompt(self, current_datetime: str) -> str:
        """
        Her iki provider için de aynı promptu üretir.
        """
        return (
            "Sen Türkçe konuşan bir randevu asistanı intent parser'sın. "
            "Görevin kullanıcı mesajını analiz edip SADECE geçerli JSON döndürmek.\n"
            f"Bugünün tarihi ve saati: {current_datetime}\n"
            "Kurallar:\n"
            "1) Sadece JSON döndür. Açıklama, markdown, kod bloğu ekleme.\n"
            "2) JSON şeması kesinlikle şu olmalı:\n"
            '{"intent":"randevu_al|bilgi_sor|diger",'
            '"date":"YYYY-MM-DD|null",'
            '"time":"HH:MM|null",'
            '"service":"string|null"}\n'
            "3) 'yarın', 'haftaya salı', 'akşamüstü' gibi ifadeleri bugünün tarihine göre çözümle.\n"
            "4) Tarih/saat net değilse ilgili alan(lar) null olsun.\n"
            "5) intent yalnızca şu değerlerden biri olabilir: randevu_al, bilgi_sor, diger.\n"
            "6) Tarih formatı YYYY-MM-DD, saat formatı HH:MM (24 saat).\n"
            f"7) service alanı SADECE şu listeden biri olmalı: {', '.join(CANONICAL_SERVICE_NAMES)}\n"
            "8) Kullanıcı açık hizmet belirtmediyse service alanını null döndür.\n"
            "9) Kullanıcı belirsiz ama hizmet niyeti belirten ifade kullanırsa (örn işlem/bakım) service='Genel İşlem' döndür."
        )

    def _safe_parse_json(self, raw_content: str) -> dict[str, Any]:
        """
        Model çıktısını güvenli şekilde parse eder, kod bloğu/markdown temizler.
        """
        try:
            cleaned = raw_content.strip()
            cleaned = cleaned.replace("```json", "").replace("```", "").strip()
            if not cleaned.startswith("{"):
                json_match = re.search(r"\{[\s\S]*\}", cleaned)
                if json_match:
                    cleaned = json_match.group(0)
            parsed = json.loads(cleaned)
            if not isinstance(parsed, dict):
                return self._default_response()
            return parsed
        except Exception as exc:
            logger.exception("Failed to parse model JSON output: %s", exc)
            return self._default_response()

    def _normalize_output(self, data: dict[str, Any], user_message: str) -> dict[str, Any]:
        """
        Çıktıyı şemaya uygun normalize eder.
        """
        default = self._default_response()

        intent = self._normalize_intent_value(data.get("intent"))

        date_value = data.get("date")
        if not self._is_valid_date_or_null(date_value):
            date_value = None

        time_value = data.get("time")
        if not self._is_valid_time_or_null(time_value):
            time_value = None

        service_value = self._normalize_service_value(
            service_value=data.get("service"),
            user_message=user_message,
        )

        return {
            "intent": intent,
            "date": date_value,
            "time": time_value,
            "service": service_value,
        }

    def _normalize_service_value(self, service_value: Any, user_message: str) -> str | None:
        """Normalize parsed service name into canonical whitelist or null when unspecified."""
        generic_hint = self._has_generic_service_hint(user_message)

        if service_value is None:
            return "Genel İşlem" if generic_hint else None

        raw = str(service_value).strip()
        if not raw:
            return "Genel İşlem" if generic_hint else None

        if raw in SERVICE_DURATIONS:
            return raw

        lowered = raw.lower()
        alias_mapped = SERVICE_ALIASES.get(lowered)
        if alias_mapped:
            return alias_mapped

        for canonical_name in CANONICAL_SERVICE_NAMES:
            if lowered in canonical_name.lower() or canonical_name.lower() in lowered:
                return canonical_name

        return "Genel İşlem" if generic_hint else None

    @staticmethod
    def _has_generic_service_hint(user_message: str) -> bool:
        """Return True if user mentions generic service intent without explicit service name."""
        text = (user_message or "").lower()
        return any(keyword in text for keyword in GENERIC_SERVICE_HINTS)

    def _normalize_intent_value(self, intent_value: Any) -> str:
        """Normalize intent synonyms returned by model to allowed values."""
        if not isinstance(intent_value, str):
            return "diger"

        normalized = intent_value.strip().lower().replace("-", "_").replace(" ", "_")
        mapping = {
            "randevu_al": "randevu_al",
            "randevual": "randevu_al",
            "book": "randevu_al",
            "booking": "randevu_al",
            "availability": "randevu_al",
            "bilgi_sor": "bilgi_sor",
            "soru": "bilgi_sor",
            "info": "bilgi_sor",
            "diger": "diger",
            "other": "diger",
            "unknown": "diger",
        }
        mapped = mapping.get(normalized, "diger")
        return mapped if mapped in self.ALLOWED_INTENTS else "diger"

    def _default_response(self) -> dict[str, Any]:
        """
        Hatalı durumda güvenli fallback şeması döner.
        """
        return {
            "intent": "diger",
            "date": None,
            "time": None,
            "service": None,
        }

    @staticmethod
    def _is_valid_date_or_null(value: Any) -> bool:
        if value is None:
            return True
        if not isinstance(value, str):
            return False
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))

    @staticmethod
    def _is_valid_time_or_null(value: Any) -> bool:
        if value is None:
            return True
        if not isinstance(value, str):
            return False
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            return False
        hour = int(value[:2])
        minute = int(value[3:5])
        return 0 <= hour <= 23 and 0 <= minute <= 59
