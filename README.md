# Rezevy 💇‍♂️📅

Güzellik salonları, kuaförler ve berberler için geliştirilmiş; WhatsApp otomasyonu, dinamik Webview arayüzü ve Google Takvim senkronizasyonu ile randevu süreçlerini uçtan uca yöneten, yüksek performanslı asenkron backend API altyapısı.

🚧 **Durum:** Aktif Geliştirme Aşamasında (Work in Progress)

---

### ✨ Öne Çıkan Özellikler

* **WhatsApp & Webview Entegrasyonu:** Müşteri WhatsApp üzerinden randevu talep ettiğinde, sistem saniyeler içinde işletmenin canlı müsaitlik durumunu yansıtan tek kullanımlık, şifreli bir Webview linki üretir ve iletir.
* **Google Calendar Senkronizasyonu:** Çakışma (double-booking) kontrolleri arka planda yapılarak web arayüzünden tamamlanan randevular otomatik olarak işletmenin Google Takvimi'ne işlenir ve müşteriye WhatsApp üzerinden anlık onay mesajı gönderilir.
* **Akıllı Hatırlatma Motoru (Smart Reminder):** Randevu zamanına kalan süreye göre dinamik çalışan (24 saat kala veya 2 saat kala) otomatik hatırlatma mekanizması ile "Gelmeyen Müşteri" (No-show) oranını ve işletme ciro kaybını minimuma indirir.
* **HMAC-SHA256 Tabanlı Güvenlik (Zero Trust):** Müşterilere iletilen dinamik randevu linkleri HMAC-SHA256 algoritması ile şifrelenmiş olup, süreli (Time-to-Live) ve tek kullanımlık mimariye sahiptir; manipülasyonları engeller.
* **Dönüşüm Hunisi Analitiği (Funnel Tracking):** Müşterilerin sisteme hangi kanallardan ulaştığını (Instagram, Google vb. UTM parametreleri ile) ve randevu tamamlama adımlarının hangisinde (sayfa açılışı, hizmet seçimi, onay aşaması) takıldığını loglayan analitik altyapı.
* **Yerleşik KVKK Uyumluluğu:** Randevu alımı sırasında kullanıcıdan onay alan entegre KVKK aydınlatma ve veri işleme mekanizması.

---

### 🛠️ Teknik Altyapı ve Teknolojiler

* **Backend Framework:** Python / FastAPI (Tamamen asenkron, yüksek hızlı ve yüksek eşzamanlı işlemlere [concurrency] uygun mimari)
* **API & Entegrasyonlar:** Google Calendar API, Twilio API (WhatsApp Gateway & Webhooks)
* **Güvenlik & Kriptografi:** HMAC-SHA256 Token Doğrulama, Environment Tabanlı Gizlilik

---

### 📸 Ekran Görüntüleri


---

### 📂 Proje Yapısı (Folder Structure)

Proje, Clean Architecture prensiplerine sadık kalınarak ölçeklenebilir bir modüler yapıda kurgulanmıştır:

```text
Rezevy/
├── app/
│   ├── api/
│   │   ├── calendar_routes.py   # Webview arayüzü ve rezervasyon API endpoint'leri
│   │   └── webhook.py           # Twilio WhatsApp mesaj ve state yönlendirmeleri
│   ├── services/
│   │   ├── calendar_service.py  # Google Calendar API entegrasyonu
│   │   ├── llm_service.py       # Yapay zeka / mantık servisleri
│   │   └── session_service.py   # Kullanıcı session ve state yönetimi
│   └── main.py                  # FastAPI uygulama giriş noktası (Entrypoint)
├── logs/
│   └── funnel_events.jsonl      # Dönüşüm hunisi (Funnel) log kayıtları
├── .env.example                 # Çevresel değişkenlerin şablonu (Gizli veriler hariç)
├── .gitignore                   # Güvenlik için repoya dahil edilmeyen dosyalar
└── requirements.txt             # Proje bağımlılıkları
