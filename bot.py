"""Uzum Analitika — Telegram bot."""
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app import fetch_product, store_snapshot

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "8863044902:AAFh6Vc3SdKkpZu781n_OvX_19qegDXRoxM")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")

_DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
_DATA_DIR.mkdir(parents=True, exist_ok=True)

BOT_SETTINGS_FILE = _DATA_DIR / "bot_settings.json"
AUTH_FILE         = _DATA_DIR / "authorized_users.json"
ADMIN_CHAT_ID_ENV = int(os.environ.get("ADMIN_CHAT_ID", 0))

# Login state machine: {chat_id: 'awaiting_phone' | 'awaiting_otp' | 'processing'}
_login_state: dict = {}


# ─── WebApp URL ───────────────────────────────────────────────────────────────

def get_webapp_url():
    if WEBAPP_URL:
        return WEBAPP_URL
    if BOT_SETTINGS_FILE.exists():
        try:
            return json.loads(BOT_SETTINGS_FILE.read_text()).get("webapp_url", "")
        except Exception:
            pass
    return ""


def save_webapp_url(url):
    cfg = {}
    if BOT_SETTINGS_FILE.exists():
        try:
            cfg = json.loads(BOT_SETTINGS_FILE.read_text())
        except Exception:
            pass
    cfg["webapp_url"] = url
    BOT_SETTINGS_FILE.write_text(json.dumps(cfg, indent=2))


# ─── Auth ─────────────────────────────────────────────────────────────────────

def load_auth() -> dict:
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text())
        except Exception:
            pass
    return {"admin_chat_id": ADMIN_CHAT_ID_ENV or 0, "users": []}


def save_auth(data: dict):
    AUTH_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def is_authorized(chat_id: int) -> bool:
    data = load_auth()
    if data.get("admin_chat_id") == chat_id:
        return True
    return any(u["chat_id"] == chat_id for u in data.get("users", []))


def is_admin(chat_id: int) -> bool:
    return load_auth().get("admin_chat_id") == chat_id


def get_admin_chat_id() -> int:
    return load_auth().get("admin_chat_id") or 0


def add_user(chat_id: int, name: str, username: str = "") -> bool:
    """Yangi foydalanuvchi qo'shadi. Allaqachon bo'lsa False qaytaradi."""
    data = load_auth()
    users = data.setdefault("users", [])
    if any(u["chat_id"] == chat_id for u in users):
        return False
    users.append({
        "chat_id": chat_id,
        "name": name,
        "username": username,
        "role": "user",
        "added_at": datetime.utcnow().isoformat(),
    })
    save_auth(data)
    return True


def remove_user(chat_id: int) -> bool:
    data = load_auth()
    users = data.get("users", [])
    new_users = [u for u in users if u["chat_id"] != chat_id]
    if len(new_users) == len(users):
        return False
    data["users"] = new_users
    save_auth(data)
    return True


def check_auth(message) -> bool:
    """Autorizatsiyani tekshiradi. Ruxsat yo'q bo'lsa xabar yuboradi, False qaytaradi."""
    chat_id = message.chat.id
    if is_authorized(chat_id):
        return True
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📩 Kirish so'rash", callback_data=f"req_{chat_id}"))
    bot.reply_to(
        message,
        "🔒 *Ruxsat yo'q*\n\nBu bot faqat ruxsat etilgan foydalanuvchilar uchun.",
        parse_mode="Markdown",
        reply_markup=markup,
    )
    return False


# ─── Helpers ──────────────────────────────────────────────────────────────────

bot = telebot.TeleBot(BOT_TOKEN)


def fmt_num(n):
    if not n:
        return "—"
    return f"{int(n):,}".replace(",", " ")


def save_admin_chat_id(chat_id):
    cfg = {}
    if BOT_SETTINGS_FILE.exists():
        try:
            cfg = json.loads(BOT_SETTINGS_FILE.read_text())
        except Exception:
            pass
    cfg["admin_chat_id"] = chat_id
    BOT_SETTINGS_FILE.write_text(json.dumps(cfg, indent=2))


# ─── OTP (legacy auto-login) ──────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and re.match(r'^\d{6}$', m.text.strip()))
def handle_otp(message):
    """6 xonali OTP kodni qabul qilish (avtomatik refresh uchun)."""
    if not check_auth(message):
        return
    data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
    otp_file = data_dir / "pending_otp.json"
    if not otp_file.exists():
        return  # Kutilmayapti — handle_login_flow ushlaydi
    otp_code = message.text.strip()
    otp_data = {"otp": otp_code, "waiting": False, "timestamp": __import__('time').time()}
    otp_file.write_text(json.dumps(otp_data))
    bot.reply_to(message, "✅ OTP qabul qilindi! Login boshlanmoqda...")
    print(f"[bot] OTP olindi: {otp_code}")


# ─── /start ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "menu"])
def handle_start(message):
    chat_id = message.chat.id
    data    = load_auth()

    # Birinchi foydalanuvchi — admin bo'ladi
    if not data.get("admin_chat_id"):
        fname = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
        uname = message.from_user.username or ""
        data["admin_chat_id"] = chat_id
        if not any(u["chat_id"] == chat_id for u in data.get("users", [])):
            data.setdefault("users", []).append({
                "chat_id": chat_id,
                "name":    fname or f"User {chat_id}",
                "username": uname,
                "role":    "admin",
                "added_at": datetime.utcnow().isoformat(),
            })
        save_auth(data)
        save_admin_chat_id(chat_id)
        bot.send_message(
            chat_id,
            "👑 *Tabriklaymiz!*\n\n"
            "Siz birinchi foydalanuvchi sifatida *admin* bo'ldingiz.\n\n"
            "Boshqalarni qo'shish:\n"
            "`/adduser <chat_id> Ism`\n\n"
            "Foydalanuvchilarni ko'rish:\n"
            "`/users`",
            parse_mode="Markdown",
        )

    if not is_authorized(chat_id):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📩 Kirish so'rash", callback_data=f"req_{chat_id}"))
        bot.send_message(
            chat_id,
            "🔒 *Uzum Analitika*\n\n"
            "Bu bot yopiq — faqat ruxsat etilgan foydalanuvchilar uchun.\n\n"
            "Kirish uchun so'rov yuboring 👇",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    url = get_webapp_url()
    markup = InlineKeyboardMarkup()
    if url:
        markup.add(InlineKeyboardButton("📊 Analitika ochish", web_app=WebAppInfo(url=url)))

    admin_hint = "\n• `/users` — foydalanuvchilar ro'yxati" if is_admin(chat_id) else ""
    bot.send_message(
        chat_id,
        "👋 *Uzum Analitika*\n\n"
        "Uzum Market mahsulotlarining aniq sotuv statistikasi.\n\n"
        "📌 *Buyruqlar:*\n"
        "• `/mahsulot 1287402` — mahsulot tahlili\n"
        "• Yoki ID ni to'g'ridan-to'g'ri yuboring"
        + admin_hint,
        parse_mode="Markdown",
        reply_markup=markup,
    )


# ─── Kirish so'rovi ───────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("req_"))
def handle_access_request(call):
    """Foydalanuvchi kirish so'radi."""
    try:
        requester_id = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id)
        return

    if is_authorized(requester_id):
        bot.answer_callback_query(call.id, "✅ Allaqachon ruxsatga egasiz!")
        try:
            bot.send_message(requester_id, "✅ /start ni bosing.")
        except Exception:
            pass
        return

    bot.answer_callback_query(call.id, "✅ So'rovingiz yuborildi!")
    try:
        bot.edit_message_text(
            "✅ So'rovingiz adminga yuborildi. Javob kuting...",
            call.message.chat.id, call.message.message_id,
        )
    except Exception:
        pass

    admin_id = get_admin_chat_id()
    if not admin_id:
        return

    user  = call.from_user
    fname = f"{user.first_name or ''} {user.last_name or ''}".strip() or f"User {requester_id}"
    uname = f"@{user.username}" if user.username else "(username yo'q)"

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Qabul qilish", callback_data=f"approve_{requester_id}"),
        InlineKeyboardButton("❌ Rad etish",    callback_data=f"deny_{requester_id}"),
    )
    try:
        bot.send_message(
            admin_id,
            f"👤 *Kirish so'rovi*\n\n"
            f"Ism: {fname}\n"
            f"Username: {uname}\n"
            f"ID: `{requester_id}`",
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except Exception as e:
        print(f"[bot] Admin notify error: {e}")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("approve_"))
def handle_approve(call):
    """Admin foydalanuvchini tasdiqladi."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Faqat admin!")
        return
    try:
        target_id = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id)
        return

    if add_user(target_id, f"User {target_id}"):
        bot.answer_callback_query(call.id, "✅ Qabul qilindi!")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, f"✅ `{target_id}` qabul qilindi.", parse_mode="Markdown")
        try:
            bot.send_message(
                target_id,
                "✅ *Kirish ruxsati berildi!*\n\n"
                "Endi botdan foydalanishingiz mumkin.\n/start ni bosing.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id, "Allaqachon mavjud")
        bot.send_message(call.message.chat.id, f"⚠️ `{target_id}` allaqachon ro'yxatda.", parse_mode="Markdown")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("deny_"))
def handle_deny(call):
    """Admin so'rovni rad etdi."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Faqat admin!")
        return
    try:
        target_id = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, "Rad etildi")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, f"❌ `{target_id}` rad etildi.", parse_mode="Markdown")
    try:
        bot.send_message(target_id, "❌ Kirish so'rovingiz rad etildi.")
    except Exception:
        pass


# ─── Foydalanuvchilarni boshqarish ────────────────────────────────────────────

@bot.message_handler(commands=["users"])
def handle_users(message):
    if not check_auth(message):
        return
    if not is_admin(message.chat.id):
        bot.reply_to(message, "❌ Faqat admin uchun.")
        return
    data  = load_auth()
    users = data.get("users", [])
    if not users:
        bot.reply_to(message, "Hali foydalanuvchilar yo'q.")
        return
    lines = [f"👥 *Foydalanuvchilar ({len(users)} ta):*\n"]
    for u in users:
        icon  = "👑" if u.get("role") == "admin" else "👤"
        name  = u.get("name") or f"User {u['chat_id']}"
        uname = f" (@{u['username']})" if u.get("username") else ""
        lines.append(f"{icon} `{u['chat_id']}` — {name}{uname}")
    lines.append("\n_O'chirish: `/removeuser <chat_id>`_")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["adduser"])
def handle_adduser(message):
    if not check_auth(message):
        return
    if not is_admin(message.chat.id):
        bot.reply_to(message, "❌ Faqat admin uchun.")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(
            message,
            "Ishlatish: `/adduser 123456789 Ism`\n\n"
            "_chat\\_id ni bilish uchun foydalanuvchi `/start` bosganda bot log'da chiqadi._",
            parse_mode="Markdown",
        )
        return
    target_id = int(parts[1])
    name      = " ".join(parts[2:]) if len(parts) > 2 else f"User {target_id}"
    if add_user(target_id, name):
        bot.reply_to(message, f"✅ Qo'shildi: `{target_id}` — {name}", parse_mode="Markdown")
        try:
            bot.send_message(
                target_id,
                "✅ *Uzum Analitika*\n\nSizga kirish ruxsati berildi!\n/start bosing.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    else:
        bot.reply_to(message, f"⚠️ `{target_id}` allaqachon mavjud.", parse_mode="Markdown")


@bot.message_handler(commands=["removeuser"])
def handle_removeuser(message):
    if not check_auth(message):
        return
    if not is_admin(message.chat.id):
        bot.reply_to(message, "❌ Faqat admin uchun.")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(message, "Ishlatish: `/removeuser 123456789`", parse_mode="Markdown")
        return
    target_id = int(parts[1])
    if target_id == message.chat.id:
        bot.reply_to(message, "❌ O'zingizni o'chira olmaysiz!")
        return
    if remove_user(target_id):
        bot.reply_to(message, f"✅ `{target_id}` o'chirildi.", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"⚠️ `{target_id}` topilmadi.", parse_mode="Markdown")


# ─── Mahsulot ─────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["mahsulot"])
def handle_product_cmd(message):
    if not check_auth(message):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "❌ ID kiriting: `/mahsulot 1287402`", parse_mode="Markdown")
        return
    _send_product(message, int(parts[1].strip()))


@bot.message_handler(func=lambda m: m.chat.id in _login_state)
def handle_login_flow(message):
    """Login state machine — telefon va OTP qabul qiladi."""
    if not check_auth(message):
        return
    chat_id = message.chat.id
    state   = _login_state.get(chat_id)
    text    = message.text.strip()

    if state == "awaiting_phone":
        msg = bot.reply_to(message, "⏳ SMS yuborilmoqda...")
        _login_state[chat_id] = "processing"

        def _do_phone():
            try:
                from uzum_login import start_login
                result = start_login(chat_id, text)
                if result["ok"]:
                    _login_state[chat_id] = "awaiting_otp"
                    bot.edit_message_text(
                        "✅ SMS yuborildi!\n\n📨 *6 xonali kodni yuboring:*\n_(5 daqiqa ichida)_",
                        chat_id, msg.message_id, parse_mode="Markdown",
                    )
                else:
                    del _login_state[chat_id]
                    bot.edit_message_text(
                        f"❌ Xato: {result['error']}\n\nQayta: /login",
                        chat_id, msg.message_id,
                    )
            except Exception as e:
                _login_state.pop(chat_id, None)
                bot.edit_message_text(f"❌ Xato: {e}", chat_id, msg.message_id)

        threading.Thread(target=_do_phone, daemon=True).start()

    elif state == "awaiting_otp":
        msg = bot.reply_to(message, "⏳ Kod tekshirilmoqda...")
        _login_state[chat_id] = "processing"

        def _do_otp():
            try:
                from uzum_login import submit_otp
                result = submit_otp(chat_id, text)
                if result["ok"]:
                    _login_state.pop(chat_id, None)
                    bot.edit_message_text(
                        "✅ *Login muvaffaqiyatli!*\n\n🔑 Token yangilandi.\n📦 Mahsulotlar endi ishlaydi!",
                        chat_id, msg.message_id, parse_mode="Markdown",
                    )
                else:
                    _login_state[chat_id] = "awaiting_otp"
                    bot.edit_message_text(
                        f"❌ {result['error']}\n\nKodni qayta yuboring:",
                        chat_id, msg.message_id,
                    )
            except Exception as e:
                _login_state.pop(chat_id, None)
                bot.edit_message_text(f"❌ Xato: {e}", chat_id, msg.message_id)

        threading.Thread(target=_do_otp, daemon=True).start()


@bot.message_handler(func=lambda m: m.text and m.text.strip().isdigit())
def handle_plain_id(message):
    if not check_auth(message):
        return
    _send_product(message, int(message.text.strip()))


def _send_product(message, pid):
    msg = bot.reply_to(message, "⏳ Yuklanmoqda...")
    try:
        p = fetch_product(pid)
        if not p:
            bot.edit_message_text(
                "❌ Mahsulot topilmadi yoki token eskirgan",
                message.chat.id, msg.message_id,
            )
            return
        store_snapshot(p)

        chars = p.get("characteristics", [])
        from app import extract_variant_label
        color_stock: dict = {}
        for sku in p.get("skuList", []):
            color, _ = extract_variant_label(sku, chars)
            if color:
                color_stock[color] = color_stock.get(color, 0) + (sku.get("availableAmount") or 0)

        color_lines = ""
        if color_stock:
            color_lines = "\n\n🎨 *Ranglar (qoldiq):*\n"
            for name, stock in sorted(color_stock.items(), key=lambda x: -x[1]):
                color_lines += f"  • {name}: {fmt_num(stock)} dona\n"

        seller = p.get("seller") or {}
        text = (
            f"📦 *{(p.get('title') or '')[:70]}*\n"
            f"🏪 _{seller.get('title', '—')}_\n\n"
            f"✅ *Jami sotuv:* `{fmt_num(p.get('ordersAmount'))}` ta\n"
            f"🔄 *Yaqin davr:* `{fmt_num(p.get('rOrdersAmount'))}` ta\n"
            f"⭐ *Reyting:* {(p.get('rating') or 0):.1f}\n"
            f"💬 *Izohlar:* {fmt_num(p.get('reviewsAmount'))} ta\n"
            f"📦 *Qoldiq:* {fmt_num(p.get('totalAvailableAmount'))} dona"
            f"{color_lines}"
        )

        markup = InlineKeyboardMarkup()
        url = get_webapp_url()
        if url:
            markup.add(InlineKeyboardButton(
                "🔍 Batafsil ko'rish",
                web_app=WebAppInfo(url=f"{url}?pid={pid}"),
            ))
        markup.add(InlineKeyboardButton(
            "🔗 Uzumda ko'rish",
            url=f"https://uzum.uz/ru/product/p-{pid}",
        ))

        bot.edit_message_text(
            text, message.chat.id, msg.message_id,
            parse_mode="Markdown", reply_markup=markup,
        )
    except Exception as e:
        bot.edit_message_text(f"❌ Xato: {e}", message.chat.id, msg.message_id)


# ─── Sozlamalar ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=["seturl"])
def handle_seturl(message):
    if not check_auth(message):
        return
    if not is_admin(message.chat.id):
        bot.reply_to(message, "❌ Faqat admin uchun.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("https://"):
        bot.reply_to(message, "❌ HTTPS URL: `/seturl https://...`", parse_mode="Markdown")
        return
    url = parts[1].strip().rstrip("/")
    save_webapp_url(url)
    bot.reply_to(message, f"✅ Saqlandi: `{url}`", parse_mode="Markdown")


@bot.message_handler(commands=["login"])
def handle_login(message):
    if not check_auth(message):
        return
    chat_id = message.chat.id
    _login_state[chat_id] = "awaiting_phone"
    bot.reply_to(
        message,
        "📱 *Uzum Login*\n\n"
        "Telefon raqamingizni yuboring:\n"
        "_(masalan: `901234567` yoki `+998901234567`)_\n\n"
        "Bekor qilish uchun: /cancel",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    if not check_auth(message):
        return
    chat_id = message.chat.id
    if chat_id in _login_state:
        del _login_state[chat_id]
        try:
            from uzum_login import cancel_login
            cancel_login(chat_id)
        except Exception:
            pass
        bot.reply_to(message, "❌ Login bekor qilindi.")
    else:
        bot.reply_to(message, "Hozir faol jarayon yo'q.")


@bot.message_handler(commands=["token"])
def handle_token(message):
    if not check_auth(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or len(parts[1]) < 50:
        bot.reply_to(
            message,
            "❌ Token qisqa yoki kiritilmadi.\n\nIshlatish: `/token eyJraWQi...`\n\n"
            "Tokenni Uzum ilovasidan oling:\n"
            "Ilova → Profil → Uzum ID → Chiqish (oldidan) → Kirish → SMS kod",
            parse_mode="Markdown",
        )
        return
    new_token = parts[1].strip()
    s = {"token": new_token, "xiid": "9499b4e3-636a-416e-8c9a-30ecfae50e55"}
    (_DATA_DIR / "settings.json").write_text(json.dumps(s, indent=2))
    bot.reply_to(message, "✅ Uzum token yangilandi! Endi mahsulotlarni yuklash mumkin.")


@bot.message_handler(content_types=["document"])
def handle_session_upload(message):
    if not check_auth(message):
        return
    doc = message.document
    if not doc or not doc.file_name or "session" not in doc.file_name.lower():
        return
    try:
        file_info = bot.get_file(doc.file_id)
        data = bot.download_file(file_info.file_path)
        (_DATA_DIR / "uzum_session.json").write_bytes(data)
        bot.reply_to(message, "✅ Session fayl saqlandi! Avto-refresh ishlay boshlaydi.")
        try:
            from token_refresher import refresh_and_save
            ok = refresh_and_save()
            if ok:
                bot.reply_to(message, "✅ Yangi token muvaffaqiyatli olindi!")
            else:
                bot.reply_to(message, "⚠️ Token olishda muammo — loglarni tekshiring")
        except Exception as e:
            bot.reply_to(message, f"⚠️ Refresh xato: {e}")
    except Exception as e:
        bot.reply_to(message, f"❌ Xato: {e}")


@bot.message_handler(commands=["status"])
def handle_status(message):
    if not check_auth(message):
        return
    try:
        import base64
        import time as _time
        s_path    = Path(__file__).parent / "settings.json"
        sess_path = Path(__file__).parent / "uzum_session.json"
        if not s_path.exists():
            bot.reply_to(message, "❌ settings.json yo'q")
            return
        s = json.loads(s_path.read_text())
        token = s.get("token", "")
        if not token:
            bot.reply_to(message, "❌ Token yo'q")
            return
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp  = data.get("exp", 0)
        left = exp - int(_time.time())
        hours = left // 3600
        mins  = (left % 3600) // 60
        sess_ok = "✅" if sess_path.exists() else "❌"
        bot.reply_to(
            message,
            f"📊 *Token holati*\n\n"
            f"⏳ Eskirishigacha: `{hours}s {mins}d`\n"
            f"💾 Session fayl: {sess_ok}\n"
            f"🔄 Avto-refresh: {'yoqilgan' if sess_path.exists() else 'session kerak'}",
            parse_mode="Markdown",
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Xato: {e}")


# ─── Callbacks ────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "do_login")
def handle_do_login_callback(call):
    """Token eskirdi xabardagi 'Login qilish' tugmasi."""
    chat_id = call.message.chat.id
    if not is_authorized(chat_id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q")
        return
    bot.answer_callback_query(call.id)
    _login_state[chat_id] = "awaiting_phone"
    bot.send_message(
        chat_id,
        "📱 *Uzum Login*\n\n"
        "Telefon raqamingizni yuboring:\n"
        "_(masalan: `901234567`)_\n\n"
        "Bekor qilish uchun: /cancel",
        parse_mode="Markdown",
    )


# ─── Polling ──────────────────────────────────────────────────────────────────

def start_polling():
    print(f"🤖 Bot ishga tushdi | Mini App: {get_webapp_url() or '(sozlanmagan)'}")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    start_polling()
