"""
Mitmproxy hook — Uzum tokenni avtomatik tutib serverga yuboradi.

Ishlatish:
  mitmweb --listen-port 8888 -s mitmproxy_hook.py

Sozlash (hook ichida o'zgartiring):
  SERVER_URL   — Railway server URL (https://your-app.railway.app)
  SECRET       — TOKEN_UPDATE_SECRET (Railway env var bilan bir xil)
"""
import json
import urllib.request

SERVER_URL = "https://your-app.railway.app"  # <- o'zgartiring
SECRET = "your-secret-here"                   # <- TOKEN_UPDATE_SECRET bilan bir xil


def response(flow):
    # Faqat Uzum auth javoblarini tekshir
    host = flow.request.pretty_host
    if "id.uzum.uz" not in host and "api.uzum.uz" not in host:
        return

    try:
        body = flow.response.json()
    except Exception:
        return

    # Access token qidirish (turli javob formatlarida)
    token = None
    payload = body.get("payload") or body
    if isinstance(payload, dict):
        token = (
            payload.get("accessToken")
            or payload.get("access_token")
            or payload.get("token")
        )

    if not token or len(token) < 50:
        return

    # Token topildi — serverga yuboramiz
    url = f"{SERVER_URL}/api/update-token?t={token}&secret={SECRET}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(f"[uzum-hook] ✅ Token avtomatik yangilandi! ({len(token)} belgi)")
            else:
                print(f"[uzum-hook] ❌ Xato: {result}")
    except Exception as e:
        print(f"[uzum-hook] ❌ Server ulanish xatosi: {e}")
