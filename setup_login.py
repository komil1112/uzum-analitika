"""
Bir martalik login skripti.

Ishlatish (Mac'da):
  pip install playwright
  playwright install chromium
  python setup_login.py

Brauzer ochiladi → telefon + SMS bilan kiring → ENTER bosing → session saqlanadi.
Keyin uzum_session.json ni Railway'ga yuklang.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SESSION_FILE = Path(__file__).parent / "uzum_session.json"


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.goto("https://uzum.uz/ru")

        print("\n" + "="*60)
        print("👉 Brauzerda telefon raqam + SMS bilan login qiling")
        print("👉 Login bo'lgach shu yerga qayting va ENTER bosing")
        print("="*60 + "\n")
        input()

        # localStorage va cookies ni saqlash
        storage = context.storage_state()

        # localStorage'dan access tokenni tekshirish
        token = page.evaluate("() => localStorage.getItem('auth_sdk_access_token')")
        if not token or len(token) < 100:
            print("❌ Token topilmadi! Login to'liq bo'lmagan.")
            browser.close()
            sys.exit(1)

        # Tozalab JSON sifatida saqlash
        SESSION_FILE.write_text(json.dumps(storage, indent=2))
        print(f"✅ Sessiya saqlandi: {SESSION_FILE}")
        print(f"   Cookies: {len(storage.get('cookies', []))} ta")
        print(f"   Origins: {len(storage.get('origins', []))} ta")
        print(f"   Token uzunligi: {len(token.strip(chr(34)))}")

        browser.close()


if __name__ == "__main__":
    main()
