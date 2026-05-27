"""
Avto-token yangilash — Playwright headless brauzer orqali.

Saqlangan sessiya (uzum_session.json) bilan brauzer ochiladi,
uzum.uz yangi access tokenni avtomatik chiqaradi, biz uni
settings.json ga yozamiz.
"""
import base64
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
SESSION_FILE = BASE_DIR / "uzum_session.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
BOT_SETTINGS_FILE = BASE_DIR / "bot_settings.json"


def jwt_expiry(token):
    """JWT 'exp' ni o'qib unix vaqt qaytaradi."""
    try:
        payload = token.split(".")[1]
        # base64 padding
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("exp", 0))
    except Exception:
        return 0


def notify_admin(text):
    """Telegram orqali admin ga xabar yuborish."""
    try:
        import requests
        bot_token = os.environ.get("BOT_TOKEN", "")
        if not BOT_SETTINGS_FILE.exists() or not bot_token:
            return
        cfg = json.loads(BOT_SETTINGS_FILE.read_text())
        chat_id = cfg.get("admin_chat_id")
        if not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        print(f"[refresher] notify xato: {e}")


def refresh_token_once():
    """Saqlangan sessiya bilan yangi access tokenni oladi."""
    if not SESSION_FILE.exists():
        print("[refresher] ❌ uzum_session.json topilmadi. setup_login.py ishlating.")
        return None

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            context = browser.new_context(storage_state=str(SESSION_FILE))
            page = context.new_page()
            page.goto("https://uzum.uz/ru", wait_until="domcontentloaded", timeout=30000)

            # localStorage to'lguncha biroz kutish (SDK token o'qiydi/yangilaydi)
            page.wait_for_timeout(4000)

            token = page.evaluate("() => localStorage.getItem('auth_sdk_access_token')")
            if not token:
                print("[refresher] ❌ localStorage'da token yo'q — sessiya tugagan.")
                notify_admin(
                    "⚠️ *Uzum sessiyasi tugadi!*\n\n"
                    "Mac'da `python setup_login.py` ishlatib qayta login qiling, "
                    "so'ng `uzum_session.json` ni Railway'ga yuklang."
                )
                return None

            # Tirnoqlarni olib tashlash
            token = token.strip('"').strip()

            # Yangi sessiyani saqlash (cookies yangilangan bo'lishi mumkin)
            context.storage_state(path=str(SESSION_FILE))

            return token
        finally:
            browser.close()


def update_settings_with_token(token):
    """settings.json'ga yangi tokenni yozish."""
    s = {}
    if SETTINGS_FILE.exists():
        s = json.loads(SETTINGS_FILE.read_text())
    s["token"] = token
    s.setdefault("xiid", "9499b4e3-636a-416e-8c9a-30ecfae50e55")
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def refresh_and_save():
    """Bitta refresh sikli."""
    try:
        print(f"[refresher] 🔄 Token yangilanmoqda... {datetime.now().isoformat()}")
        token = refresh_token_once()
        if not token:
            return False
        exp = jwt_expiry(token)
        update_settings_with_token(token)
        exp_str = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M") if exp else "?"
        print(f"[refresher] ✅ Token yangilandi. Eskirish: {exp_str}")
        return True
    except Exception as e:
        print(f"[refresher] ❌ Xato: {e}")
        notify_admin(f"⚠️ Token avto-yangilash xatosi:\n`{e}`")
        return False


def auto_refresh_loop():
    """Token eskirishidan 30 daqiqa oldin yangilab turadi."""
    # Startupda 30 soniya kutish
    time.sleep(30)
    while True:
        try:
            current_exp = 0
            if SETTINGS_FILE.exists():
                s = json.loads(SETTINGS_FILE.read_text())
                current_exp = jwt_expiry(s.get("token", ""))

            now = int(time.time())
            seconds_left = current_exp - now

            if seconds_left < 1800:  # 30 daqiqadan kam qolgan
                refresh_and_save()
                # Yangidan o'qish
                if SETTINGS_FILE.exists():
                    s = json.loads(SETTINGS_FILE.read_text())
                    current_exp = jwt_expiry(s.get("token", ""))
                seconds_left = current_exp - int(time.time())

            # Eskirishdan 30 daqiqa oldin uyg'onish (min 5 daq, max 5.5 soat)
            sleep_for = max(300, min(19800, seconds_left - 1800))
            print(f"[refresher] 💤 {sleep_for}s uxlayman (token {seconds_left}s da eskiradi)")
            time.sleep(sleep_for)
        except Exception as e:
            print(f"[refresher] Loop xato: {e}")
            time.sleep(600)


def start_auto_refresher():
    t = threading.Thread(target=auto_refresh_loop, daemon=True)
    t.start()
    print("[refresher] ⏰ Avto-refresher ishga tushdi")


if __name__ == "__main__":
    # Qo'lda test qilish: python token_refresher.py
    refresh_and_save()
