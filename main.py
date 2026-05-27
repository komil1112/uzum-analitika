"""Entry point for Railway — runs Flask + Telegram bot + auto-refresher together."""
import os
import threading

from app import app, start_background_refresher
from bot import start_polling
from token_refresher import start_auto_refresher

if __name__ == "__main__":
    # Bot
    threading.Thread(target=start_polling, daemon=True).start()
    # Mahsulotlarni yangilash
    start_background_refresher(6)
    # Uzum tokenni avtomatik yangilash
    start_auto_refresher()

    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Flask server: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
