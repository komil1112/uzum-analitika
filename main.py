"""Entry point for Railway — runs Flask + Telegram bot together."""
import os
import threading

from app import app, start_background_refresher
from bot import start_polling

if __name__ == "__main__":
    # Bot va background refresher alohida threadlarda ishlaydi
    threading.Thread(target=start_polling, daemon=True).start()
    start_background_refresher(6)

    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Flask server: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
