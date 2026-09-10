# ONYX BOT V3 — Render

## ما تغيّر
- عند `/start` تظهر صورة ONYX الأصلية فقط، بدون رسالة دعائية طويلة.
- تحت الصورة أزرار تيليجرام فعلية: تحميل فيديو، تحميل صوت، المنصات المدعومة، المساعدة، من نحن؟
- زر «من نحن؟» يحتوي نبذة احترافية عن البوت ووظيفته.
- النسخة تحتوي Health Server متوافقًا مع Render.
- تم ترك `yt-dlp` بدون تثبيت رقم إصدار محدد حتى لا يفشل Build بسبب إصدار غير متاح.

## Render
Build Command:
```bash
pip install -r requirements.txt
```

Start Command:
```bash
python bot.py
```

Environment Variables الأساسية:
- `BOT_TOKEN`
- `ADMIN_IDS`
- `MAX_FILE_MB=49`
- `COOLDOWN_SECONDS=4`
- `MAX_CONCURRENT_DOWNLOADS=2`

لا ترفع ملف `.env` العام إلى GitHub.
