"""
Uzum sahifasidan "Bu hafta X kishi sotib oldi" raqamini ajratib oladi.
Playwright + saqlangan sessiya orqali ishlaydi.
"""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
SESSION_FILE = DATA_DIR / "uzum_session.json"

WEEKLY_RE = re.compile(r"(\d+)\s*челов[а-я]*\s*купили")

_lock = threading.Lock()


def fetch_weekly_buyers(pid, timeout_ms=12000):
    """Bitta mahsulot uchun haftalik xaridorlar sonini qaytaradi.

    None — banner ko'rinmasa (yangi mahsulot yoki kam sotuv).
    """
    if not SESSION_FILE.exists():
        return None

    from playwright.sync_api import sync_playwright

    with _lock:  # bir vaqtda faqat bitta sessiya ishlatish
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                context = browser.new_context(
                    storage_state=str(SESSION_FILE),
                    locale="ru-RU",
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                page = context.new_page()
                page.goto(f"https://uzum.uz/ru/product/p-{pid}",
                          wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2500)
                html = page.content()
                m = WEEKLY_RE.search(html)
                return int(m.group(1)) if m else None  # None = banner topilmadi, eski raqamni saqlash
            except Exception as e:
                print(f"[weekly] {pid}: {e}")
                return None
            finally:
                browser.close()


def fetch_weekly_batch(pids, delay=0.5):
    """Bir nechta mahsulot uchun haftalik raqamlarni ketma-ket oladi.

    Bitta brauzer bilan tezroq ishlaydi.
    """
    if not SESSION_FILE.exists():
        return {}

    from playwright.sync_api import sync_playwright

    results = {}
    with _lock:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                context = browser.new_context(
                    storage_state=str(SESSION_FILE),
                    locale="ru-RU",
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                page = context.new_page()
                for pid in pids:
                    try:
                        page.goto(f"https://uzum.uz/ru/product/p-{pid}",
                                  wait_until="domcontentloaded", timeout=12000)
                        page.wait_for_timeout(2000)
                        html = page.content()
                        m = WEEKLY_RE.search(html)
                        results[pid] = int(m.group(1)) if m else None  # None = eski raqamni saqlash
                        time.sleep(delay)
                    except Exception as e:
                        print(f"[weekly] {pid}: {e}")
                        results[pid] = None
            finally:
                browser.close()
    return results


def _fetch_chunk(pids, delay=0.3):
    """Bitta worker uchun: o'z brauzerini ochib, berilgan ID'larni ketma-ket oladi."""
    if not SESSION_FILE.exists():
        return {pid: None for pid in pids}
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            context = browser.new_context(
                storage_state=str(SESSION_FILE),
                locale="ru-RU",
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            for pid in pids:
                try:
                    page.goto(f"https://uzum.uz/ru/product/p-{pid}",
                              wait_until="domcontentloaded", timeout=12000)
                    page.wait_for_timeout(1800)
                    html = page.content()
                    m = WEEKLY_RE.search(html)
                    results[pid] = int(m.group(1)) if m else 0
                    time.sleep(delay)
                except Exception as e:
                    print(f"[weekly-par] {pid}: {e}")
                    results[pid] = None
        finally:
            browser.close()
    return results


def fetch_weekly_parallel(pids, workers=3, delay=0.3, progress_cb=None):
    """Bir nechta brauzerlarni parallel ishga tushiradi (~3x tezroq).

    progress_cb(done, total) — har worker o'z chunk'ini tugatganda chaqiriladi.
    """
    if not SESSION_FILE.exists():
        return {}
    pids = list(pids)
    if not pids:
        return {}

    chunks = [[] for _ in range(workers)]
    for i, pid in enumerate(pids):
        chunks[i % workers].append(pid)
    chunks = [c for c in chunks if c]

    results = {}
    done = 0
    total = len(pids)

    with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
        futures = {ex.submit(_fetch_chunk, chunk, delay): chunk for chunk in chunks}
        for fut in as_completed(futures):
            try:
                res = fut.result()
                results.update(res)
                done += len(res)
                if progress_cb:
                    try:
                        progress_cb(done, total)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[weekly-par] chunk xato: {e}")
    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for pid in sys.argv[1:]:
            print(f"{pid}: {fetch_weekly_buyers(int(pid))} kishi bu hafta")
