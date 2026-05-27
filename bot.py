"""Uzum Analitika — Telegram bot."""
import json
import os
from pathlib import Path

import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app import fetch_product, store_snapshot

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8863044902:AAFh6Vc3SdKkpZu781n_OvX_19qegDXRoxM")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")

BOT_SETTINGS_FILE = Path(__file__).parent / "bot_settings.json"


def get_webapp_url():
    if WEBAPP_URL:
        return WEBAPP_URL
    if BOT_SETTINGS_FILE.exists():
        return json.loads(BOT_SETTINGS_FILE.read_text()).get("webapp_url", "")
    return ""


def save_webapp_url(url):
    BOT_SETTINGS_FILE.write_text(json.dumps({"webapp_url": url}, indent=2))


bot = telebot.TeleBot(BOT_TOKEN)


def fmt_num(n):
    if not n:
        return "—"
    return f"{int(n):,}".replace(",", " ")


@bot.message_handler(commands=["start", "menu"])
def handle_start(message):
    url = get_webapp_url()
    markup = InlineKeyboardMarkup()
    if url:
        markup.add(InlineKeyboardButton("📊 Analitika ochish", web_app=WebAppInfo(url=url)))

    bot.send_message(
        message.chat.id,
        "👋 *Uzum Analitika*\n\n"
        "Uzum Market mahsulotlarining aniq sotuv statistikasi.\n\n"
        "📌 *Buyruqlar:*\n"
        "• `/mahsulot 1287402` — mahsulot tahlili\n"
        "• Yoki ID ni to'g'ridan-to'g'ri yuboring",
        parse_mode="Markdown",
        reply_markup=markup,
    )


@bot.message_handler(commands=["mahsulot"])
def handle_product_cmd(message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "❌ ID kiriting: `/mahsulot 1287402`", parse_mode="Markdown")
        return
    _send_product(message, int(parts[1].strip()))


@bot.message_handler(func=lambda m: m.text and m.text.strip().isdigit())
def handle_plain_id(message):
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

        # Rang taqsimoti
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


@bot.message_handler(commands=["seturl"])
def handle_seturl(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("https://"):
        bot.reply_to(message, "❌ HTTPS URL: `/seturl https://...`", parse_mode="Markdown")
        return
    url = parts[1].strip().rstrip("/")
    save_webapp_url(url)
    bot.reply_to(message, f"✅ Saqlandi: `{url}`", parse_mode="Markdown")


@bot.message_handler(commands=["token"])
def handle_token(message):
    """Uzum tokenni yangilash: /token <yangi_token>"""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or len(parts[1]) < 50:
        bot.reply_to(message,
            "❌ Token qisqa yoki kiritilmadi.\n\n"
            "Ishlatish: `/token eyJraWQi...`\n\n"
            "Tokenni Uzum ilovasidan oling:\n"
            "Ilova → Profil → Uzum ID → Chiqish (oldidan) → Kirish → SMS kod",
            parse_mode="Markdown")
        return
    new_token = parts[1].strip()
    s = {"token": new_token, "xiid": "9499b4e3-636a-416e-8c9a-30ecfae50e55"}
    from pathlib import Path
    import json
    Path(__file__).parent.joinpath("settings.json").write_text(json.dumps(s, indent=2))
    bot.reply_to(message, "✅ Uzum token yangilandi! Endi mahsulotlarni yuklash mumkin.")


def start_polling():
    print(f"🤖 Bot ishga tushdi | Mini App: {get_webapp_url() or '(sozlanmagan)'}")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    start_polling()
