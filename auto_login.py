"""Telegram bot orqali avtomatik login — OTP so'rab, sessiyani yangilaydi."""
import json
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
SESSION_FILE = DATA_DIR / "uzum_session.json"
BOT_SETTINGS_FILE = DATA_DIR / "bot_settings.json"

def send_telegram(text):
    """Adminga Telegram xabar yuborish."""
    try:
        import requests
        bot_token = os.environ.get("BOT_TOKEN", "")
        if not BOT_SETTINGS_FILE.exists() or not bot_token:
            return False
        cfg = json.loads(BOT_SETTINGS_FILE.read_text())
        chat_id = cfg.get("admin_chat_id")
        if not chat_id:
            return False
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
        return True
    except:
        return False

def wait_for_otp(timeout=120):
    """Telegram botdan OTP kod kutish."""
    cfg = json.loads(BOT_SETTINGS_FILE.read_text())
    otp_file = DATA_DIR / "pending_otp.json"
    
    # OTP so'rash
    otp_data = {"waiting": True, "timestamp": time.time()}
    otp_file.write_text(json.dumps(otp_data))
    
    send_telegram("🔐 *Uzum sessiya eskirgan!*\n\nIltimos, telefoningizga kelgan 6 xonali OTP kodni shu yerga yuboring:")
    
    # OTP kutish (2 daqiqa)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if otp_file.exists():
            data = json.loads(otp_file.read_text())
            if "otp" in data and not data.get("waiting"):
                return data["otp"]
    
    send_telegram("❌ OTP kutilmadi. Qayta urinish kerak.")
    return None

def do_login(phone_number="998913408656"):
    """Playwright orqali login qilish."""
    otp = wait_for_otp()
    if not otp:
        return False
    
    try:
        from playwright.sync_api import sync_playwright
        
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context()
            page = context.new_page()
            
            # Login sahifasi
            page.goto("https://uzum.uz/ru/login", wait_until="networkidle")
            page.wait_for_timeout(3000)
            
            # Telefon raqamni kiritish
            phone_input = page.locator('input[type="tel"]')
            phone_input.fill(phone_number)
            
            # Davom etish tugmasi
            page.locator('button:has-text("Далее")').click()
            page.wait_for_timeout(2000)
            
            # OTP kodni kiritish
            otp_input = page.locator('input[placeholder*="код"]')
            otp_input.fill(otp)
            page.wait_for_timeout(3000)
            
            # Sessiyani saqlash
            context.storage_state(path=str(SESSION_FILE))
            
            browser.close()
        
        send_telegram("✅ *Sessiya muvaffaqiyatli yangilandi!*")
        return True
        
    except Exception as e:
        send_telegram(f"❌ Login xatosi: {e}")
        return False

if __name__ == "__main__":
    do_login()
