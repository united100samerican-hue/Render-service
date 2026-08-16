# Social Media Render Service

خدمة Render مستقلة لمصادر الوسائط الخارجية. الاسم محايد ولا يعتمد على YouTube وحده.

يدعم أي مصدر يوفره `yt-dlp`، مع دعم الفيديو والصوت أو الصوت فقط عند عدم توفر فيديو مناسب.

## الملفات

- `app.py` — HTTP API فقط.
- `service.py` — orchestration والجلسات والتشغيل.
- `extractor.py` — yt-dlp، الكوكيز، الاستخراج والتنزيل.
- `player.py` — PyTgCalls فقط.
- `session.py` — حالة كل مجموعة وLock.
- `models.py` — نماذج البيانات.
- `config.py` — الإعدادات والمتغيرات.
- `cleanup.py` — تنظيف الملفات.
- `media.py` — أدوات ffprobe وحجم الملفات.
- `errors.py` — أخطاء الخدمة.

## متغيرات البيئة

مطلوبة:
- `API_ID`
- `API_HASH`
- `SESSION_STRING`
- `KEEPALIVE_SECRET`

للكوكيز:
- `SOCIAL_COOKIES_FILE=/etc/secrets/youtube_cookies.txt`

اختيارية:
- `SOCIAL_MEDIA_DIR=/tmp/render_social_media`
- `SOCIAL_MAX_MEDIA_BYTES=1073741824`
- `SOCIAL_MAX_DURATION=21600`
- `SOCIAL_MAX_HEIGHT=720`
- `SOCIAL_REQUEST_TIMEOUT=120`
- `SOCIAL_MEDIA_USER_AGENT`

يجب أن يكون ملف الكوكيز بصيغة Netscape/Mozilla وأن يبدأ بـ `# Netscape HTTP Cookie File` أو `# HTTP Cookie File`.

لا يتم وضع ملف الكوكيز داخل GitHub؛ استخدم Render Secret File.

## API

- `GET /ping`
- `GET /health`
- `GET /state/{chat_id}`
- `POST /meta`
- `POST /start`
- `POST /next`
- `POST /skip`
- `POST /pause`
- `POST /resume`
- `POST /seek`
- `POST /stop`

لا توجد Queue داخل Render. Worker/D1 سيكون مصدر قائمة التشغيل الوحيد لاحقًا.
