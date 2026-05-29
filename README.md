# Rezevy 💇‍♂️📅

A high-performance asynchronous backend API infrastructure developed for beauty salons, hairdressers, and barbers. It provides end-to-end appointment management with WhatsApp automation, a dynamic Webview interface, and seamless Google Calendar synchronization.

🚧 **Status:** Work in Progress (Active Development)

## ✨ Key Features

* **WhatsApp & Webview Integration:** When a customer requests an appointment via WhatsApp, the system generates a secure, one-time Webview link reflecting the business's real-time availability in seconds.
* **Google Calendar Synchronization:** Double-booking checks are performed in the background. Appointments completed via the web interface are automatically synced to the business's Google Calendar, and an instant confirmation message is sent via WhatsApp.
* **Smart Reminder Engine:** An automated reminder mechanism that dynamically triggers based on the time left to the appointment (e.g., 24 hours or 2 hours prior), minimizing "No-show" rates and revenue loss.
* **Localized LLM Integration:** The AI routing and response generation are currently optimized for Turkish Natural Language Processing (NLP) to seamlessly serve local businesses, while the underlying architecture remains completely language-agnostic and easily adaptable.
* **HMAC-SHA256 Based Security (Zero Trust):** Dynamic appointment links sent to customers are encrypted using the HMAC-SHA256 algorithm. They feature a Time-to-Live (TTL) and one-time-use architecture to prevent manipulation.
* **Funnel Analytics:** Logs which channels customers arrive from (via UTM parameters like Instagram, Google) and tracks where they drop off in the appointment process (page load, service selection, confirmation stage).
* **Built-in GDPR/KVKK Compliance:** Integrated consent mechanisms and privacy notices actively approved by users during the booking process.

## 🛠️ Tech Stack & Dependencies

* **Backend Framework:** Python / FastAPI (Fully asynchronous architecture optimized for high concurrency)
* **API & Integrations:** Google Calendar API, Twilio API (WhatsApp Gateway & Webhooks)
* **Security & Cryptography:** HMAC-SHA256 Token Validation, Environment-Based Secrecy

## 📸 Screenshots

<p align="center">

  <img src="assets/whatsapp_mesaj_basla.jpg" width="350" alt="Rezevy WhatsApp Mesaj ve Link Başlangıcı">

  <img src="assets/takvim_webview_kvkk.jpg" width="350" alt="Rezevy Takvim Webview ve KVKK Onayı">

</p>



<p align="center">

  <img src="assets/webview_tamamlandi.jpg" width="350" alt="Rezevy Web Tamamlama ve Sohbete Dönüş">

  <img src="assets/whatsapp_onay_mesaji.jpg" width="350" alt="Rezevy WhatsApp Sohbet Onay Mesajı">

</p>



---

## 📂 Folder Structure

```text
Rezevy/
├── app/
│   ├── api/
│   │   ├── calendar_routes.py   # Webview interface and booking API endpoints
│   │   └── webhook.py           # Twilio WhatsApp message and state routing
│   ├── services/
│   │   ├── calendar_service.py  # Google Calendar API integration
│   │   ├── llm_service.py       # AI / logic services
│   │   └── session_service.py   # User session and state management
│   └── main.py                  # FastAPI application entrypoint
├── logs/
│   └── funnel_events.jsonl      # Funnel analytics log records
├── .env.example                 # Environment variables template
├── .gitignore                   # Excluded files for security
└── requirements.txt             # Project dependencies
```
*(Note: Local or server-specific files such as `__pycache__`, `.venv`, `ngrok.exe`, and `credentials.json` are excluded from the repository by design.)*

## 🚀 Local Setup

Follow these steps to run the project on your local machine.

**1. Clone the repository and create a virtual environment:**
```bash
git clone [https://github.com/mardakorkut/Rezevy.git](https://github.com/mardakorkut/Rezevy.git)
cd Rezevy
python -m venv venv314
```

For Windows:
```bash
.\venv314\Scripts\activate
```

For MacOS/Linux:
```bash
source venv314/bin/activate
```

**2. Install dependencies:**
```bash
pip install -r requirements.txt
```

**3. Configure Environment Variables:**
Rename the `.env.example` file in the root directory to `.env` and fill in your API keys:

```text
# .env file

# App & Regional Settings
APP_ENV=dev
TIMEZONE=Europe/Istanbul

# LLM Provider Selection: openai | gemini
LLM_PROVIDER=gemini

# OpenAI Settings (If applicable)
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-4o-mini

# Gemini Settings (If applicable)
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash

# Google Calendar Integration
GOOGLE_CALENDAR_ID=your_calendar_id_here@group.calendar.google.com
GOOGLE_CREDENTIALS_PATH=credentials.json

# Twilio (WhatsApp) Settings
TWILIO_ACCOUNT_SID=your_twilio_sid_here
TWILIO_AUTH_TOKEN=your_twilio_auth_token_here
TWILIO_WHATSAPP_NUMBER=whatsapp:+1234567890

# Security & Config (min 32 bytes = 64 hex chars)
BOOKING_LINK_SECRET=your_super_secret_hmac_key_here

# Optional: Add your Ngrok or prod URL here
# BOOKING_BASE_URL=[https://your-ngrok-url.ngrok-free.app](https://your-ngrok-url.ngrok-free.app)

# Smart Reminder Rule (smart | double | single24 | single2 | none)
REMINDER_MODE=smart

# GDPR/KVKK & Funnel Tracking
KVKK_CONSENT_VERSION=v1
PRIVACY_NOTICE_URL=[https://example.com/kvkk-aydinlatma](https://example.com/kvkk-aydinlatma)
FUNNEL_LOG_ENABLED=true
```

**4. Start the Application and Tunnel:**
Use Uvicorn to start the FastAPI server:
```bash
python -m uvicorn app.main:app --reload
```

Open a tunnel using Ngrok (or a similar tool) so WhatsApp webhooks can reach your local server:
```bash
ngrok http 8000
```
*Do not forget to add the HTTPS link provided by Ngrok to the Sandbox Webhook URL section in the Twilio dashboard (append `/api/webhook` to the URL).*

---

## 👨‍💻 Developer

**Muhammed Arda Korkut**
Computer Engineering Student & Backend Developer
