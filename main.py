"""Entry point for Railway — runs Flask + Telegram bot together."""
import os
import threading

from app import app
from bot import start_polling

if __name__ == "__main__":
    # Bot alohida threadda ishlaydi
    t = threading.Thread(target=start_polling, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Flask server: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
