import asyncio
import datetime
import io
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import requests
import yt_dlp

# Load .env
import os

_env = {}
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text("utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _env[_k.strip()] = _v.strip()

def _env_get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _env.get(key) or default

TOKEN = _env_get("TOKEN")
ADMIN_ID = int(_env_get("ADMIN_ID", "0"))
GOPAY = _env_get("GOPAY", "085809117547")
QRIS_PATH = str(Path(__file__).parent / _env_get("QRIS_FILE", "qris.png"))
PRICE = 25000
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)



BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
CLIPS_DIR = DOWNLOAD_DIR / "clips"
SCHEDULE_DIR = DOWNLOAD_DIR / "scheduled"
for d in [DOWNLOAD_DIR, CLIPS_DIR, SCHEDULE_DIR]:
    d.mkdir(exist_ok=True)

_ffmpeg_local = str(BASE_DIR / "ffmpeg" / "ffmpeg.exe")
FFMPEG_PATH = _ffmpeg_local if os.path.exists(_ffmpeg_local) else "ffmpeg"
FONT_PATH = "C:/Windows/Fonts/arial.ttf" if os.name == "nt" else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
if not os.path.exists(FONT_PATH):
    for _p in ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc"]:
        if os.path.exists(_p):
            FONT_PATH = _p
            break
MAX_FILE_SIZE = 50 * 1024 * 1024
DB_PATH = str(BASE_DIR / "bot.db")
COOKIES_PATH = str(BASE_DIR / "cookies.txt")

URL_PATTERN = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_+.~#?&/=]*)",
)

_app: Application | None = None
_loop: asyncio.AbstractEventLoop | None = None
_cancel_events: dict[int, asyncio.Event] = {}
_processing_locks: dict[int, asyncio.Lock] = {}
_url_store: dict[str, tuple[str, float]] = {}
_url_counter: int = 0
URL_STORE_TTL = 600  # 10 menit
_rate_limit: dict[int, float] = {}
RATE_LIMIT_SEC = 5

FYP_TAGS = [
    "#fyp #foryou #viral #trending #videoviral #fypシ",
    "#fypage #viralvideo #trend #reels #explore #explorepage",
    "#viralpost #fy #foryoupage #trendingvideo #videoviral #fypdong",
    "#fypviral #viralreels #trendingreels #exploremore #fyp゚ #viral_video",
    "#foryoupageシ #viralcontent #trendingnow #explorepage✨ #fypシ゚viral",
]

# ── Database ──

def db_init():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            sub_id TEXT UNIQUE,
            password TEXT,
            username TEXT,
            expiry DATE,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_path TEXT,
            caption TEXT,
            schedule_time TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS facebook_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            page_id TEXT NOT NULL,
            page_name TEXT,
            access_token TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS social_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_path TEXT,
            caption TEXT,
            platform TEXT DEFAULT 'facebook',
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    try:
        c.execute("ALTER TABLE users ADD COLUMN sub_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN password TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sub_id ON users(sub_id)")
    except sqlite3.OperationalError:
        pass
    # migrate old user_id=0 to unique negative id
    c.execute("SELECT user_id FROM users WHERE user_id=0")
    if c.fetchone():
        c.execute("SELECT COALESCE(MIN(user_id), -1) FROM users WHERE user_id<0")
        r = c.fetchone()
        new_id = (r[0] or -1) - 1
        c.execute("UPDATE users SET user_id=? WHERE user_id=0", (new_id,))
    conn.commit()
    conn.close()


def db_user_get(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, sub_id, password, username, expiry, is_admin FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"user_id": row[0], "sub_id": row[1], "password": row[2], "username": row[3], "expiry": row[4], "is_admin": row[5]}
    return None


def db_user_upsert(user_id: int, username: str | None, days: int = 0, sub_id: str | None = None, password: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.date.today()
    c.execute("SELECT expiry FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row and row[0]:
        old = datetime.date.fromisoformat(row[0])
        new_exp = max(old, now) + datetime.timedelta(days=days)
    else:
        new_exp = now + datetime.timedelta(days=days)
    c.execute("""
        INSERT INTO users (user_id, username, expiry, is_admin, sub_id, password)
        VALUES (?, ?, ?, 0, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=?, expiry=?, sub_id=COALESCE(?, sub_id), password=COALESCE(?, password)
    """, (user_id, username, new_exp.isoformat(), sub_id, password, username, new_exp.isoformat(), sub_id, password))
    conn.commit()
    conn.close()


def db_user_set_admin(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def db_all_users() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, sub_id, username, expiry, is_admin FROM users ORDER BY expiry DESC")
    rows = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "sub_id": r[1], "username": r[2], "expiry": r[3], "is_admin": r[4]} for r in rows]


def db_register_sub(sub_id: str, password: str, days: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.date.today()
    expiry = now + datetime.timedelta(days=days)
    try:
        c.execute("SELECT COALESCE(MIN(user_id), 0) FROM users WHERE user_id < 0")
        row = c.fetchone()
        placeholder_id = (row[0] or 0) - 1
        c.execute(
            "INSERT INTO users (user_id, sub_id, password, expiry) VALUES (?, ?, ?, ?)",
            (placeholder_id, sub_id, password, expiry.isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def db_login(sub_id: str, password: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT user_id, sub_id, password, username, expiry, is_admin FROM users WHERE sub_id=? AND password=?",
        (sub_id, password)
    )
    row = c.fetchone()
    conn.close()
    if row:
        return {"user_id": row[0], "sub_id": row[1], "password": row[2], "username": row[3], "expiry": row[4], "is_admin": row[5]}
    return None


def db_link_telegram(sub_id: str, telegram_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("UPDATE users SET user_id=? WHERE sub_id=? AND (user_id<0 OR user_id=0)", (telegram_id, sub_id))
        conn.commit()
        return c.rowcount > 0
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def db_fb_get(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, page_id, page_name, access_token FROM facebook_accounts WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"user_id": row[0], "page_id": row[1], "page_name": row[2], "access_token": row[3]}
    return None


def db_fb_save(user_id: int, page_id: str, page_name: str, access_token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO facebook_accounts (user_id, page_id, page_name, access_token) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET page_id=?, page_name=?, access_token=?",
        (user_id, page_id, page_name, access_token, page_id, page_name, access_token)
    )
    conn.commit()
    conn.close()


def db_fb_delete(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM facebook_accounts WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def db_all_fb() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, page_id, page_name FROM facebook_accounts ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "page_id": r[1], "page_name": r[2]} for r in rows]


def db_social_all() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, file_path, caption, platform, status, error, created_at FROM social_posts ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "user_id": r[1], "file_path": r[2], "caption": r[3], "platform": r[4], "status": r[5], "error": r[6], "created_at": r[7]} for r in rows]


def db_social_log(user_id: int, file_path: str, caption: str, platform: str, status: str, error: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO social_posts (user_id, file_path, caption, platform, status, error) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, file_path, caption, platform, status, error)
    )
    conn.commit()
    conn.close()


def db_schedule_insert(user_id: int, file_path: str, caption: str, schedule_time: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO scheduled_posts (user_id, file_path, caption, schedule_time) VALUES (?, ?, ?, ?)",
        (user_id, file_path, caption, schedule_time),
    )
    conn.commit()
    conn.close()


def db_schedule_pending() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, file_path, caption, schedule_time FROM scheduled_posts WHERE status='pending' AND schedule_time <= datetime('now')"
    )
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "user_id": r[1], "file_path": r[2], "caption": r[3], "schedule_time": r[4]} for r in rows]


def db_schedule_mark(post_id: int, status: str = "sent"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE scheduled_posts SET status=? WHERE id=?", (status, post_id))
    conn.commit()
    conn.close()


def is_subscribed(user_id: int) -> bool:
    u = db_user_get(user_id)
    if not u or not u["expiry"]:
        return False
    if u["is_admin"]:
        return True
    exp = datetime.date.fromisoformat(u["expiry"])
    return exp >= datetime.date.today()


def days_left(user_id: int) -> int:
    u = db_user_get(user_id)
    if not u or not u["expiry"]:
        return 0
    if u["is_admin"]:
        return 999
    delta = datetime.date.fromisoformat(u["expiry"]) - datetime.date.today()
    return max(0, delta.days)


# ── Helpers ──

def extract_url(text: str) -> str | None:
    m = URL_PATTERN.search(text)
    return m.group(0) if m else None


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _processing_locks:
        _processing_locks[chat_id] = asyncio.Lock()
    return _processing_locks[chat_id]


def get_cancel(chat_id: int) -> asyncio.Event:
    if chat_id not in _cancel_events:
        _cancel_events[chat_id] = asyncio.Event()
    return _cancel_events[chat_id]


def fmt_size(size: int | float) -> str:
    if size >= 1_000_000_000:
        return f"{size/1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size/1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size/1_000:.1f} KB"
    return f"{size} B"


def fmt_dur(sec: int) -> str:
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fyp_caption(text: str) -> str:
    import random
    return f"{text}\n\n{random.choice(FYP_TAGS)}"


async def edit(chat_id: int, msg_id: int, text: str, **kw):
    try:
        await _app.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, **kw)
    except Exception:
        pass


def format_time(dt: datetime.datetime) -> str:
    return dt.strftime("%d %b %Y, %H:%M") + " WIB"


# ── Subscription guard ──

async def need_sub(update: Update) -> bool:
    uid = update.effective_user.id
    if not is_subscribed(uid):
        await update.message.reply_text(
            f"❌ Akses ditolak!\n"
            f"Sudah punya akun? Ketik: /login <sub_id> <password>\n"
            f"Belum punya? Ketik /subscribe — Rp{PRICE:,}/bln via GoPay {GOPAY}"
        )
        return False
    return True


# ── Commands ──

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await need_sub(update):
        return
    await show_menu(update, context)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await need_sub(update):
        return
    await show_menu(update, context)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sisa = days_left(uid)
    status = f"✅ Sisa {sisa} hari" if sisa > 0 and sisa < 900 else "👑 Admin" if sisa >= 900 else "❌ Belum subscribe"

    msg = (
        f"━━━━━━━━━━━━━━━━\n"
        f"     🤖 *MENU BOT*     \n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"📌 *Status*: {status}\n\n"
        f"━━━ 📥 *DOWNLOAD* ━━━\n"
        f"Kirim link video (YT, TikTok, IG, FB, Twitter, dll)\n"
        f"→ Pilih format: Best Video / HD / SD / Audio MP3\n\n"
        f"━━━ ✂️ *CLIP* ━━━\n"
        f"`/clip <link>` — Auto-clip bagian viral 30-60dtk\n"
        f"  + judul overlay FYP\n\n"
        f"━━━ ⏰ *JADWAL* ━━━\n"
        f"Abis download, pilih ⏰ Jadwalkan\n"
        f"→ Masukin jam `HH:MM`, bot upload otomatis\n\n"
        f"━━━ 💰 *LANGGANAN* ━━━\n"
        f"`/subscribe` — Rp{PRICE:,}/bln\n"
        f"`/cek` — Cek sisa hari\n\n"
        f"━━━ 📘 *FACEBOOK* ━━━\n"
        f"`/fb` — Tautkan/hapus FB Page\n"
        f"Abis download, upload langsung ke FB!\n\n"
        f"━━━ 👤 *AKUN* ━━━\n"
        f"`/login <sub_id> <pass>` — Login akun\n"
        f"`/menu` — Tampilkan menu ini"
    )

    keyboard = [
        [
            InlineKeyboardButton("📥 Download", callback_data="m_dl"),
            InlineKeyboardButton("✂️ Clip", callback_data="m_clip"),
        ],
        [
            InlineKeyboardButton("🎵 MP3", callback_data="m_audio"),
            InlineKeyboardButton("⏰ Jadwal", callback_data="m_schedule"),
        ],
        [
            InlineKeyboardButton("🔑 Login", callback_data="m_login"),
            InlineKeyboardButton("📊 Status", callback_data="m_cek"),
        ],
        [
            InlineKeyboardButton("💰 Subscribe", callback_data="m_sub"),
        ],
    ]

    if update.effective_user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="m_admin")])
        reply_rows = [
            ["📥 Download", "✂️ Clip"],
            ["🎵 MP3", "⏰ Jadwal"],
            ["📘 Facebook", "📊 Status"],
            ["🔑 Login", "💰 Subscribe"],
            ["👑 Register", "👥 Users"],
            ["📢 Broadcast"],
        ]
    else:
        reply_rows = [
            ["📥 Download", "✂️ Clip"],
            ["🎵 MP3", "⏰ Jadwal"],
            ["📘 Facebook", "📊 Status"],
            ["🔑 Login", "💰 Subscribe"],
        ]

    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    reply_kb = ReplyKeyboardMarkup(reply_rows, resize_keyboard=True)
    await update.message.reply_text("👇 Pilih menu:", reply_markup=reply_kb)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = db_user_get(uid)
    if u and u["expiry"] and datetime.date.fromisoformat(u["expiry"]) >= datetime.date.today():
        await update.message.reply_text(f"✅ Kamu masih punya {days_left(uid)} hari masa aktif.")
        return
    msg = (
        f"💰 *Harga*: Rp{PRICE:,}/bulan (30 hari)\n\n"
        "📲 *Pembayaran*:\n"
        f"• GoPay: {GOPAY}\n"
        "• QRIS: (scan gambar di bawah)\n\n"
        "📤 *Setelah bayar, kirim foto bukti ke sini*\n"
        "Admin auto-proses, kamu langsung dapet sub_id & password!"
    )
    if os.path.exists(QRIS_PATH):
        with open(QRIS_PATH, "rb") as f:
            await update.message.reply_photo(photo=f, caption=msg)
    else:
        await update.message.reply_text(msg)


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Gunakan: /login <sub_id> <password>")
        return

    sub_id = context.args[0]
    password = context.args[1]
    uid = update.effective_user.id

    account = db_login(sub_id, password)
    if not account:
        await update.message.reply_text("❌ sub_id atau password salah.")
        return

    if account["user_id"] > 0 and account["user_id"] != uid:
        await update.message.reply_text("❌ Akun ini sudah dipakai orang lain.")
        return

    if not account["expiry"] or datetime.date.fromisoformat(account["expiry"]) < datetime.date.today():
        await update.message.reply_text("❌ Masa aktif akun sudah habis.")
        return

    if db_link_telegram(sub_id, uid):
        context.user_data["sub_id"] = sub_id
        await update.message.reply_text(
            f"✅ Login berhasil!\n"
            f"sub_id: `{sub_id}`\n"
            f"Sisa: {days_left(uid)} hari\n\n"
            "Ketik /menu untuk mulai."
        )
    else:
        await update.message.reply_text("❌ Gagal login. Coba lagi.")


async def cmd_cek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sisa = days_left(uid)
    if sisa >= 900:
        await update.message.reply_text("👑 Admin — akses penuh.")
    elif sisa > 0:
        await update.message.reply_text(f"✅ Subscribe aktif! Sisa {sisa} hari.")
    else:
        await update.message.reply_text("❌ Subscribe habis. Ketik /subscribe untuk perpanjang.")


# ── Admin commands ──

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    import random
    import string
    def rand_str(n):
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

    if len(context.args) == 0:
        await update.message.reply_text(
            "Gunakan:\n"
            "`/reg <telegram_id> [hari]` — Auto sub_id & password, kirim ke konsumen\n"
            "`/reg manual <sub_id> <pass> <hari>` — Manual"
        )
        return

    if context.args[0] == "manual" and len(context.args) >= 4:
        sub_id = context.args[1]
        password = context.args[2]
        try:
            days = int(context.args[3])
        except ValueError:
            await update.message.reply_text("Hari harus angka.")
            return
    else:
        try:
            customer_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Telegram ID harus angka.")
            return
        days = int(context.args[1]) if len(context.args) > 1 else 30
        sub_id = rand_str(6)
        password = rand_str(8)
        while not db_register_sub(sub_id, password, days):
            sub_id = rand_str(6)

    if db_register_sub(sub_id, password, days):
        msg = f"✅ Akun dibuat!\nsub_id: `{sub_id}`\npass: `{password}`\nhari: {days}"
        if context.args[0] != "manual":
            try:
                await context.bot.send_message(
                    chat_id=customer_id,
                    text=f"🎉 *Akun bot sudah aktif!*\n\n"
                         f"sub_id: `{sub_id}`\n"
                         f"password: `{password}`\n"
                         f"masa aktif: {days} hari\n\n"
                         f"Ketik: /login {sub_id} {password}\n"
                         f"lalu /menu untuk mulai."
                )
                msg += "\n\n✅ Juga sudah dikirim ke konsumen."
            except Exception:
                msg += "\n\n⚠️ Gagal kirim ke konsumen (ID salah/bot diblokir)."
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("❌ sub_id sudah dipakai. Coba lagi.")


async def cmd_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = db_all_users()
    if not users:
        await update.message.reply_text("Belum ada user.")
        return
    lines = ["📋 *Daftar User*:\n"]
    for u in users:
        sisa = days_left(u["user_id"])
        status = "✅" if sisa > 0 else "❌"
        tag = " 👑" if u["is_admin"] else ""
        sid = f"({u['sub_id']})" if u["sub_id"] else ""
        name = u["username"] or str(u["user_id"])
        lines.append(f"{status} {name} {sid} — {sisa} hari{tag}")
    await update.message.reply_text("\n".join(lines))


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Gunakan: /broadcast <pesan>")
        return
    users = db_all_users()
    sent = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["user_id"], text=f"📢 *Broadcast*:\n{msg}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast terkirim ke {sent} user.")


async def cmd_setcookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "📤 *Kirim file cookies.txt*\n\n"
        "Cara dapatin cookies:\n"
        "1. Install ekstensi 'Get cookies.txt' di Chrome\n"
        "2. Login ke YouTube di Chrome\n"
        "3. Klik ekstensi → Export cookies\n"
        "4. Kirim file cookies.txt ke sini"
    )


# ── Handle incoming text (URL or schedule time) ──

async def handle_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE, act: str):
    chat_id = update.effective_user.id
    msgs = {
        "dl": "📥 *Download Video*\n\nKirim link video (YouTube, TikTok, IG, FB, Twitter, dll)\nNanti pilih format: Best Video / HD / SD / Audio MP3\n\nLink otomatis terdeteksi.",
        "clip": "✂️ *Auto Clip*\n\nCara: `/clip <link YouTube>`\nBot akan download, cari bagian viral (heatmap),\npotong 30-60 detik, lalu tambah judul overlay.\n\nHasil: video siap FYP!",
        "audio": "🎵 *Audio MP3*\n\nKirim link video, pilih format 🎵 Audio MP3\nBot akan extract audio jadi MP3 192kbps.",
        "schedule": "⏰ *Jadwal Upload*\n\n1. Kirim link video\n2. Pilih format download\n3. Abis download, klik ⏰ Jadwalkan\n4. Masukin jam `HH:MM`\n5. Bot upload otomatis di jam itu!",
        "login": f"🔑 *Login Akun*\n\nCara: `/login <sub_id> <password>`\n\nBelum punya akun?\nKetik /subscribe — Rp{PRICE:,}/bln via GoPay {GOPAY}",
        "cek": "📊 *Status Akun*\n\n" + (
            "👑 Admin — akses penuh." if days_left(chat_id) >= 900
            else f"✅ Subscribe aktif! Sisa {days_left(chat_id)} hari." if days_left(chat_id) > 0
            else "❌ Subscribe habis."
        ),
        "sub": (
            f"💰 Kamu masih punya {days_left(chat_id)} hari masa aktif."
            if days_left(chat_id) > 0 and days_left(chat_id) < 900
            else f"💰 *Langganan*\n\nHarga: Rp{PRICE:,}/bulan\n\nGoPay: {GOPAY}\nQRIS: (scan gambar)\n\n📤 Kirim foto bukti transfer ke bot ini.\nAdmin auto-proses, kamu langsung dapet akun!"
        ),
        "fb": "📘 *Facebook*\n\n`/fb` — Lihat/link/hapus akun FB\n`/fb <page_id> <token>` — Tautkan FB Page\n`/fb unlink` — Hapus tautan\n\nAbis download, klik 📘 Upload ke FB langsung upload video ke Page kamu.",
        "register": "👑 *Register User*\n\n`/reg <telegram_id> [hari]` — Auto sub_id & pass, kirim ke konsumen\n`/reg manual <sub_id> <pass> <hari>` — Manual\n\nAtau konsumen kirim foto bukti, tinggal tap ✅ Terima.",
        "list": "👥 *Daftar User*\n\nKetik `/list_users` untuk lihat semua user.",
        "broadcast": "📢 *Broadcast*\n\nKetik `/broadcast <pesan>` untuk kirim ke semua user.",
    }
    if act == "admin" and chat_id == ADMIN_ID:
        await update.message.reply_text(
            "👑 *Admin Panel*\n\n"
            "`/reg <telegram_id> [hari]` — Auto sub_id & pass, kirim ke konsumen\n"
            "`/list_users` — Daftar semua user\n"
            "`/broadcast <pesan>` — Kirim pesan ke semua user"
        )
        return
    if act in ("register", "list", "broadcast", "admin") and chat_id != ADMIN_ID:
        await update.message.reply_text("❌ Hanya admin yang bisa akses ini.")
        return
    if act in msgs:
        await update.message.reply_text(msgs[act])


# ── Payment proof handler ──

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    uid = update.effective_user.id
    name = update.effective_user.full_name or str(uid)
    username = update.effective_user.username or "-"
    photo = update.message.photo[-1]

    caption = (
        f"💳 *Pembayaran baru!*\n\n"
        f"User: {name}\n"
        f"ID: `{uid}`\n"
        f"Username: @{username}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Terima", callback_data=f"pay_ok|{uid}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"pay_no|{uid}"),
        ],
    ])
    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo.file_id,
        caption=caption,
        reply_markup=keyboard,
    )
    await update.message.reply_text("✅ Bukti diterima! Admin akan proses dalam beberapa saat.")


# ── Handle incoming text (URL or schedule time) ──

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Handle reply keyboard menu
    menu_map = {
        "📥 Download": "m_dl",
        "✂️ Clip": "m_clip",
        "🎵 MP3": "m_audio",
        "⏰ Jadwal": "m_schedule",
        "🔑 Login": "m_login",
        "📊 Status": "m_cek",
        "💰 Subscribe": "m_sub",
        "📘 Facebook": "m_fb",
        "👑 Register": "m_register",
        "👥 Users": "m_list",
        "📢 Broadcast": "m_broadcast",
    }
    if text in menu_map:
        await handle_menu_action(update, context, menu_map[text])
        return

    # If waiting for schedule time
    if context.user_data.get("awaiting_schedule"):
        # If user sends a URL instead, cancel schedule mode
        if extract_url(text):
            context.user_data["awaiting_schedule"] = False
            await handle_url(update, context)
            return

        if re.match(r"^\d{1,2}:\d{2}$", text):
            await schedule_file(update, context, text)
        else:
            await update.message.reply_text("Format salah. Kirim jam *HH:MM* (contoh: `14:30`)")
        return

    # Normal: try URL, if no URL found, show menu
    if not extract_url(text):
        await show_menu(update, context)
        return
    await handle_url(update, context)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await need_sub(update):
        return

    chat_id = update.effective_chat.id
    now = time.time()
    last = _rate_limit.get(chat_id, 0)
    if now - last < RATE_LIMIT_SEC:
        await update.message.reply_text(f"⏳ Tunggu {RATE_LIMIT_SEC - (now - last):.0f} detik sebelum download lagi.")
        return
    _rate_limit[chat_id] = now
    url = extract_url(update.message.text)
    if not url:
        await update.message.reply_text("Tidak ada link yang ditemukan.")
        return

    if get_lock(chat_id).locked():
        await update.message.reply_text("Tunggu download sebelumnya selesai.")
        return

    # Clear any stale schedule state
    context.user_data.pop("awaiting_schedule", None)

    msg = await update.message.reply_text("Mengambil info video...")

    try:
        info = await extract_info(url)

        if info.get("_type") == "playlist":
            await show_playlist_menu(update, msg, info, url)
            return

        title = info.get("title", "Unknown")
        dur = info.get("duration", 0)
        views = info.get("view_count")
        uploader = info.get("uploader", "")

        text = f"📹 *{title}*"
        if uploader:
            text += f"\n👤 {uploader}"
        text += f"\n⏱ {fmt_dur(dur)}"
        if views:
            text += f"\n👁 {views:,} views"
        text += "\n\nPilih format:"

        global _url_counter
        _url_counter += 1
        ref = str(_url_counter)
        _url_store[ref] = (url, time.time())

        keyboard = [
            [
                InlineKeyboardButton("🎬 Best Video", callback_data=f"f_best|{ref}"),
                InlineKeyboardButton("🎵 Audio MP3", callback_data=f"f_audio|{ref}"),
            ],
            [
                InlineKeyboardButton("🎬 HD (1080p)", callback_data=f"f_hd|{ref}"),
                InlineKeyboardButton("🎬 SD (480p)", callback_data=f"f_sd|{ref}"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="f_cancel")],
        ]

        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        err = str(e)
        await msg.edit_text(f"Gagal ambil info: {err[:200]}")


async def extract_info(url: str) -> dict:
    def _run():
        opts = {"quiet": True, "no_warnings": True, "ffmpeg_location": FFMPEG_PATH, "extractor_args": {"youtube": {"player_client": ["android_embedded", "android", "ios"]}}, "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        if os.path.exists(COOKIES_PATH):
            opts["cookiefile"] = COOKIES_PATH
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await asyncio.to_thread(_run)


# ── Playlist ──

async def show_playlist_menu(update: Update, msg, info: dict, url: str):
    global _url_counter
    _url_counter += 1
    ref = str(_url_counter)
    _url_store[ref] = (url, time.time())
    entries = info.get("entries", [])
    if not entries:
        await msg.edit_text("Playlist kosong.")
        return
    text = f"📋 *Playlist*: {info.get('title', 'Untitled')}\n{len(entries)} video\n\nPilih opsi:"
    keyboard = [
        [InlineKeyboardButton("⬇️ Download Semua", callback_data=f"pl_all|{ref}")],
        [InlineKeyboardButton("⬇️ 5 Teratas", callback_data=f"pl_top5|{ref}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="pl_cancel")],
    ]
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ── Callback handler ──

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    data = query.data

    if data.startswith("f_"):
        parts = data[2:].split("|", 1)
        fmt = parts[0]
        if fmt == "cancel":
            await query.edit_message_text("✅ Dibatalkan.")
            return
        ref = parts[1]
        entry = _url_store.get(ref)
        if not entry or time.time() - entry[1] > URL_STORE_TTL:
            _url_store.pop(ref, None)
            await query.edit_message_text("❌ Link kadaluarsa, kirim ulang linknya.")
            return
        url = entry[0]
        await query.edit_message_text("Memproses...")
        async with get_lock(chat_id):
            await download(chat_id, query.message.id, url, fmt, context)

    elif data.startswith("pl_"):
        raw = data[3:]
        if raw == "cancel":
            await query.edit_message_text("✅ Dibatalkan.")
            return
        act, ref = raw.split("|", 1)
        entry = _url_store.get(ref)
        if not entry or time.time() - entry[1] > URL_STORE_TTL:
            _url_store.pop(ref, None)
            await query.edit_message_text("❌ Link kadaluarsa, kirim ulang.")
            return
        url = entry[0]
        msg = await query.edit_message_text("Mengambil daftar playlist...")
        info = await extract_info(url)
        entries = info.get("entries", [])
        limit = len(entries) if act == "all" else min(5, len(entries))

        await msg.edit_text(f"Mendownload {limit} video...\n0/{limit}")

        for i, entry in enumerate(entries[:limit], 1):
            vurl = entry.get("webpage_url") or entry.get("url")
            if not vurl:
                continue
            if get_cancel(chat_id).is_set():
                await msg.edit_text(f"❌ Dibatalkan setelah {i-1} video.")
                get_cancel(chat_id).clear()
                return
            await msg.edit_text(f"Video {i}/{limit}: {entry.get('title', 'Unknown')[:50]}...")
            await download(chat_id, msg.id, vurl, "best", context)
        await msg.edit_text(f"✅ Selesai! {limit} video.")

    elif data.startswith("dlc_"):
        cid = int(data.split("_")[1])
        get_cancel(cid).set()
        await query.edit_message_text("⏹️ Membatalkan...")

    elif data.startswith("sch_"):
        act = data.split("_", 1)[1]
        if act == "now":
            uid_ctx = context.user_data.get("dl_uid")
            fmt = context.user_data.get("dl_fmt")
            title = context.user_data.get("dl_title", "Video")
            fp = find_file(uid_ctx, fmt) if uid_ctx else None
            if fp and os.path.exists(fp):
                await query.edit_message_text("📤 Mengupload...")
                await send_file(query.message.chat.id, fp, fmt, title, context)
                cleanup_uid(uid_ctx)
                context.user_data.pop("dl_uid", None)
                context.user_data.pop("dl_fmt", None)
                context.user_data.pop("dl_title", None)
                await edit(query.message.chat.id, query.message.id, "✅ Selesai!")
            else:
                await query.edit_message_text("❌ File sudah tidak ada.")
        elif act == "schedule":
            context.user_data["awaiting_schedule"] = True
            await query.edit_message_text(
                "⏰ Kirim jam jadwal dalam format *HH:MM* (24 jam)\n"
                "Contoh: `14:30` = jam 2:30 siang.",
            )
        elif act == "fb":
            uid_ctx = context.user_data.get("dl_uid")
            fmt = context.user_data.get("dl_fmt")
            title = context.user_data.get("dl_title", "Video")
            fp = find_file(uid_ctx, fmt) if uid_ctx else None
            if not fp or not os.path.exists(fp):
                await query.edit_message_text("❌ File sudah tidak ada.")
            else:
                await query.edit_message_text("📘 Mengupload ke Facebook...")
                result = await fb_upload_video(chat_id, fp, fyp_caption(title))
                cleanup_uid(uid_ctx)
                context.user_data.pop("dl_uid", None)
                context.user_data.pop("dl_fmt", None)
                context.user_data.pop("dl_title", None)
                await edit(query.message.chat.id, query.message.id, result)

    elif data.startswith("pay_"):
        act, cid = data.split("|", 1)
        cid = int(cid)
        act = act.split("_")[1]
        await query.edit_message_reply_markup(reply_markup=None)
        if act == "ok":
            import random
            import string
            def rand_str(n):
                return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))
            sub_id = rand_str(6)
            password = rand_str(8)
            while not db_register_sub(sub_id, password, 30):
                sub_id = rand_str(6)
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n✅ *DITERIMA*\nsub_id: `{sub_id}`\npass: `{password}`\n30 hari"
            )
            try:
                await context.bot.send_message(
                    chat_id=cid,
                    text=f"🎉 *Pembayaran diterima! Akun kamu aktif!*\n\n"
                         f"sub_id: `{sub_id}`\n"
                         f"password: `{password}`\n"
                         f"masa aktif: 30 hari\n\n"
                         f"Ketik: `/login {sub_id} {password}`\n"
                         f"lalu /menu untuk mulai."
                )
            except Exception:
                await query.message.reply_text(f"⚠️ Gagal kirim ke user {cid}")
        elif act == "no":
            await query.edit_message_caption(caption=query.message.caption + "\n\n❌ *DITOLAK*")
            try:
                await context.bot.send_message(
                    chat_id=cid,
                    text="❌ Maaf, pembayaran kamu ditolak. Hubungi @kucingaarr untuk info lebih lanjut."
                )
            except Exception:
                pass

    elif data.startswith("m_"):
        act = data.split("_", 1)[1]
        if act == "dl":
            await query.edit_message_text(
                "📥 *Download Video*\n\n"
                "Kirim link video (YouTube, TikTok, IG, FB, Twitter, dll)\n"
                "Nanti pilih format: Best Video / HD / SD / Audio MP3\n\n"
                "Link otomatis terdeteksi."
            )
        elif act == "clip":
            await query.edit_message_text(
                "✂️ *Auto Clip*\n\n"
                "Cara: `/clip <link YouTube>`\n"
                "Bot akan download, cari bagian viral (heatmap),\n"
                "potong 30-60 detik, lalu tambah judul overlay.\n\n"
                "Hasil: video siap FYP!"
            )
        elif act == "audio":
            await query.edit_message_text(
                "🎵 *Audio MP3*\n\n"
                "Kirim link video, pilih format 🎵 Audio MP3\n"
                "Bot akan extract audio jadi MP3 192kbps."
            )
        elif act == "schedule":
            await query.edit_message_text(
                "⏰ *Jadwal Upload*\n\n"
                "1. Kirim link video\n"
                "2. Pilih format download\n"
                "3. Abis download, klik ⏰ Jadwalkan\n"
                "4. Masukin jam `HH:MM`\n"
                "5. Bot upload otomatis di jam itu!"
            )
        elif act == "login":
            await query.edit_message_text(
                "🔑 *Login Akun*\n\n"
                "Cara: `/login <sub_id> <password>`\n\n"
                "Belum punya akun?\n"
                "Ketik /subscribe — Rp{PRICE:,}/bln\n"
                f"via GoPay {GOPAY}"
            )
        elif act == "sub":
            sisa = days_left(chat_id)
            if sisa > 0 and sisa < 900:
                await query.edit_message_text(f"💰 Kamu masih punya {sisa} hari masa aktif.")
            else:
                await query.edit_message_text(
                    f"💰 *Langganan*\n\n"
                    f"Harga: Rp{PRICE:,}/bulan\n\n"
                    f"GoPay: {GOPAY}\nQRIS: (scan gambar)\n\n"
                    "📤 Kirim foto bukti transfer ke bot ini.\nAdmin auto-proses, langsung dapet akun.\n\n"
                    "Ketik /login setelah punya akun."
                )
        elif act == "cek":
            sisa = days_left(chat_id)
            if sisa >= 900:
                text = "👑 Admin — akses penuh."
            elif sisa > 0:
                text = f"✅ Subscribe aktif! Sisa {sisa} hari."
            else:
                text = "❌ Subscribe habis."
            await query.edit_message_text(f"📊 *Status Akun*\n\n{text}")
        elif act == "admin":
            if chat_id != ADMIN_ID:
                return
            await query.edit_message_text(
                "👑 *Admin Panel*\n\n"
                "`/reg <telegram_id> [hari]` — Auto sub_id & pass, kirim ke konsumen\n"
                "`/list_users` — Daftar semua user\n"
                "`/broadcast <pesan>` — Kirim pesan ke semua user"
            )


# ── Schedule file ──

async def schedule_file(update: Update, context: ContextTypes.DEFAULT_TYPE, time_str: str):
    uid_ctx = context.user_data.get("dl_uid")
    fmt = context.user_data.get("dl_fmt")
    title = context.user_data.get("dl_title", "Video")
    fp = find_file(uid_ctx, fmt) if uid_ctx else None

    if not fp or not os.path.exists(fp):
        await update.message.reply_text("❌ File sudah tidak ada. Download ulang.")
        context.user_data["awaiting_schedule"] = False
        return

    new_name = f"scheduled_{uid_ctx}_{Path(fp).name}"
    new_path = str(SCHEDULE_DIR / new_name)
    os.rename(fp, new_path)

    now = datetime.datetime.now()
    h, m = map(int, time_str.split(":"))
    sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if sched <= now:
        sched += datetime.timedelta(days=1)

    cap = fyp_caption(title)
    db_schedule_insert(update.effective_user.id, new_path, cap, sched.isoformat())

    context.user_data.pop("dl_uid", None)
    context.user_data.pop("dl_fmt", None)
    context.user_data.pop("dl_title", None)
    context.user_data["awaiting_schedule"] = False

    await update.message.reply_text(f"✅ Terjadwal!\nVideo akan diupload {format_time(sched)}.")


# ── Download ──

async def download(chat_id: int, msg_id: int, url: str, fmt: str, context: ContextTypes.DEFAULT_TYPE):
    uid = str(uuid.uuid4())[:8]
    cancel = get_cancel(chat_id)
    cancel.clear()

    fmt_map = {
        "best": "best[filesize<50M]/best",
        "hd": "bestvideo[height<=1080][filesize<50M]+bestaudio/best[height<=1080][filesize<50M]/best",
        "sd": "best[height<=480][filesize<50M]/best",
        "audio": "bestaudio/best",
    }

    opts = {
        "format": fmt_map.get(fmt, "best"),
        "outtmpl": str(DOWNLOAD_DIR / f"{uid}_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
        "max_filesize": MAX_FILE_SIZE,
        "progress_hooks": [_make_hook(chat_id, msg_id)],
        "extractor_args": {"youtube": {"player_client": ["android_embedded", "android", "ios"]}},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    if os.path.exists(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH

    if fmt == "audio":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Download", callback_data=f"dlc_{chat_id}")]
    ])

    try:
        await edit(chat_id, msg_id, "📥 Mendownload...", reply_markup=cancel_kb)

        def _dl():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.to_thread(_dl)

        if cancel.is_set():
            return

        filepath = find_file(uid, fmt)
        if not filepath:
            await edit(chat_id, msg_id, "❌ File tak ditemukan.")
            return

        size = os.path.getsize(filepath)
        if size > MAX_FILE_SIZE:
            os.remove(filepath)
            await edit(chat_id, msg_id, "❌ Terlalu besar (max 50MB).")
            return

        title = (info.get("title") or "Video")[:200]

        context.user_data["dl_uid"] = uid
        context.user_data["dl_fmt"] = fmt
        context.user_data["dl_title"] = title

        sch_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📤 Kirim Sekarang", callback_data="sch_now"),
                InlineKeyboardButton("⏰ Jadwalkan", callback_data="sch_schedule"),
            ],
            [
                InlineKeyboardButton("📘 Upload ke FB", callback_data="sch_fb"),
            ],
        ])

        await edit(chat_id, msg_id, f"✅ Download selesai!\n*{title}*\n\n📤 Kirim atau upload?", reply_markup=sch_kb)

    except Exception as e:
        err = str(e)
        if "Cancelled" in err or cancel.is_set():
            await edit(chat_id, msg_id, "❌ Dibatalkan.")
        else:
            await edit(chat_id, msg_id, f"❌ Gagal: {err[:200]}")
        cleanup_uid(uid)
        get_cancel(chat_id).clear()
        for k in ["dl_uid", "dl_fmt", "dl_title"]:
            context.user_data.pop(k, None)


async def send_file(chat_id: int, filepath: str, fmt: str, title: str, context: ContextTypes.DEFAULT_TYPE):
    cap = fyp_caption(title)
    if fmt == "audio":
        with open(filepath, "rb") as f:
            await context.bot.send_audio(
                chat_id=chat_id, audio=f, title=title, caption=cap,
            )
    else:
        with open(filepath, "rb") as f:
            await context.bot.send_video(
                chat_id=chat_id, video=f, caption=cap, supports_streaming=True,
            )


def _make_hook(chat_id: int, msg_id: int):
    last = [0.0]
    def hook(d):
        if chat_id in _cancel_events and _cancel_events[chat_id].is_set():
            raise Exception("Cancelled by user")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            dl = d.get("downloaded_bytes", 0)
            speed = d.get("speed", 0)
            eta = d.get("eta", 0)
            if total > 0:
                pct = dl / total * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                text = f"📥 Downloading...\n{bar} {pct:.0f}%\n{fmt_size(dl)} / {fmt_size(total)}"
                if speed:
                    text += f"\n⚡ {fmt_size(speed)}/s"
                if eta:
                    text += f" ⏱ {int(eta)}s"
                now = time.time()
                if now - last[0] < 2 and pct < 99:
                    return
                last[0] = now
                asyncio.run_coroutine_threadsafe(edit(chat_id, msg_id, text), _loop)
    return hook


def find_file(uid: str, fmt: str) -> str | None:
    if fmt == "audio":
        for f in DOWNLOAD_DIR.iterdir():
            if f.name.startswith(uid) and f.suffix == ".mp3":
                return str(f)
    for f in DOWNLOAD_DIR.iterdir():
        if f.name.startswith(uid):
            return str(f)
    return None


def cleanup_uid(uid: str):
    for f in DOWNLOAD_DIR.iterdir():
        if f.name.startswith(uid):
            f.unlink(missing_ok=True)


# ── Facebook upload ──

FB_API = "https://graph.facebook.com/v22.0"


async def fb_upload_video(user_id: int, file_path: str, caption: str) -> str:
    account = db_fb_get(user_id)
    if not account:
        return "❌ Akun Facebook belum ditautkan. Ketik /fb"
    try:
        def _upload():
            with open(file_path, "rb") as f:
                r = requests.post(
                    f"{FB_API}/{account['page_id']}/videos",
                    params={"access_token": account["access_token"], "description": caption},
                    files={"source": f},
                    timeout=300,
                )
            return r.json()
        result = await asyncio.to_thread(_upload)
        if "id" in result:
            db_social_log(user_id, file_path, caption, "facebook", "success")
            return f"✅ Upload berhasil!\nVideo ID: {result['id']}"
        err = result.get("error", {}).get("message", str(result))
        db_social_log(user_id, file_path, caption, "facebook", "error", err)
        return f"❌ Gagal upload: {err[:200]}"
    except Exception as e:
        db_social_log(user_id, file_path, caption, "facebook", "error", str(e))
        return f"❌ Error: {str(e)[:200]}"


async def cmd_fb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await need_sub(update):
        return
    uid = update.effective_user.id
    if not context.args:
        acc = db_fb_get(uid)
        if acc:
            await update.message.reply_text(
                f"🔗 *Facebook tertaut*\nPage: {acc['page_name']} (ID: {acc['page_id']})\n\n"
                "`/fb unlink` — Hapus tautan\n"
                "`/fb <page_id> <token>` — Ganti akun\n\n"
                "Cara dapatin token: buka developers.facebook.com"
            )
        else:
            await update.message.reply_text(
                "🔑 *Tautkan Facebook*\n\n"
                "Gunakan:\n"
                "`/fb <page_id> <access_token>`\n\n"
                "Cara dapatin token:\n"
                "1. Buka developers.facebook.com\n"
                "2. Buat app → dapatkan Page Access Token\n"
                "3. Kirim ke bot: `/fb 123456789 token_nya`"
            )
        return
    if context.args[0] == "unlink":
        db_fb_delete(uid)
        await update.message.reply_text("❌ Akun Facebook dihapus.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Gunakan: `/fb <page_id> <access_token>`")
        return
    page_id = context.args[0]
    token = context.args[1]
    try:
        def _verify():
            r = requests.get(f"{FB_API}/{page_id}", params={"access_token": token}, timeout=15)
            return r.json()
        info = await asyncio.to_thread(_verify)
        name = info.get("name", page_id)
        if "error" in info:
            await update.message.reply_text(f"❌ Token/ID salah: {info['error']['message'][:200]}")
            return
        db_fb_save(uid, page_id, name, token)
        await update.message.reply_text(f"✅ Facebook tertaut!\nPage: {name}")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal: {str(e)[:200]}")


# ── Clip with title overlay ──

async def clip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await need_sub(update):
        return
    if not update.message or not context.args:
        await update.message.reply_text("Gunakan: /clip <link YouTube>")
        return

    url = extract_url(" ".join(context.args))
    if not url:
        await update.message.reply_text("Link tidak valid.")
        return

    msg = await update.message.reply_text("Mendownload & menganalisis...")

    # cek ffmpeg
    try:
        subprocess.run([FFMPEG_PATH, "-version"], capture_output=True, timeout=10)
    except Exception:
        await msg.edit_text("❌ ffmpeg gak tersedia di server. Hubungi admin.")
        return

    try:
        filepath, info = await download_video_with_info(url)
        if filepath is None:
            await msg.edit_text("Video terlalu besar (max 50MB).")
            return

        await msg.edit_text("Membuat klip dengan judul...")
        try:
            clip_paths = await create_clips(filepath, info)
        except Exception as clip_err:
            await msg.edit_text(f"Gagal bikin klip: {str(clip_err)[:150]}")
            os.remove(filepath)
            return

        if not clip_paths:
            await msg.edit_text("Gagal membuat klip: durasi terlalu pendek atau error ffmpeg.")
            os.remove(filepath)
            return

        await msg.edit_text(f"Mengupload {len(clip_paths)} klip...")
        for cp in clip_paths:
            try:
                title = info.get("title", "Video")[:100]
                with open(cp, "rb") as f:
                    await update.message.reply_video(f, caption=fyp_caption(title))
            except Exception:
                pass
            finally:
                os.remove(cp)

        os.remove(filepath)
        await msg.delete()

    except Exception as e:
        await msg.edit_text(f"Gagal: {str(e)[:200]}")


async def download_video_with_info(url: str) -> tuple[str | None, dict | None]:
    uid = str(uuid.uuid4())[:8]
    opts = {
        "format": "best[filesize<50M]/best",
        "outtmpl": str(DOWNLOAD_DIR / f"{uid}_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
        "extractor_args": {"youtube": {"player_client": ["android_embedded", "android", "ios"]}},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    def _dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    info = await asyncio.to_thread(_dl)
    fp = find_file(uid, "video")
    return (fp, info) if fp else (None, None)


async def create_clips(video_path: str, info: dict) -> list[str]:
    duration = info.get("duration") or 0
    if duration < 30:
        return []

    heatmap = info.get("heatmap") or []
    if heatmap:
        segments = sorted(heatmap, key=lambda s: s["value"], reverse=True)
    else:
        segments = []
        count = min(4, max(1, duration // 60))
        for i in range(count):
            t = (duration / (count + 1)) * (i + 1)
            segments.append({"start_time": t, "end_time": t + 1, "value": 0.5})

    title = info.get("title", "Video")[:80]
    safe_title = re.sub(r'[^\w\s\-.,!?]', '', title).strip()[:60]
    clip_paths = []
    seen_ranges = []

    for seg in segments[:4]:
        peak = (seg["start_time"] + seg["end_time"]) / 2
        clip_dur = min(max(30, seg["end_time"] - seg["start_time"] + 10), 60)
        start = max(0, peak - clip_dur / 2)
        end = min(duration, start + clip_dur)

        if end - start < 15:
            continue
        if any(s < end and e > start for s, e in seen_ranges):
            continue

        end = min(duration, start + clip_dur)
        if start + clip_dur > duration:
            start = max(0, duration - clip_dur)
        seen_ranges.append((start, end))

        clip_id = str(uuid.uuid4())[:8]
        ext = Path(video_path).suffix or ".mp4"
        clip_path = str(CLIPS_DIR / f"clip_{clip_id}{ext}")
        clip_no_overlay = str(CLIPS_DIR / f"clip_{clip_id}_no{ext}")

        try:
            result = subprocess.run(
                [FFMPEG_PATH, "-y", "-ss", str(int(start)), "-i", video_path,
                 "-t", str(int(end - start)),
                 "-c:v", "libx264", "-preset", "fast",
                 "-c:a", "aac", "-b:a", "128k", clip_no_overlay],
                capture_output=True, timeout=180,
            )
            if result.returncode != 0:
                continue
            if not os.path.exists(clip_no_overlay) or os.path.getsize(clip_no_overlay) == 0:
                continue

            if safe_title and os.path.exists(FONT_PATH):
                vf = (
                    f"drawtext=fontfile={FONT_PATH}:"
                    f"text={safe_title}:"
                    f"fontsize=24:fontcolor=white:box=1:boxcolor=black@0.5:"
                    f"boxborderw=8:x=(w-text_w)/2:y=h-th-30"
                )
                subprocess.run(
                    [FFMPEG_PATH, "-y", "-i", clip_no_overlay, "-vf", vf,
                     "-c:a", "copy", clip_path],
                    capture_output=True, timeout=60,
                )
                if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
                    clip_paths.append(clip_path)
                    os.remove(clip_no_overlay)
                else:
                    clip_paths.append(clip_no_overlay)
            else:
                clip_paths.append(clip_no_overlay)

            if len(clip_paths) >= 2:
                break
        except Exception:
            continue

    return clip_paths


# ── Scheduler background ──

async def cleanup_url_store():
    now = time.time()
    expired = [k for k, v in _url_store.items() if now - v[1] > URL_STORE_TTL]
    for k in expired:
        _url_store.pop(k, None)


async def scheduler_loop(app: Application):
    while True:
        try:
            await cleanup_url_store()
            for p in db_schedule_pending():
                if not os.path.exists(p["file_path"]):
                    db_schedule_mark(p["id"], "sent")
                    continue
                try:
                    with open(p["file_path"], "rb") as f:
                        await app.bot.send_video(
                            chat_id=p["user_id"],
                            video=f,
                            caption=p["caption"],
                            supports_streaming=True,
                        )
                except Exception:
                    pass
                try:
                    os.remove(p["file_path"])
                except Exception:
                    pass
                db_schedule_mark(p["id"], "sent")
                await asyncio.sleep(1)
        except Exception:
            pass
        await asyncio.sleep(30)


# ── Cookies file handler ──

async def handle_cookies_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    doc = update.message.document
    if not doc.file_name or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Kirim file .txt")
        return
    file = await doc.get_file()
    await file.download_to_drive(COOKIES_PATH)
    await update.message.reply_text("✅ Cookies tersimpan! YouTube sekarang bisa dipake.")


# ── Main ──

async def main_async():
    global _app, _loop
    _loop = asyncio.get_running_loop()
    db_init()
    db_user_upsert(ADMIN_ID, "admin", 9999)
    db_user_set_admin(ADMIN_ID)

    app = Application.builder().token(TOKEN).build()
    _app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("cek", cmd_cek))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("reg", cmd_register))
    app.add_handler(CommandHandler("list_users", cmd_list_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("clip", clip_command))
    app.add_handler(CommandHandler("fb", cmd_fb))
    app.add_handler(CommandHandler("setcookies", cmd_setcookies))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_cookies_file))

    async with app:
        await app.start()
        await app.updater.start_polling()
        app.create_task(scheduler_loop(app))
        try:
            await app.bot.send_message(chat_id=ADMIN_ID, text="✅ *Bot Online!*\nBot udah hidup 24 jam di Railway.")
        except Exception:
            pass
        print("Bot berjalan dengan fitur baru...")
        while True:
            await asyncio.sleep(3600)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
