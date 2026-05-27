"""Webview calendar booking endpoints.

Provides:
  GET  /takvim                  — Mobile-friendly visual booking page (HTML)
  GET  /api/available-slots     — JSON list of free slots for a given date
  POST /api/confirm-booking     — Creates calendar event, sends WhatsApp confirmation
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

logger = logging.getLogger(__name__)
router = APIRouter(tags=["calendar"])

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")
BOOKING_LINK_TTL_SECONDS = 1800  # 30 minutes
DEFAULT_TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
TRACKING_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "gclid",
    "fbclid",
)

# ---------------------------------------------------------------------------
# Single-use token store  (in-memory, TTL-cleaned, thread-safe)
# ---------------------------------------------------------------------------
_used_booking_tokens: dict[str, float] = {}   # token → expiry timestamp
_used_tokens_lock = Lock()
_funnel_events: list[dict[str, Any]] = []
_funnel_events_lock = Lock()


def _clean_expired_used_tokens() -> None:
    now = time.time()
    with _used_tokens_lock:
        expired = [t for t, exp in _used_booking_tokens.items() if now > exp]
        for t in expired:
            del _used_booking_tokens[t]


def _mark_token_used(token: str, exp: int) -> None:
    _clean_expired_used_tokens()
    with _used_tokens_lock:
        _used_booking_tokens[token] = float(exp)


def _is_token_used(token: str) -> bool:
    with _used_tokens_lock:
        return token in _used_booking_tokens


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _get_booking_secret() -> str:
    secret = os.getenv("BOOKING_LINK_SECRET", "").strip()
    if len(secret) < 32:
        raise RuntimeError(
            "BOOKING_LINK_SECRET env var is missing or too short (min 32 chars)."
        )
    return secret


def _make_booking_token(phone: str, service: str, exp: int, secret: str) -> str:
    # Keep service argument for backward compatibility, but bind token to phone+exp.
    message = f"{phone}|{exp}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _verify_booking_token(
    phone: str,
    service: str,
    exp: int,
    token: str,
    secret: str,
) -> bool:
    """Return True only when token is cryptographically valid and not expired."""
    if int(time.time()) > exp:
        return False
    expected = _make_booking_token(phone, service, exp, secret)
    return hmac.compare_digest(expected, token)


def make_booking_link(
    request: Request,
    phone: str,
    service: str | None = None,
    tracking: dict[str, str] | None = None,
) -> str:
    """Build a signed, time-limited (30 min) booking URL for the given customer."""
    secret = _get_booking_secret()
    exp = int(time.time()) + BOOKING_LINK_TTL_SECONDS
    service_value = (service or "").strip()
    token = _make_booking_token(phone, service_value, exp, secret)

    base_url = os.getenv("BOOKING_BASE_URL", "").strip()
    if not base_url:
        forwarded_host = request.headers.get("x-forwarded-host", "").strip()
        forwarded_proto = request.headers.get("x-forwarded-proto", "").strip()
        host = forwarded_host or request.headers.get("host", "") or request.url.netloc
        scheme = forwarded_proto or request.url.scheme
        base_url = f"{scheme}://{host}"

    query_parts = [
        f"phone={quote_plus(phone)}",
        f"exp={exp}",
        f"token={token}",
        f"consent_version={quote_plus(_get_kvkk_consent_version())}",
        f"privacy_notice_url={quote_plus(_get_privacy_notice_url())}",
    ]
    if service_value:
        query_parts.append(f"service={quote_plus(service_value)}")

    for field_name, field_value in _sanitize_tracking_params(tracking).items():
        query_parts.append(f"{field_name}={quote_plus(field_value)}")

    query = "&".join(query_parts)

    return f"{base_url}/takvim?{query}"


def _sanitize_tracking_params(tracking: dict[str, str] | None) -> dict[str, str]:
    """Normalize and clamp campaign parameters before persisting or forwarding."""
    cleaned: dict[str, str] = {}
    for field_name in TRACKING_FIELDS:
        raw_value = (tracking or {}).get(field_name)
        value = str(raw_value or "").strip()
        if value:
            cleaned[field_name] = value[:120]
    return cleaned


def _extract_tracking_from_request(request: Request) -> dict[str, str]:
    """Extract campaign parameters from URL query fields."""
    result: dict[str, str] = {}
    for field_name in TRACKING_FIELDS:
        value = request.query_params.get(field_name, "").strip()
        if value:
            result[field_name] = value[:120]
    return result


def log_funnel_event(
    event_name: str,
    phone: str,
    event_data: dict[str, Any] | None = None,
) -> None:
    """Record funnel events in memory and JSONL for lightweight attribution analysis."""
    if not _is_funnel_log_enabled():
        return

    payload = {
        "timestamp": datetime.now(ISTANBUL_TZ).isoformat(),
        "event": event_name,
        "phone": (phone or "").strip(),
        "data": event_data or {},
    }

    with _funnel_events_lock:
        _funnel_events.append(payload)
        if len(_funnel_events) > 5000:
            del _funnel_events[:1000]

    try:
        os.makedirs("logs", exist_ok=True)
        with open(os.path.join("logs", "funnel_events.jsonl"), "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        logger.exception("funnel_log_write_failed event=%s", event_name)


# ---------------------------------------------------------------------------
# Booking page HTML  (self-contained; JS in external <script> block)
# ---------------------------------------------------------------------------

_BOOKING_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Online Rezervasyon</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#fdf6f9;min-height:100vh;padding-bottom:48px}
.wrap{max-width:480px;margin:0 auto;padding:28px 16px}
.header{text-align:center;margin-bottom:32px}
.header h1{font-size:24px;color:#c47282;font-weight:800}
.header p{color:#aaa;margin-top:6px;font-size:14px}
.badge{display:inline-block;background:#fde8ed;color:#c47282;border-radius:20px;padding:6px 20px;font-size:14px;font-weight:700;margin-top:10px}
.section{margin-bottom:24px}
.lbl{font-size:11px;font-weight:800;color:#999;margin-bottom:8px;text-transform:uppercase;letter-spacing:.6px}
.date-input{width:100%;padding:14px 16px;border:2px solid #eee;border-radius:12px;font-size:16px;color:#333;background:#fff;outline:none;cursor:pointer;-webkit-appearance:none}
.date-input:focus{border-color:#c47282}
.slots-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.slot-btn{padding:14px 6px;border:2px solid #eee;border-radius:10px;background:#fff;color:#444;font-size:16px;font-weight:700;cursor:pointer;transition:all .15s;line-height:1}
.slot-btn:hover{border-color:#c47282;color:#c47282}
.slot-btn.active{background:#c47282;border-color:#c47282;color:#fff;transform:scale(1.04)}
.confirm-btn{width:100%;padding:17px;background:#c47282;color:#fff;border:none;border-radius:12px;font-size:17px;font-weight:800;cursor:pointer;transition:background .15s;margin-top:10px;letter-spacing:.2px}
.confirm-btn:disabled{background:#e0e0e0;color:#bbb;cursor:not-allowed}
.confirm-btn:not(:disabled):active{background:#a85a6a}
.info-msg{text-align:center;padding:22px;color:#bbb;font-size:14px}
.empty-msg{text-align:center;padding:18px;color:#c47282;background:#fde8ed;border-radius:12px;font-size:14px;line-height:1.6}
.err-msg{text-align:center;padding:18px;color:#b05c00;background:#fff3e0;border-radius:12px;font-size:14px;line-height:1.6}
#success-screen{display:none;text-align:center;padding:48px 20px}
.ok-icon{font-size:64px;margin-bottom:14px}
.ok-title{font-size:22px;font-weight:800;color:#2a7a2a;margin-bottom:12px}
.ok-detail{color:#555;font-size:15px;line-height:1.8}
.wa-btn{display:inline-block;margin-top:24px;padding:15px 30px;background:#25d366;color:#fff;border-radius:12px;font-size:15px;font-weight:700;text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
  <div id="main-screen">
    <div class="header">
      <h1>🌸 Online Rezervasyon</h1>
      <p>Gün ve saatinizi takvimden kolayca seçin</p>
      <div class="badge" id="svc-badge">Yükleniyor…</div>
    </div>

    <div class="section">
            <div class="lbl">İşlem Seçin</div>
            <select class="date-input" id="svc"></select>
        </div>

        <div class="section">
      <div class="lbl">Tarih Seçin</div>
      <input type="date" class="date-input" id="dp"/>
    </div>

    <div class="section" id="slots-section" style="display:none">
      <div class="lbl">Saat Seçin</div>
      <div id="slots-wrap"><div class="info-msg">Müsait saatler yükleniyor…</div></div>
    </div>

        <div class="section">
            <label style="display:flex;gap:10px;align-items:flex-start;color:#555;font-size:13px;line-height:1.5">
                <input type="checkbox" id="kvkk-consent" style="margin-top:3px"/>
                <span>
                    KVKK aydinlatma metnini okudum ve rezervasyon icin islenmesini onayliyorum.
                    <a id="privacy-link" href="#" target="_blank" rel="noopener">Aydinlatma Metni</a>
                </span>
            </label>
            <label style="display:flex;gap:10px;align-items:flex-start;color:#777;font-size:13px;line-height:1.5;margin-top:10px">
                <input type="checkbox" id="marketing-consent" style="margin-top:3px"/>
                <span>Kampanya ve duyuru mesajlari almak istiyorum. (Opsiyonel)</span>
            </label>
        </div>

        <button class="confirm-btn" id="confirm-btn" disabled>Randevu Onayla ✓</button>
  </div>

  <div id="success-screen">
    <div class="ok-icon">✅</div>
    <div class="ok-title">Randevunuz Oluşturuldu!</div>
    <div class="ok-detail" id="ok-detail"></div>
    <a href="#" id="wa-link" class="wa-btn">💬 WhatsApp'a Dön</a>
  </div>
</div>
<script src="/static/booking.js"></script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Booking JS (served as static file to keep HTML clean / CSP-friendly)
# ---------------------------------------------------------------------------
# This JS is embedded inline so we don't need a separate static file serving setup.
# It is injected via the /static/booking.js endpoint below.

_BOOKING_JS = r"""
(function () {
  var p = new URLSearchParams(location.search);
  var phone   = p.get('phone')   || '';
    var presetService = decodeURIComponent(p.get('service') || '').trim();
    var service = presetService;
  var token   = p.get('token')   || '';
  var exp     = p.get('exp')     || '';
    var consentVersion = p.get('consent_version') || 'v1';
    var privacyNoticeUrl = p.get('privacy_notice_url') || '';
    var tracking = {
        utm_source: p.get('utm_source') || '',
        utm_medium: p.get('utm_medium') || '',
        utm_campaign: p.get('utm_campaign') || '',
        utm_content: p.get('utm_content') || '',
        utm_term: p.get('utm_term') || '',
        gclid: p.get('gclid') || '',
        fbclid: p.get('fbclid') || ''
    };

  var selDate = null, selTime = null, selIso = null;

    var svcSelect = document.getElementById('svc');
    var svcBadge = document.getElementById('svc-badge');
        var confirmBtn = document.getElementById('confirm-btn');
        var kvkkConsentCheckbox = document.getElementById('kvkk-consent');
        var marketingConsentCheckbox = document.getElementById('marketing-consent');
        var privacyLink = document.getElementById('privacy-link');
    svcBadge.textContent = service || 'Secim bekleniyor';

        if (privacyNoticeUrl) {
            privacyLink.href = privacyNoticeUrl;
        } else {
            privacyLink.href = '#';
            privacyLink.addEventListener('click', function (evt) {
                evt.preventDefault();
                alert('Aydinlatma metni baglantisi yakinda yayinda olacak.');
            });
        }

  var dp = document.getElementById('dp');
  var today = new Date().toISOString().slice(0, 10);
  var maxD  = new Date(); maxD.setDate(maxD.getDate() + 60);
  dp.min = today;
  dp.max = maxD.toISOString().slice(0, 10);
  dp.value = today;
  selDate = today;

    if (presetService) {
        svcSelect.innerHTML = '<option value="' + escapeHtml(presetService) + '" selected>' + escapeHtml(presetService) + '</option>';
        svcSelect.disabled = true;
        loadSlots(today);
    } else {
        loadServices();
    }

  dp.addEventListener('change', function () {
    selDate = this.value;
    selTime = null; selIso = null;
                confirmBtn.disabled = true;
    loadSlots(this.value);
  });

    svcSelect.addEventListener('change', function () {
        service = this.value || '';
        svcBadge.textContent = service || 'Secim bekleniyor';
        selTime = null;
        selIso = null;
        updateConfirmEnabled();
        if (!service) {
            document.getElementById('slots-section').style.display = 'none';
            document.getElementById('slots-wrap').innerHTML = '<div class="info-msg">Once bir islem secin.</div>';
            return;
        }
        loadSlots(dp.value);
    });

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    kvkkConsentCheckbox.addEventListener('change', updateConfirmEnabled);

    function buildTrackingQuery() {
        var parts = [];
        Object.keys(tracking).forEach(function (key) {
            if (tracking[key]) {
                parts.push(key + '=' + encodeURIComponent(tracking[key]));
            }
        });
        return parts.length ? '&' + parts.join('&') : '';
    }

    function updateConfirmEnabled() {
        var canConfirm = Boolean(service && selTime && kvkkConsentCheckbox.checked);
        confirmBtn.disabled = !canConfirm;
    }

    function loadServices() {
        svcSelect.innerHTML = '<option value="">Yukleniyor…</option>';
        fetch('/api/services?phone=' + encodeURIComponent(phone) + '&token=' + encodeURIComponent(token) + '&exp=' + encodeURIComponent(exp))
            .then(function (r) {
                if (r.status === 403) {
                    throw new Error('expired');
                }
                if (!r.ok) {
                    throw new Error('fetch_failed');
                }
                return r.json();
            })
            .then(function (data) {
                var services = Array.isArray(data.services) ? data.services : [];
                if (!services.length) {
                    svcSelect.innerHTML = '<option value="">Servis bulunamadi</option>';
                    return;
                }

                svcSelect.innerHTML = '<option value="">Islem seciniz</option>';
                services.forEach(function (item) {
                    var opt = document.createElement('option');
                    opt.value = item.name;
                    opt.textContent = item.duration_min ? (item.name + ' (' + item.duration_min + ' dk)') : item.name;
                    svcSelect.appendChild(opt);
                });
            })
            .catch(function (err) {
                if (err && err.message === 'expired') {
                    document.getElementById('main-screen').innerHTML =
                        '<div class="err-msg" style="margin-top:60px">⏰ Rezervasyon linkinizin suresi dolmus.<br>WhatsApp\'tan yeni link isteyin.</div>';
                    return;
                }
                svcSelect.innerHTML = '<option value="">Servisler yuklenemedi</option>';
            });
    }

  function slotApiUrl(date) {
    return (
      '/api/available-slots'
      + '?date='    + encodeURIComponent(date)
      + '&phone='   + encodeURIComponent(phone)
      + '&service=' + encodeURIComponent(service)
      + '&token='   + encodeURIComponent(token)
      + '&exp='     + encodeURIComponent(exp)
            + buildTrackingQuery()
    );
  }

  function loadSlots(date) {
        if (!service) {
            document.getElementById('slots-section').style.display = 'none';
            return;
        }

    var ss = document.getElementById('slots-section');
    var sw = document.getElementById('slots-wrap');
    ss.style.display = 'block';
    sw.innerHTML = '<div class="info-msg">Müsait saatler yükleniyor…</div>';

    fetch(slotApiUrl(date))
      .then(function (r) {
        if (r.status === 403) {
          sw.innerHTML = '<div class="err-msg">⚠️ Rezervasyon linkinizin süresi dolmuş.<br>WhatsApp\'tan yeni link isteyin.</div>';
                                        updateConfirmEnabled();
          return null;
        }
        if (!r.ok) {
          sw.innerHTML = '<div class="err-msg">Saatler yüklenirken hata oluştu. Sayfayı yenileyin.</div>';
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        if (!data.slots || data.slots.length === 0) {
          sw.innerHTML = '<div class="empty-msg">Bu gün için müsait saat bulunmuyor.<br>Lütfen farklı bir gün seçin.</div>';
          return;
        }
        var grid = document.createElement('div');
        grid.className = 'slots-grid';
        data.slots.forEach(function (slot) {
          var btn = document.createElement('button');
          btn.className = 'slot-btn';
          btn.textContent = slot.display;
          btn.dataset.iso  = slot.iso;
          btn.dataset.time = slot.display;
          btn.addEventListener('click', function () {
            document.querySelectorAll('.slot-btn').forEach(function (b) {
              b.classList.remove('active');
            });
            this.classList.add('active');
            selIso  = this.dataset.iso;
            selTime = this.dataset.time;
                                                updateConfirmEnabled();
          });
          grid.appendChild(btn);
        });
        sw.innerHTML = '';
        sw.appendChild(grid);
      })
      .catch(function () {
        document.getElementById('slots-wrap').innerHTML =
          '<div class="err-msg">Saatler yüklenirken hata oluştu. Sayfayı yenileyin.</div>';
      });
  }

    confirmBtn.addEventListener('click', function () {
        if (!service) {
            alert('Lutfen once bir islem secin.');
            return;
        }
    if (!kvkkConsentCheckbox.checked) {
      alert('Lutfen KVKK onayini verin.');
      return;
    }
    if (!selDate || !selTime) return;
    var btn = this;
    btn.disabled  = true;
    btn.textContent = 'Kaydediliyor…';

    fetch('/api/confirm-booking', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        phone:   phone,
        service: service,
        date:    selDate,
        time:    selTime,
        token:   token,
                exp:     parseInt(exp, 10),
                kvkk_consent: kvkkConsentCheckbox.checked,
                kvkk_consent_timestamp: new Date().toISOString(),
                kvkk_consent_version: consentVersion,
                marketing_consent: marketingConsentCheckbox.checked,
                utm_source: tracking.utm_source,
                utm_medium: tracking.utm_medium,
                utm_campaign: tracking.utm_campaign,
                utm_content: tracking.utm_content,
                utm_term: tracking.utm_term,
                gclid: tracking.gclid,
                fbclid: tracking.fbclid
      })
    })
      .then(function (r) {
        if (r.status === 409) {
          alert('Bu saat artık müsait değil. Lütfen başka bir saat seçin.');
          document.querySelectorAll('.slot-btn.active').forEach(function (b) {
            b.classList.remove('active');
          });
          btn.disabled    = true;
          btn.textContent = 'Randevu Onayla ✓';
          loadSlots(selDate);
                    updateConfirmEnabled();
          return null;
        }
        if (r.status === 410) {
          document.getElementById('main-screen').innerHTML =
            '<div class="err-msg" style="margin-top:60px">⚠️ Bu rezervasyon linki daha önce kullanılmış.<br>WhatsApp\'tan yeni link talep edin.</div>';
          return null;
        }
        if (r.status === 403) {
          document.getElementById('main-screen').innerHTML =
            '<div class="err-msg" style="margin-top:60px">⏰ Rezervasyon linkinizin süresi dolmuş.<br>WhatsApp\'tan yeni link isteyin.</div>';
          return null;
        }
        if (!r.ok) {
          btn.disabled    = false;
          btn.textContent = 'Randevu Onayla ✓';
          alert('Bir hata oluştu. Lütfen tekrar deneyin.');
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        document.getElementById('main-screen').style.display = 'none';
        var sc = document.getElementById('success-screen');
        sc.style.display = 'block';
                var sendStatus = data.outbound_status_message
                    || (data.outbound_sent
                        ? 'WhatsApp numaraniza onay mesaji gonderildi.'
                        : 'Onay mesaji simdi gonderilemedi, yine de rezervasyonunuz olusturuldu.');
        document.getElementById('ok-detail').innerHTML =
          '<br>📅 <strong>' + data.display_date + '</strong>'
          + '<br>⏰ <strong>' + data.display_time + '</strong>'
          + '<br>💇 ' + data.service
                    + '<br><br>' + sendStatus;
            var waLink = (data.whatsapp_deeplink || 'https://wa.me/14155238886').trim();
            document.getElementById('wa-link').href = waLink;
      })
      .catch(function () {
        btn.disabled    = false;
        btn.textContent = 'Randevu Onayla ✓';
                updateConfirmEnabled();
        alert('Bir hata oluştu. Lütfen tekrar deneyin.');
      });
  });
})();
"""


# ---------------------------------------------------------------------------
# Serve booking.js as a static endpoint
# ---------------------------------------------------------------------------

from fastapi.responses import Response as _Response  # noqa: E402


@router.get("/static/booking.js", include_in_schema=False)
async def serve_booking_js() -> _Response:
    return _Response(
        content=_BOOKING_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/takvim", response_class=HTMLResponse)
async def booking_page(
    request: Request,
    phone: str = Query(...),
    service: str | None = Query(None),
    token: str = Query(...),
    exp: int = Query(...),
) -> HTMLResponse:
    """Serve mobile-friendly visual booking page after token validation."""
    try:
        secret = _get_booking_secret()
    except RuntimeError:
        return HTMLResponse(
            content="<h2>Rezervasyon servisi yapılandırılmamış. Lütfen yöneticiyle iletişime geçin.</h2>",
            status_code=503,
        )

    service_value = (service or "").strip()

    if not _verify_booking_token(phone, service_value, exp, token, secret):
        return HTMLResponse(
            content=(
                "<div style='font-family:sans-serif;text-align:center;padding:60px 20px'>"
                "<h2>⏰ Link Süresi Doldu</h2>"
                "<p style='color:#888;margin-top:10px'>Bu rezervasyon linki geçersiz veya süresi dolmuş.<br>"
                "WhatsApp üzerinden yeni bir link talep edin.</p></div>"
            ),
            status_code=403,
        )

    # Service whitelist check
    calendar_manager = getattr(request.app.state, "calendar_manager", None)
    if service_value and calendar_manager is not None:
        valid_services = set(calendar_manager.SERVICE_DURATIONS.keys())
        if service_value not in valid_services:
            return HTMLResponse(
                content="<h2>Geçersiz hizmet seçimi. WhatsApp'tan tekrar deneyin.</h2>",
                status_code=400,
            )

    log_funnel_event(
        "booking_link_opened",
        phone,
        {
            "service": service_value,
            **_extract_tracking_from_request(request),
        },
    )

    return HTMLResponse(content=_BOOKING_HTML)


@router.get("/api/available-slots")
async def available_slots(
    request: Request,
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    phone: str = Query(...),
    service: str = Query(...),
    token: str = Query(...),
    exp: int = Query(...),
) -> JSONResponse:
    """Return available time slots for the requested date as JSON."""
    try:
        secret = _get_booking_secret()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Rezervasyon servisi yapılandırılmamış.")

    if not _verify_booking_token(phone, service, exp, token, secret):
        raise HTTPException(status_code=403, detail="Geçersiz veya süresi dolmuş token.")

    # Date range validation — only today through +60 days
    try:
        req_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz tarih formatı.")

    today = datetime.now(ISTANBUL_TZ).date()
    if req_date < today or req_date > today + timedelta(days=60):
        raise HTTPException(status_code=400, detail="Tarih aralığı dışında.")

    calendar_manager = request.app.state.calendar_manager

    # Service whitelist
    if service not in calendar_manager.SERVICE_DURATIONS:
        raise HTTPException(status_code=400, detail="Geçersiz hizmet.")

    duration_min: int = calendar_manager.get_service_duration(service)
    slots_iso: list[str] = calendar_manager.find_all_available_slots_same_day(
        date=date,
        duration_min=duration_min,
    )

    slots_display: list[dict[str, str]] = []
    for slot_iso in slots_iso:
        try:
            dt = datetime.fromisoformat(slot_iso).astimezone(ISTANBUL_TZ)
            slots_display.append({"iso": slot_iso, "display": dt.strftime("%H:%M")})
        except ValueError:
            continue

    origin = _build_origin(request)
    log_funnel_event(
        "service_selected",
        phone,
        {
            "service": service,
            "date": date,
            **_extract_tracking_from_request(request),
        },
    )
    return JSONResponse(
        {"slots": slots_display},
        headers={"Access-Control-Allow-Origin": origin},
    )


@router.get("/api/services")
async def list_services(
    request: Request,
    phone: str = Query(...),
    token: str = Query(...),
    exp: int = Query(...),
) -> JSONResponse:
    """Return service list for webview selection after token validation."""
    try:
        secret = _get_booking_secret()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Rezervasyon servisi yapılandırılmamış.")

    if not _verify_booking_token(phone, "", exp, token, secret):
        raise HTTPException(status_code=403, detail="Geçersiz veya süresi dolmuş token.")

    calendar_manager = request.app.state.calendar_manager
    services = [
        {"name": name, "duration_min": duration}
        for name, duration in calendar_manager.SERVICE_DURATIONS.items()
    ]

    origin = _build_origin(request)
    return JSONResponse(
        {"services": services},
        headers={"Access-Control-Allow-Origin": origin},
    )


class ConfirmBookingRequest(BaseModel):
    phone: str
    service: str
    date: str
    time: str
    token: str
    exp: int
    kvkk_consent: bool = False
    kvkk_consent_timestamp: str | None = None
    kvkk_consent_version: str | None = None
    marketing_consent: bool = False
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    gclid: str | None = None
    fbclid: str | None = None


@router.post("/api/confirm-booking")
async def confirm_booking(
    request: Request,
    body: ConfirmBookingRequest,
) -> JSONResponse:
    """Validate token, create calendar event, send WhatsApp confirmation."""
    try:
        secret = _get_booking_secret()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Rezervasyon servisi yapılandırılmamış.")

    # Step 1: Token HMAC + expiry
    if not _verify_booking_token(body.phone, body.service, body.exp, body.token, secret):
        raise HTTPException(status_code=403, detail="Geçersiz veya süresi dolmuş rezervasyon linki.")

    # Step 1b: Single-use check
    if _is_token_used(body.token):
        raise HTTPException(status_code=410, detail="Bu rezervasyon linki daha önce kullanıldı.")

    # Step 2: Service whitelist
    calendar_manager = request.app.state.calendar_manager
    if body.service not in calendar_manager.SERVICE_DURATIONS:
        raise HTTPException(status_code=400, detail="Geçersiz hizmet seçimi.")

    # Step 3: Input format validation
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", body.date):
        raise HTTPException(status_code=400, detail="Geçersiz tarih formatı.")
    if not re.match(r"^\d{2}:\d{2}$", body.time):
        raise HTTPException(status_code=400, detail="Geçersiz saat formatı.")

    # Step 4: Phone format validation
    if not re.match(r"^(whatsapp:)?\+?\d{7,15}$", body.phone):
        raise HTTPException(status_code=400, detail="Geçersiz telefon numarası formatı.")

    if not body.kvkk_consent:
        raise HTTPException(
            status_code=400,
            detail="KVKK onayı olmadan rezervasyon tamamlanamaz.",
        )

    consent_version = (body.kvkk_consent_version or "").strip() or _get_kvkk_consent_version()
    consent_timestamp = (body.kvkk_consent_timestamp or "").strip()
    if not consent_timestamp:
        consent_timestamp = datetime.now(ISTANBUL_TZ).isoformat()

    tracking_data = _sanitize_tracking_params(
        {
            "utm_source": body.utm_source or "",
            "utm_medium": body.utm_medium or "",
            "utm_campaign": body.utm_campaign or "",
            "utm_content": body.utm_content or "",
            "utm_term": body.utm_term or "",
            "gclid": body.gclid or "",
            "fbclid": body.fbclid or "",
        }
    )

    log_funnel_event(
        "slot_selected",
        body.phone,
        {
            "service": body.service,
            "date": body.date,
            "time": body.time,
            "consent_timestamp": consent_timestamp,
            "consent_version": consent_version,
            **tracking_data,
        },
    )

    session_manager = request.app.state.session_manager

    duration_min: int = calendar_manager.get_service_duration(body.service)

    # Step 5: Create calendar event
    try:
        event_id: str = calendar_manager.create_event(
            date=body.date,
            time=body.time,
            user_name=body.phone,
            service_name=body.service,
            duration_min=duration_min,
        )
    except ValueError as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("not available", "past", "business", "slot")):
            raise HTTPException(
                status_code=409,
                detail="Seçilen saat artık müsait değil. Lütfen başka bir saat seçin.",
            ) from exc
        raise HTTPException(status_code=400, detail="Geçersiz rezervasyon bilgileri.") from exc
    except Exception:
        logger.exception("confirm_booking: event creation failed phone=%s", body.phone)
        raise HTTPException(status_code=500, detail="Randevu oluşturulurken bir hata oluştu.")

    # Step 6: Mark token as used (only after successful creation)
    _mark_token_used(body.token, body.exp)

    # Human-readable date string
    _MONTH_TR = {
        1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
        7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
    }
    try:
        dt = datetime.strptime(body.date, "%Y-%m-%d")
        display_date = f"{dt.day} {_MONTH_TR[dt.month]} {dt.year}"
    except (ValueError, KeyError):
        display_date = body.date

    # Step 7: Outbound WhatsApp confirmation via real Twilio API call
    outbound_sent = False
    outbound_error: str | None = None
    outbound_status_message: str | None = None
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    bot_number = _get_twilio_whatsapp_sender()

    if not account_sid or not auth_token or not bot_number:
        twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
        twilio_whatsapp_from = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
        twilio_whatsapp_number = os.getenv("TWILIO_WHATSAPP_NUMBER", "").strip()
        logger.error(
            "confirm_booking: outbound skipped due to missing Twilio env vars "
            "(TWILIO_ACCOUNT_SID=%s TWILIO_AUTH_TOKEN=%s TWILIO_PHONE_NUMBER=%s TWILIO_WHATSAPP_FROM=%s TWILIO_WHATSAPP_NUMBER=%s)",
            bool(account_sid),
            bool(auth_token),
            bool(twilio_phone_number),
            bool(twilio_whatsapp_from),
            bool(twilio_whatsapp_number),
        )
        outbound_error = "Twilio credentials or sender number missing"
        outbound_status_message = _build_outbound_status_message(False, outbound_error)
    else:
        from_whatsapp = _normalize_whatsapp_number(bot_number)
        to_whatsapp = _normalize_whatsapp_number(body.phone)
        if not from_whatsapp or not to_whatsapp:
            logger.error(
                "confirm_booking: outbound skipped due to invalid number formatting from=%s to=%s",
                bot_number,
                body.phone,
            )
            outbound_error = "Invalid WhatsApp number format"
            outbound_status_message = _build_outbound_status_message(False, outbound_error)
        else:
            try:
                client = Client(account_sid, auth_token)
                message_text = (
                    f"✅ Randevunuz başarıyla oluşturuldu!\n\n"
                    f"📅 Tarih: {display_date}\n"
                    f"⏰ Saat: {body.time}\n"
                    f"💇 Hizmet: {body.service}\n\n"
                    f"Bizi tercih ettiğiniz için teşekkürler, görüşmek üzere! 🌸"
                )
                sent_message = client.messages.create(
                    from_=from_whatsapp,
                    to=to_whatsapp,
                    body=message_text,
                )
                outbound_sent = True
                outbound_error = None
                outbound_status_message = _build_outbound_status_message(True, None)
                logger.info(
                    "confirm_booking: WhatsApp outbound sent phone=%s sid=%s",
                    body.phone,
                    getattr(sent_message, "sid", None),
                )
            except TwilioRestException as exc:
                logger.exception(
                    "confirm_booking: WhatsApp outbound failed phone=%s twilio_code=%s",
                    body.phone,
                    getattr(exc, "code", None),
                )
                twilio_code = getattr(exc, "code", None)
                error_text = str(exc).strip()
                outbound_error = (
                    f"Twilio error {twilio_code}: {error_text}" if twilio_code else error_text
                )[:240]
                outbound_status_message = _build_outbound_status_message(False, outbound_error)
            except Exception as exc:
                logger.exception(
                    "confirm_booking: WhatsApp outbound failed phone=%s", body.phone
                )
                outbound_error = str(exc)[:240]
                outbound_status_message = _build_outbound_status_message(False, outbound_error)

    # Step 8: Reset session to idle
    try:
        session_manager.update(
            body.phone,
            {
                "current_step": "idle",
                "selected_service": None,
                "selected_service_locked": False,
                "booking_link_sent": False,
                "service_menu_repeat_count": 0,
                "requested_date": None,
                "requested_time": None,
                "alternative_slots": [],
                "alternative_slots_iso": [],
                "alternative_slots_date": None,
                "awaiting_alternative_pick": False,
                "awaiting_followup": False,
                "confirmation_pending": False,
            },
        )
    except Exception:
        logger.warning("confirm_booking: session reset failed phone=%s", body.phone)

    origin = _build_origin(request)
    log_funnel_event(
        "booking_confirmed",
        body.phone,
        {
            "event_id": event_id,
            "service": body.service,
            "date": body.date,
            "time": body.time,
            "outbound_sent": outbound_sent,
            "consent_timestamp": consent_timestamp,
            "consent_version": consent_version,
            "marketing_consent": bool(body.marketing_consent),
            **tracking_data,
        },
    )
    return JSONResponse(
        {
            "success": True,
            "event_id": event_id,
            "display_date": display_date,
            "display_time": body.time,
            "service": body.service,
            "outbound_sent": outbound_sent,
            "outbound_error": outbound_error,
            "outbound_status_message": outbound_status_message,
            "whatsapp_contact": _normalize_whatsapp_number(bot_number),
            "whatsapp_deeplink": _build_whatsapp_deeplink(bot_number),
        },
        headers={"Access-Control-Allow-Origin": origin},
    )


def _build_origin(request: Request) -> str:
    """Return allowed CORS origin matching the current host (ngrok-safe)."""
    base_url = os.getenv("BOOKING_BASE_URL", "").strip()
    if base_url:
        return base_url
    forwarded_host = request.headers.get("x-forwarded-host", "").strip()
    forwarded_proto = request.headers.get("x-forwarded-proto", "").strip()
    host = forwarded_host or request.headers.get("host", "") or request.url.netloc
    scheme = forwarded_proto or request.url.scheme
    return f"{scheme}://{host}"


def _normalize_whatsapp_number(raw: str) -> str:
    """Normalize raw number to Twilio WhatsApp format: whatsapp:+905xxxxxxxxx."""
    value = (raw or "").strip()
    if not value:
        return ""

    if value.lower().startswith("whatsapp:"):
        value = value.split(":", 1)[1].strip()

    cleaned = "".join(ch for ch in value if ch.isdigit() or ch == "+")
    if not cleaned:
        return ""

    if cleaned.startswith("+"):
        cleaned = "+" + cleaned[1:].replace("+", "")
    else:
        cleaned = "+" + cleaned.replace("+", "")

    if not re.fullmatch(r"\+\d{7,15}", cleaned):
        return ""

    return f"whatsapp:{cleaned}"


def _get_twilio_whatsapp_sender() -> str:
    """Return configured Twilio WhatsApp sender, defaulting to sandbox number."""
    twilio_whatsapp_from = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    twilio_whatsapp_number = os.getenv("TWILIO_WHATSAPP_NUMBER", "").strip()
    twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
    return (
        twilio_whatsapp_from
        or twilio_whatsapp_number
        or twilio_phone_number
        or DEFAULT_TWILIO_WHATSAPP_FROM
    )


def _build_whatsapp_deeplink(raw: str) -> str:
    """Build a wa.me link for the given WhatsApp number."""
    normalized = _normalize_whatsapp_number(raw)
    if not normalized:
        normalized = DEFAULT_TWILIO_WHATSAPP_FROM

    raw_number = normalized.replace("whatsapp:", "").replace("+", "")
    return f"https://wa.me/{raw_number}"


def _build_outbound_status_message(outbound_sent: bool, outbound_error: str | None) -> str:
    """Return a concise user-facing WhatsApp delivery status message."""
    if outbound_sent:
        return "WhatsApp numaranıza onay mesajı gönderildi."

    if not outbound_error:
        return "Onay mesajı şu anda gönderilemedi, yine de rezervasyonunuz oluşturuldu."

    normalized = outbound_error.lower()
    if "credentials or sender number missing" in normalized:
        return "Onay mesajı gönderilemedi; Twilio gönderici ayarları eksik görünüyor."
    if "invalid whatsapp number format" in normalized:
        return "Onay mesajı gönderilemedi; WhatsApp numara formatı geçersiz görünüyor."
    if "63007" in normalized or "channel did not accept" in normalized:
        return "Onay mesajı gönderilemedi; Twilio WhatsApp kanalı bu gönderimi kabul etmedi."
    if "21608" in normalized or "not a valid" in normalized:
        return "Onay mesajı gönderilemedi; hedef numara Twilio için geçerli görünmüyor."
    if "63016" in normalized or "template" in normalized:
        return "Onay mesajı gönderilemedi; Twilio WhatsApp şablon veya oturum kuralına takıldı."
    if "authenticate" in normalized or "20003" in normalized:
        return "Onay mesajı gönderilemedi; Twilio kimlik doğrulama ayarlarını kontrol edin."

    return "Onay mesajı şu anda gönderilemedi, yine de rezervasyonunuz oluşturuldu."


def _is_funnel_log_enabled() -> bool:
    value = os.getenv("FUNNEL_LOG_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _get_kvkk_consent_version() -> str:
    return os.getenv("KVKK_CONSENT_VERSION", "v1").strip() or "v1"


def _get_privacy_notice_url() -> str:
    return os.getenv("PRIVACY_NOTICE_URL", "").strip() or "https://example.com/kvkk-aydinlatma"
