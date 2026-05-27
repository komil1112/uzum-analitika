"""
Uzum Market — Telegram bot orqali SMS OTP login.

Jarayon:
  1. start_login(phone)  → brauzer ochadi, telefon kiritadi, SMS yuboradi
  2. submit_otp(otp)     → OTP kiritadi, login yakunlaydi, session/token saqlaydi

Brauzer start_login() va submit_otp() orasida fonda tirik turadi (max 5 daqiqa).
"""
import json
import os
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE  = DATA_DIR / "uzum_session.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

OTP_TIMEOUT = 300  # 5 daqiqa

# Faol login sessiyalari: {chat_id: {browser, context, page, expires_at, phone}}
_active: dict = {}
_lock = threading.Lock()


# ── Yordamchi ────────────────────────────────────────────────────────────────

def _load_settings():
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {}


def _save_token(token: str):
    s = _load_settings()
    s["token"] = token
    s.setdefault("xiid", "9499b4e3-636a-416e-8c9a-30ecfae50e55")
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def _cleanup(chat_id: int):
    """Browser ni yopadi va sessiyani o'chiradi."""
    with _lock:
        sess = _active.pop(chat_id, None)
    if sess:
        try:
            sess["browser"].close()
        except Exception:
            pass


def _auto_expire(chat_id: int, delay: int):
    """Vaqt tugaganda browser ni avtomatik yopadi."""
    time.sleep(delay)
    with _lock:
        sess = _active.get(chat_id)
        if sess and time.time() > sess["expires_at"]:
            _cleanup(chat_id)


# ── Asosiy funksiyalar ────────────────────────────────────────────────────────

def start_login(chat_id: int, phone: str) -> dict:
    """
    Uzum login modal ochadi, telefon raqamini kiritadi, SMS yuboradi.

    phone: '901234567' yoki '+998901234567' yoki '998901234567'

    Returns:
        {"ok": True}  — SMS yuborildi, submit_otp() kutilmoqda
        {"ok": False, "error": "..."}
    """
    # Eski sessiyani tozalash
    _cleanup(chat_id)

    # Telefon raqamni tozalash (+998 prefix olib tashlash)
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+998"):
        phone = phone[4:]
    elif phone.startswith("998"):
        phone = phone[3:]
    if len(phone) != 9 or not phone.isdigit():
        return {"ok": False, "error": f"Noto'g'ri telefon: '{phone}' — 9 raqam bo'lishi kerak (901234567)"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "Playwright o'rnatilmagan"}

    try:
        pw      = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        page = context.new_page()

        page.goto("https://uzum.uz/ru", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)

        # Login modal ochish
        page.click('[data-test-id="button__auth"]')
        page.wait_for_selector('input[type="tel"]', timeout=8000)
        page.wait_for_timeout(500)

        # Telefon raqam kiritish
        tel_input = page.query_selector('input[type="tel"]')
        if not tel_input:
            browser.close()
            pw.stop()
            return {"ok": False, "error": "Telefon maydoni topilmadi"}

        tel_input.click()
        tel_input.fill(phone)
        page.wait_for_timeout(500)

        # "Получить код" tugmasi
        btn = page.query_selector('.sign-in-phone button.ui-button')
        if not btn:
            # Fallback: birinchi submit/button
            btn = page.query_selector('.sign-in-phone button')
        if not btn:
            browser.close()
            pw.stop()
            return {"ok": False, "error": "'Получить код' tugmasi topilmadi"}

        btn.click()
        page.wait_for_timeout(3000)

        # OTP maydoni paydo bo'ldimi?
        otp_appeared = _wait_for_otp_input(page, timeout=8000)
        if not otp_appeared:
            # Xato xabari bormi?
            err_el = page.query_selector('.sign-in-phone .error, .ui-input__error, [class*="error"]')
            err_text = err_el.inner_text() if err_el else "OTP maydoni kelmadi"
            browser.close()
            pw.stop()
            return {"ok": False, "error": err_text.strip()[:100]}

        # Sessiyani saqlab qo'yamiz
        with _lock:
            _active[chat_id] = {
                "browser":    browser,
                "pw":         pw,
                "context":    context,
                "page":       page,
                "phone":      phone,
                "expires_at": time.time() + OTP_TIMEOUT,
            }

        # Avtomatik tozalash thread
        threading.Thread(
            target=_auto_expire, args=(chat_id, OTP_TIMEOUT), daemon=True
        ).start()

        return {"ok": True}

    except Exception as e:
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:200]}


def submit_otp(chat_id: int, otp: str) -> dict:
    """
    OTP kodni kiritadi va login yakunlaydi.

    Returns:
        {"ok": True, "token": "..."}
        {"ok": False, "error": "..."}
    """
    otp = otp.strip()
    if not otp.isdigit() or len(otp) < 4:
        return {"ok": False, "error": "OTP faqat raqamlardan iborat bo'lishi kerak"}

    with _lock:
        sess = _active.get(chat_id)

    if not sess:
        return {"ok": False, "error": "Login sessiyasi topilmadi yoki muddati o'tdi. /login qayta bosing"}

    if time.time() > sess["expires_at"]:
        _cleanup(chat_id)
        return {"ok": False, "error": "5 daqiqa o'tdi — /login qayta bosing"}

    page = sess["page"]

    try:
        # OTP kiritish (6 ta alohida input yoki bitta)
        otp_inputs = page.query_selector_all('input[type="number"], input[maxlength="1"], .otp-input input')

        if len(otp_inputs) >= 4:
            # Har bir raqamni alohida kiritish
            for i, ch in enumerate(otp):
                if i < len(otp_inputs):
                    otp_inputs[i].click()
                    otp_inputs[i].fill(ch)
                    page.wait_for_timeout(100)
        else:
            # Bitta input ga to'liq kiritish
            otp_input = _find_otp_input(page)
            if not otp_input:
                return {"ok": False, "error": "OTP maydoni topilmadi"}
            otp_input.click()
            otp_input.fill(otp)

        page.wait_for_timeout(2000)

        # Confirm tugmasini bosish (agar avtomatik yuborilmasa)
        confirm_btn = page.query_selector(
            '.sign-in-code button.ui-button, '
            '.otp-form button[type="submit"], '
            'button:has-text("Войти"), button:has-text("Подтвердить")'
        )
        if confirm_btn and confirm_btn.is_enabled():
            confirm_btn.click()
            page.wait_for_timeout(3000)

        # Xato bormi?
        err_el = page.query_selector('[class*="error"]:not([class*="border"])')
        if err_el:
            err_txt = (err_el.inner_text() or '').strip()
            if err_txt and len(err_txt) > 2:
                return {"ok": False, "error": f"Uzum xatosi: {err_txt[:80]}"}

        # Token olish
        token = page.evaluate("() => localStorage.getItem('auth_sdk_access_token')")
        if not token or len(token.strip('"')) < 50:
            # Sahifa o'zgardimi — login bo'ldimi?
            page.wait_for_timeout(3000)
            token = page.evaluate("() => localStorage.getItem('auth_sdk_access_token')")

        if not token or len(token.strip('"')) < 50:
            return {"ok": False, "error": "Token olinmadi — OTP noto'g'ri bo'lishi mumkin"}

        token = token.strip('"')

        # Session va token saqlash
        context = sess["context"]
        context.storage_state(path=str(SESSION_FILE))
        _save_token(token)

        _cleanup(chat_id)
        return {"ok": True, "token": token}

    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def cancel_login(chat_id: int):
    """Faol login sessiyasini bekor qiladi."""
    _cleanup(chat_id)


def has_active_session(chat_id: int) -> bool:
    with _lock:
        sess = _active.get(chat_id)
        if not sess:
            return False
        if time.time() > sess["expires_at"]:
            _cleanup(chat_id)
            return False
        return True


# ── Ichki yordamchilar ────────────────────────────────────────────────────────

def _wait_for_otp_input(page, timeout: int = 8000) -> bool:
    """OTP input maydoni paydo bo'lishini kutadi."""
    start = time.time()
    while (time.time() - start) * 1000 < timeout:
        inp = _find_otp_input(page)
        if inp:
            return True
        time.sleep(0.4)
    return False


def _find_otp_input(page):
    """OTP input ni topadi (turli selectorlar bilan)."""
    selectors = [
        'input[type="number"]',
        'input[inputmode="numeric"]',
        '.sign-in-code input',
        '[class*="otp"] input',
        '[class*="code"] input[type="text"]',
        'input[maxlength="1"]',
        'input[maxlength="6"]',
    ]
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            return el
    return None
