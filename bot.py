import asyncio
import html
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "49"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
COOLDOWN_SECONDS = max(0, int(os.getenv("COOLDOWN_SECONDS", "4")))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # مثال: @OnyxChannel
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = Path(__file__).resolve().parent
WELCOME_IMAGE_PATH = BASE_DIR / "onyx_welcome.jpg"
DB_PATH = BASE_DIR / "onyx.sqlite3"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("onyx-bot")

URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
LAST_ACTION: dict[int, float] = {}

BRAND = "ONYX BOT"

# شاشة /start متعمدة أن تكون صورة + أزرار فقط، بدون نص دعائي طويل.
ABOUT_TEXT = (
    "<b>👑 من نحن؟ | ONYX</b>\n\n"
    "أونيكس هو بوت تيليجرام مخصص لتسهيل تنزيل الوسائط العامة من المنصات المدعومة "
    "بخطوات واضحة وخيارات متعددة للفيديو والصوت.\n\n"
    "<b>ما الذي نقدمه؟</b>\n"
    "• تنزيل فيديو بجودات متعددة حسب المتاح.\n"
    "• استخراج الصوت بصيغة MP3.\n"
    "• دعم عدد كبير من المنصات العامة عبر محرك تنزيل يتم تحديثه باستمرار.\n"
    "• معالجة مؤقتة للملفات مع تنظيفها من الخادم بعد انتهاء العملية.\n\n"
    "<i>ONYX أداة مساعدة للتنزيل من الروابط العامة. استخدمها فقط للمحتوى الذي تملكه "
    "أو لديك إذن بتنزيله.</i>"
)


PLATFORMS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "x.com": "X",
    "twitter.com": "X",
    "reddit.com": "Reddit",
    "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch",
    "pinterest.com": "Pinterest",
    "soundcloud.com": "SoundCloud",
    "snapchat.com": "Snapchat",
}


@dataclass
class MediaInfo:
    title: str
    uploader: str
    duration: str
    platform: str
    thumbnail: str | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Database / Stats
# ──────────────────────────────────────────────────────────────────────────────
def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                downloads INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT,
                mode TEXT,
                quality TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        con.commit()


def touch_user(user) -> None:
    if not user:
        return
    now = int(time.time())
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen
            """,
            (user.id, user.username or "", user.first_name or "", now, now),
        )
        con.commit()


def record_download(user_id: int, platform: str, mode: str, quality: str) -> None:
    now = int(time.time())
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "INSERT INTO downloads(user_id, platform, mode, quality, created_at) VALUES(?,?,?,?,?)",
            (user_id, platform, mode, quality, now),
        )
        con.execute("UPDATE users SET downloads = downloads + 1 WHERE user_id=?", (user_id,))
        con.commit()


def get_stats() -> tuple[int, int, int]:
    with closing(sqlite3.connect(DB_PATH)) as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        downloads = con.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        today_start = int(time.time()) - 86400
        today = con.execute(
            "SELECT COUNT(*) FROM downloads WHERE created_at >= ?", (today_start,)
        ).fetchone()[0]
    return users, downloads, today


# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────
def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🎬 تحميل فيديو", callback_data="guide_video"),
            InlineKeyboardButton("🎵 تحميل صوت", callback_data="guide_audio"),
        ],
        [
            InlineKeyboardButton("🌐 المنصات المدعومة", callback_data="platforms"),
            InlineKeyboardButton("❓ المساعدة", callback_data="help"),
        ],
        [InlineKeyboardButton("👑 من نحن؟", callback_data="about")],
    ]
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton("💬 الدعم", url=SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


def quality_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("360p", callback_data="v:360"),
            InlineKeyboardButton("480p", callback_data="v:480"),
            InlineKeyboardButton("720p HD", callback_data="v:720"),
        ],
        [
            InlineKeyboardButton("1080p FHD", callback_data="v:1080"),
            InlineKeyboardButton("⭐ الأفضل", callback_data="v:best"),
        ],
        [
            InlineKeyboardButton("🎵 MP3 128", callback_data="a:128"),
            InlineKeyboardButton("🎵 MP3 192", callback_data="a:192"),
            InlineKeyboardButton("🎵 MP3 320", callback_data="a:320"),
        ],
        [
            InlineKeyboardButton("🔄 رابط آخر", callback_data="new_link"),
            InlineKeyboardButton("✖️ إلغاء", callback_data="cancel"),
        ],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="home")]])


def join_menu() -> InlineKeyboardMarkup:
    rows = []
    if REQUIRED_CHANNEL.startswith("@"):
        rows.append([
            InlineKeyboardButton(
                "📢 الاشتراك بالقناة",
                url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}",
            )
        ])
    rows.append([InlineKeyboardButton("✅ تحقق", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


async def safe_edit(query, text: str, markup=None) -> None:
    """Edit either a photo caption or a normal text message without crashing."""
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(
                caption=text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        else:
            await query.edit_message_text(
                text=text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


# ──────────────────────────────────────────────────────────────────────────────
# Access / Anti-spam
# ──────────────────────────────────────────────────────────────────────────────
async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except (TelegramError, Forbidden):
        # لا نقفل البوت بسبب إعداد قناة خاطئ؛ نسجل الخطأ فقط.
        logger.warning("Could not verify REQUIRED_CHANNEL membership")
        return True


async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    touch_user(user)
    if await is_subscribed(context, user.id):
        return True
    target = update.callback_query.message if update.callback_query else update.effective_message
    if target:
        await target.reply_text(
            "<b>🔒 قبل استخدام أونيكس</b>\n\nاشترك بالقناة المطلوبة ثم اضغط تحقق.",
            parse_mode=ParseMode.HTML,
            reply_markup=join_menu(),
        )
    return False


def rate_limited(user_id: int) -> int:
    if COOLDOWN_SECONDS <= 0 or user_id in ADMIN_IDS:
        return 0
    now = time.monotonic()
    last = LAST_ACTION.get(user_id, 0)
    remain = COOLDOWN_SECONDS - (now - last)
    if remain > 0:
        return max(1, int(remain + 0.999))
    LAST_ACTION[user_id] = now
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────────
async def send_home_message(message) -> None:
    """يرسل واجهة ONYX الرئيسية: الصورة الأصلية ثم الأزرار فقط."""
    if WELCOME_IMAGE_PATH.exists():
        with WELCOME_IMAGE_PATH.open("rb") as f:
            await message.reply_photo(
                photo=InputFile(f, filename="onyx_welcome.jpg"),
                reply_markup=main_menu(),
            )
    else:
        await message.reply_text(
            "<b>ONYX BOT</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    touch_user(update.effective_user)
    if not await require_access(update, context):
        return
    context.user_data.pop("url", None)
    context.user_data.pop("media_info", None)
    await send_home_message(update.message)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    touch_user(update.effective_user)
    await update.message.reply_text(
        "<b>❓ طريقة الاستخدام</b>\n\n"
        "① أرسل رابطًا عامًا للمقطع.\n"
        "② انتظر حتى يتعرف أونيكس على الرابط.\n"
        "③ اختر جودة الفيديو أو MP3.\n"
        "④ يتم تجهيز الملف ثم إرساله لك مباشرة.\n\n"
        "<i>ملاحظة: دعم المواقع يتغير مع تحديثات المنصات و yt-dlp.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu(),
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS or not update.message:
        return
    users, downloads, today = get_stats()
    await update.message.reply_text(
        "<b>📊 إحصائيات أونيكس</b>\n\n"
        f"👥 المستخدمون: <b>{users:,}</b>\n"
        f"⬇️ إجمالي التحميلات: <b>{downloads:,}</b>\n"
        f"🕐 آخر 24 ساعة: <b>{today:,}</b>",
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Link handling
# ──────────────────────────────────────────────────────────────────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not await require_access(update, context):
        return

    wait = rate_limited(update.effective_user.id)
    if wait:
        await update.message.reply_text(f"⏳ انتظر {wait} ثوانٍ ثم أرسل الرابط.")
        return

    match = URL_RE.search(update.message.text)
    if not match:
        await update.message.reply_text(
            "🔗 <b>أرسل رابط المقطع</b> من منصة تواصل اجتماعي عامة.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
        return

    url = match.group(0).rstrip(".,)]}>،؛!\n")
    if len(url) > 2048:
        await update.message.reply_text("⚠️ الرابط طويل جدًا.")
        return

    context.user_data["url"] = url
    status = await update.message.reply_text("<b>🔎 أونيكس يفحص الرابط...</b>", parse_mode=ParseMode.HTML)
    try:
        raw = await asyncio.to_thread(extract_info, url)
        info = normalize_info(raw, url)
        context.user_data["media_info"] = {
            "title": info.title,
            "uploader": info.uploader,
            "duration": info.duration,
            "platform": info.platform,
        }
        text = (
            "<b>✅ تم التعرف على المقطع</b>\n\n"
            f"🎬 <b>{html.escape(info.title[:180])}</b>\n"
            f"👤 {html.escape(info.uploader[:100])}\n"
            f"🌐 {html.escape(info.platform)}\n"
            f"⏱ {html.escape(info.duration)}\n\n"
            "<b>اختر الصيغة والجودة:</b>"
        )
        await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=quality_menu())
    except Exception as exc:
        logger.warning("Info extraction failed: %s", exc)
        await status.edit_text(
            "<b>⚠️ تعذر قراءة الرابط</b>\n\n"
            "تأكد أن الرابط عام وصحيح. بعض الروابط قد تحتاج تحديث yt-dlp أو قد تكون "
            "خاصة/محميّة وغير قابلة للتنزيل.",
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Callbacks
# ──────────────────────────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    touch_user(update.effective_user)

    data = query.data or ""

    if data == "check_join":
        if await is_subscribed(context, update.effective_user.id):
            await query.message.reply_text("✅ تم التحقق. أرسل /start للبدء.")
        else:
            await query.answer("اشترك بالقناة أولًا ثم حاول مرة أخرى.", show_alert=True)
        return

    if data == "home":
        if query.message and query.message.photo:
            try:
                await query.edit_message_caption(caption="", reply_markup=main_menu())
            except BadRequest:
                await send_home_message(query.message)
        elif query.message:
            await send_home_message(query.message)
        return
    if data == "new_link":
        context.user_data.pop("url", None)
        context.user_data.pop("media_info", None)
        await safe_edit(
            query,
            "<b>🔗 رابط جديد</b>\n\nأرسل الرابط الآن وسأعرض لك خيارات الجودة.",
            back_menu(),
        )
        return
    if data == "cancel":
        context.user_data.pop("url", None)
        context.user_data.pop("media_info", None)
        await safe_edit(query, "<b>✅ تم إلغاء العملية.</b>", main_menu())
        return
    if data == "platforms":
        await safe_edit(
            query,
            "<b>🌐 المنصات</b>\n\n"
            "يعتمد أونيكس على yt-dlp ويدعم عددًا كبيرًا من المواقع العامة. "
            "من أشهرها:\n\n"
            "YouTube • TikTok • Instagram • X • Facebook • Reddit • Vimeo • "
            "Twitch • Pinterest • SoundCloud وغيرها.\n\n"
            "<i>لا يمكن ضمان كل منصة دائمًا لأن المواقع تغيّر أنظمتها باستمرار.</i>",
            back_menu(),
        )
        return
    if data in {"help", "guide_video", "guide_audio"}:
        if data == "guide_audio":
            text = (
                "<b>🎧 تحميل الصوت</b>\n\nأرسل رابط المقطع، ثم اختر MP3 بجودة 128 أو 192 أو 320 kbps."
            )
        elif data == "guide_video":
            text = (
                "<b>🎬 تحميل الفيديو</b>\n\nأرسل رابط المقطع، ثم اختر 360p أو 480p أو 720p أو 1080p أو أفضل جودة متاحة."
            )
        else:
            text = (
                "<b>❓ المساعدة</b>\n\nأرسل رابطًا عامًا، ثم اختر الجودة. "
                "إذا تعذر الرابط، جرّب رابط المشاركة المباشر أو حدّث yt-dlp على السيرفر."
            )
        await safe_edit(query, text, back_menu())
        return
    if data == "about":
        await safe_edit(query, ABOUT_TEXT, back_menu())
        return

    if not (data.startswith("v:") or data.startswith("a:")):
        return
    if not await require_access(update, context):
        return

    wait = rate_limited(update.effective_user.id)
    if wait:
        await query.answer(f"انتظر {wait} ثوانٍ قبل طلب جديد.", show_alert=True)
        return

    url = context.user_data.get("url")
    if not url:
        await query.message.reply_text("🔗 أرسل الرابط من جديد أولًا.")
        return

    mode = "video" if data.startswith("v:") else "audio"
    quality = data.split(":", 1)[1]
    mode_label = f"فيديو {quality}p" if mode == "video" and quality != "best" else (
        "أفضل فيديو" if mode == "video" else f"MP3 {quality} kbps"
    )

    progress = await query.message.reply_text(
        f"<b>⏳ جاري تجهيز {html.escape(mode_label)}...</b>\n\n"
        "قد يستغرق ذلك قليلًا حسب حجم الملف والمنصة.",
        parse_mode=ParseMode.HTML,
    )

    temp_dir = Path(tempfile.mkdtemp(prefix="onyx_"))
    try:
        async with DOWNLOAD_SEMAPHORE:
            file_path, title, platform = await asyncio.to_thread(
                download_media, url, mode, quality, temp_dir
            )

        if not file_path.exists():
            raise RuntimeError("لم يتم العثور على الملف النهائي")

        size = file_path.stat().st_size
        if size > MAX_FILE_BYTES:
            await progress.edit_text(
                "<b>⚠️ الملف أكبر من الحد المسموح</b>\n\n"
                f"الحجم: <b>{size / (1024 * 1024):.1f} MB</b>\n"
                f"الحد المضبوط: <b>{MAX_FILE_MB} MB</b>\n\n"
                "جرّب جودة أقل.",
                parse_mode=ParseMode.HTML,
                reply_markup=quality_menu(),
            )
            return

        await progress.edit_text(
            f"<b>📤 جاهز — جاري الإرسال</b>\n{size / (1024 * 1024):.1f} MB",
            parse_mode=ParseMode.HTML,
        )
        caption = (
            f"<b>✦ {html.escape(title[:180])}</b>\n\n"
            f"⚙️ {html.escape(mode_label)}\n"
            "💎 بواسطة <b>أونيكس بوت</b>"
        )

        with file_path.open("rb") as f:
            common_kwargs = dict(
                caption=caption,
                parse_mode=ParseMode.HTML,
                write_timeout=180,
                read_timeout=180,
                connect_timeout=60,
            )
            if mode == "video":
                await query.message.reply_video(
                    video=InputFile(f, filename=file_path.name),
                    supports_streaming=True,
                    **common_kwargs,
                )
            else:
                await query.message.reply_audio(
                    audio=InputFile(f, filename=file_path.name),
                    title=title[:64],
                    **common_kwargs,
                )

        record_download(update.effective_user.id, platform, mode, quality)
        await progress.delete()
        await query.message.reply_text(
            "<b>✅ اكتمل التحميل</b>\nهل تريد رابطًا آخر؟",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 تحميل رابط آخر", callback_data="new_link")],
                [InlineKeyboardButton("🏠 الرئيسية", callback_data="home")],
            ]),
        )
    except Exception as exc:
        logger.exception("Download failed")
        msg = clean_error(exc)
        await progress.edit_text(
            "<b>❌ تعذر إكمال التحميل</b>\n\n"
            "قد تكون الجودة غير متاحة، أو الرابط خاصًا/محميًا، أو تغيّر نظام المنصة.\n\n"
            f"<code>{html.escape(msg)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=quality_menu(),
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────────
# yt-dlp
# ──────────────────────────────────────────────────────────────────────────────
def base_ydl_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
    }


def extract_info(url: str) -> dict:
    opts = {
        **base_ydl_options(),
        "skip_download": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        # بعض المستخرجات ترجع playlist/result wrapper حتى مع noplaylist
        if info and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if entries:
                return entries[0]
        return info


def normalize_info(info: dict, url: str) -> MediaInfo:
    title = str(info.get("title") or "بدون عنوان")
    uploader = str(info.get("uploader") or info.get("channel") or info.get("creator") or "غير معروف")
    duration_raw = info.get("duration")
    duration = format_duration(duration_raw) if isinstance(duration_raw, (int, float)) else "غير معروف"
    platform = str(info.get("extractor_key") or info.get("extractor") or detect_platform(url))
    thumbnail = info.get("thumbnail")
    return MediaInfo(title, uploader, duration, platform, thumbnail)


def detect_platform(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        for domain, name in PLATFORMS.items():
            if host == domain or host.endswith("." + domain):
                return name
        return host or "Web"
    except Exception:
        return "Web"


def video_format(quality: str) -> str:
    if quality == "best":
        return "bestvideo+bestaudio/best"
    h = int(quality)
    # Prefer exact-ish height, then fall back below requested, then a single-file stream.
    return (
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}]/"
        "bestvideo+bestaudio/best"
    )


def download_media(url: str, mode: str, quality: str, temp_dir: Path):
    output_template = str(temp_dir / "%(title).100s [%(id)s].%(ext)s")
    common = {
        **base_ydl_options(),
        "outtmpl": output_template,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "overwrites": True,
    }

    if mode == "audio":
        opts = {
            **common,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }],
        }
    else:
        opts = {
            **common,
            "format": video_format(quality),
            "merge_output_format": "mp4",
            # إعادة ترميز الحاوية فقط عند الحاجة؛ لا نفرض تحويلًا ثقيلًا لكل ملف.
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if entries:
                info = entries[0]
        title = str(info.get("title") or "Onyx Media")
        platform = str(info.get("extractor_key") or info.get("extractor") or detect_platform(url))

    files = [
        p for p in temp_dir.iterdir()
        if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".temp"}
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("لم يتم إنشاء ملف نهائي")

    preferred = [".mp3"] if mode == "audio" else [".mp4", ".mkv", ".webm", ".mov"]
    for ext in preferred:
        for path in files:
            if path.suffix.lower() == ext:
                return path, title, platform
    return files[0], title, platform


def format_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except Exception:
        return "غير معروف"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def clean_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[-1] if str(exc).strip() else exc.__class__.__name__
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return text[:260]


# ──────────────────────────────────────────────────────────────────────────────
# Render health server
# ──────────────────────────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in {"/", "/health"}:
            body = "ONYX BOT is running".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
    logger.info("Health server listening on port %s", PORT)


# ──────────────────────────────────────────────────────────────────────────────
# Global error handler / startup
# ──────────────────────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("ضع BOT_TOKEN في ملف .env أولًا")

    db_init()
    start_health_server()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    logger.info("ONYX BOT is running | max_concurrent=%s", MAX_CONCURRENT_DOWNLOADS)

    # Python 3.14 no longer creates a default event loop automatically.
    # python-telegram-bot's polling runner expects one to exist on the main thread.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    finally:
        if not loop.is_closed():
            loop.close()


if __name__ == "__main__":
    main()
