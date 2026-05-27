"""
Mitmproxy hook — Uzum tokenni avtomatik tutib serverga yuboradi.
Ishlatish: mitmweb --listen-port 8888 -s mitmproxy_hook.py
"""
import json
import urllib.request
import urllib.parse

SERVER_URL = "https://uzum-analitika-production.up.railway.app"
SECRET = "uzum2024secret"

UZUM_HOSTS = ("id.uzum.uz", "api.uzum.uz", "uzum.uz")


def find_token(obj, depth=0):
    """Nested dict/list ichidan access token va refresh token qidiradi."""
    if depth > 6 or not obj:
        return None, None
    if isinstance(obj, dict):
        access = (
            obj.get("accessToken")
            or obj.get("access_token")
            or obj.get("token")
            or obj.get("idToken")
            or obj.get("id_token")
        )
        refresh = (
            obj.get("refreshToken")
            or obj.get("refresh_token")
        )
        if access and len(str(access)) > 50:
            return str(access), str(refresh) if refresh else None
        for v in obj.values():
            a, r = find_token(v, depth + 1)
            if a:
                return a, r
    elif isinstance(obj, list):
        for item in obj:
            a, r = find_token(item, depth + 1)
            if a:
                return a, r
    return None, None


def response(flow):
    host = flow.request.pretty_host
    if not any(h in host for h in UZUM_HOSTS):
        return

    # Faqat auth so'rovlari
    path = flow.request.path
    if not any(p in path for p in ("/auth/", "/token", "/login", "/signin", "/oauth")):
        return

    print(f"[uzum-hook] 🔍 Uzum auth so'rov: {host}{path}")

    try:
        text = flow.response.get_text()
        if not text or len(text) < 20:
            return
        body = json.loads(text)
    except Exception as e:
        print(f"[uzum-hook] JSON parse xato: {e}")
        return

    access_token, refresh_token = find_token(body)

    if not access_token:
        print(f"[uzum-hook] Token topilmadi. Javob kalitlari: {list(body.keys()) if isinstance(body, dict) else type(body)}")
        return

    print(f"[uzum-hook] ✅ Token topildi! ({len(access_token)} belgi)")
    if refresh_token:
        print(f"[uzum-hook] 🔄 Refresh token ham topildi! ({len(refresh_token)} belgi)")

    # Serverga yuborish
    params = urllib.parse.urlencode({"t": access_token, "secret": SECRET})
    if refresh_token:
        params += "&" + urllib.parse.urlencode({"rt": refresh_token})

    url = f"{SERVER_URL}/api/update-token?{params}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(f"[uzum-hook] ✅ Server ga yuborildi va saqlandi!")
            else:
                print(f"[uzum-hook] ❌ Server xato: {result}")
    except Exception as e:
        print(f"[uzum-hook] ❌ Server ulanish xatosi: {e}")
