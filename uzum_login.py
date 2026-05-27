"""
Uzum Market — SMS OTP login (Persistent Worker Thread pattern).

Playwright sync API thread'ga bog'langan — shuning uchun bitta
doimiy worker thread ishlatamiz. Barcha Playwright operatsiyalar
shu thread ichida bajariladi.

Jarayon:
  1. start_login(chat_id, phone)  → SMS yuboradi, brauzer kutadi
  2. submit_otp(chat_id, otp)     → OTP kiritadi, session/token saqlaydi
"""
import json
import os
import queue
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE  = DATA_DIR / "uzum_session.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

OTP_TIMEOUT = 300  # 5 daqiqa

# ── Worker ────────────────────────────────────────────────────────────────────
_cmd_queue: queue.Queue = queue.Queue()
_worker_thread = None  # type: threading.Thread
_worker_lock = threading.Lock()

# Faol sessiya holati (worker thread ichida o'qiladi/yoziladi)
_session_state: dict = {}  # {chat_id, browser, pw, context, page, phone, expires_at}


def _save_token(token: str):
    s = {}
    if SETTINGS_FILE.exists():
        try:
            s = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    s["token"] = token.strip('"')
    s.setdefault("xiid", "9499b4e3-636a-416e-8c9a-30ecfae50e55")
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def _find_otp_input(page):
    """OTP input elementini topadi."""
    selectors = [
        'input[type="number"]',
        'input[inputmode="numeric"]',
        '.sign-in-code input',
        '[class*="otp"] input',
        '[class*="code"] input',
        'input[maxlength="1"]',
        'input[maxlength="6"]',
    ]
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            return el
    return None


def _wait_otp_input(page, timeout_ms=8000) -> bool:
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        if _find_otp_input(page):
            return True
        time.sleep(0.3)
    return False


def _worker_loop():
    """Doimiy worker — barcha Playwright operatsiyalar shu yerda."""
    global _session_state

    pw = None
    browser = None
    context = None
    page = None

    def close_browser():
        nonlocal pw, browser, context, page
        for obj in [page, context, browser, pw]:
            try:
                if obj:
                    obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception:
                pass
        pw = browser = context = page = None
        _session_state.clear()

    while True:
        try:
            cmd, args, resp_q = _cmd_queue.get(timeout=60)
        except queue.Empty:
            # Sessiya vaqti o'tdimi?
            if _session_state and time.time() > _session_state.get("expires_at", 0):
                close_browser()
            continue

        try:
            # ── START LOGIN ──────────────────────────────────────────────────
            if cmd == "start_login":
                chat_id = args["chat_id"]
                phone   = args["phone"]

                # Eski brauzer bor bo'lsa yopamiz
                close_browser()

                from playwright.sync_api import sync_playwright as _spw
                pw      = _spw().start()
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="ru-RU",
                )
                page = context.new_page()

                page.goto("https://uzum.uz/ru", wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2500)

                # Login modal
                page.click('[data-test-id="button__auth"]')
                page.wait_for_selector('input[type="tel"]', timeout=8000)
                page.wait_for_timeout(500)

                tel = page.query_selector('input[type="tel"]')
                if not tel:
                    close_browser()
                    resp_q.put({"ok": False, "error": "Telefon maydoni topilmadi"})
                    continue

                tel.click()
                tel.fill(phone)
                page.wait_for_timeout(400)

                btn = (
                    page.query_selector('.sign-in-phone button.ui-button') or
                    page.query_selector('.sign-in-phone button')
                )
                if not btn:
                    close_browser()
                    resp_q.put({"ok": False, "error": "'Получить код' tugmasi topilmadi"})
                    continue

                btn.click()
                page.wait_for_timeout(3000)

                if not _wait_otp_input(page, timeout_ms=8000):
                    err_el  = page.query_selector('.sign-in-phone [class*="error"]')
                    err_txt = err_el.inner_text().strip()[:100] if err_el else "OTP maydoni kelmadi"
                    close_browser()
                    resp_q.put({"ok": False, "error": err_txt})
                    continue

                _session_state = {
                    "chat_id":    chat_id,
                    "phone":      phone,
                    "expires_at": time.time() + OTP_TIMEOUT,
                }
                resp_q.put({"ok": True})

            # ── SUBMIT OTP ───────────────────────────────────────────────────
            elif cmd == "submit_otp":
                otp = args["otp"]

                if not _session_state or not page:
                    resp_q.put({"ok": False, "error": "Sessiya yo'q — /login qayta bosing"})
                    continue

                if time.time() > _session_state.get("expires_at", 0):
                    close_browser()
                    resp_q.put({"ok": False, "error": "5 daqiqa o'tdi — /login qayta bosing"})
                    continue

                # OTP kiritish
                otp_inputs = page.query_selector_all(
                    'input[type="number"], input[maxlength="1"], .sign-in-code input'
                )

                if len(otp_inputs) >= 4:
                    for i, ch in enumerate(otp):
                        if i < len(otp_inputs):
                            otp_inputs[i].click()
                            otp_inputs[i].fill(ch)
                            page.wait_for_timeout(80)
                else:
                    single = _find_otp_input(page)
                    if not single:
                        resp_q.put({"ok": False, "error": "OTP maydoni topilmadi"})
                        continue
                    single.click()
                    single.fill(otp)

                page.wait_for_timeout(2000)

                # Confirm tugmasi
                confirm = page.query_selector(
                    '.sign-in-code button.ui-button, '
                    'button:has-text("Войти"), button:has-text("Подтвердить")'
                )
                if confirm and confirm.is_enabled():
                    confirm.click()
                    page.wait_for_timeout(3000)

                # Xato bormi?
                err_el = page.query_selector('.sign-in-code [class*="error"]')
                if err_el:
                    err_txt = (err_el.inner_text() or "").strip()
                    if err_txt and len(err_txt) > 2:
                        resp_q.put({"ok": False, "error": f"Uzum: {err_txt[:80]}"})
                        continue

                # Token olish
                token = page.evaluate("() => localStorage.getItem('auth_sdk_access_token')")
                if not token or len(token.strip('"')) < 50:
                    page.wait_for_timeout(3000)
                    token = page.evaluate("() => localStorage.getItem('auth_sdk_access_token')")

                if not token or len(token.strip('"')) < 50:
                    resp_q.put({"ok": False, "error": "Token olinmadi — OTP noto'g'ri bo'lishi mumkin"})
                    continue

                # Session va token saqlash
                try:
                    context.storage_state(path=str(SESSION_FILE))
                except Exception:
                    pass
                _save_token(token)
                close_browser()
                resp_q.put({"ok": True})

            # ── CANCEL ───────────────────────────────────────────────────────
            elif cmd == "cancel":
                close_browser()
                resp_q.put({"ok": True})

        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                close_browser()
            except Exception:
                pass
            resp_q.put({"ok": False, "error": str(e)[:200]})


def _ensure_worker():
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="uzum-login-worker")
            _worker_thread.start()


# ── Public API ────────────────────────────────────────────────────────────────

def start_login(chat_id: int, phone: str) -> dict:
    """SMS yuboradi. Returns {"ok": True} yoki {"ok": False, "error": "..."}"""
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+998"):
        phone = phone[4:]
    elif phone.startswith("998"):
        phone = phone[3:]
    if len(phone) != 9 or not phone.isdigit():
        return {"ok": False, "error": f"Noto'g'ri format: '{phone}' (9 raqam kerak, masalan: 901234567)"}

    _ensure_worker()
    resp_q: queue.Queue = queue.Queue()
    _cmd_queue.put(("start_login", {"chat_id": chat_id, "phone": phone}, resp_q))
    try:
        return resp_q.get(timeout=45)
    except queue.Empty:
        return {"ok": False, "error": "Vaqt tugadi (45s) — internet yoki Uzum muammosi"}


def submit_otp(chat_id: int, otp: str) -> dict:
    """OTP kodni yuboradi. Returns {"ok": True} yoki {"ok": False, "error": "..."}"""
    otp = otp.strip()
    if not otp.isdigit() or len(otp) < 4:
        return {"ok": False, "error": "OTP faqat raqamlardan iborat bo'lishi kerak"}

    _ensure_worker()
    resp_q: queue.Queue = queue.Queue()
    _cmd_queue.put(("submit_otp", {"chat_id": chat_id, "otp": otp}, resp_q))
    try:
        return resp_q.get(timeout=30)
    except queue.Empty:
        return {"ok": False, "error": "Vaqt tugadi (30s)"}


def cancel_login(chat_id: int):
    """Faol login sessiyasini bekor qiladi."""
    _ensure_worker()
    resp_q: queue.Queue = queue.Queue()
    _cmd_queue.put(("cancel", {"chat_id": chat_id}, resp_q))
    try:
        resp_q.get(timeout=10)
    except queue.Empty:
        pass


def has_active_session(chat_id: int) -> bool:
    return bool(_session_state) and time.time() < _session_state.get("expires_at", 0)
