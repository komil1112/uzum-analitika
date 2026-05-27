"""Uzum Market analytics — Flask backend."""
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

BASE_DIR = Path(__file__).parent

# DATA_DIR — Railway Volume ulanganda /data, lokallda BASE_DIR
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH       = DATA_DIR / "uzum.db"
SETTINGS_FILE = DATA_DIR / "settings.json"

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)


def load_settings():
    # Env vars ustunlik qiladi (Railway uchun)
    token = os.environ.get("UZUM_TOKEN", "")
    xiid = os.environ.get("UZUM_XIID", "")
    if token and xiid:
        return {"token": token, "xiid": xiid}
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {"token": "", "xiid": ""}


def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def uzum_headers():
    s = load_settings()
    return {
        "Accept": "application/json",
        "Accept-Language": "ru-RU",
        "Authorization": f"Bearer {s['token']}",
        "User-Agent": "UzumMarket/2.5.0",
        "x-iid": s["xiid"],
    }


# ----- Database (kunlik trekiing uchun) -----
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER,
      sku_id INTEGER,
      title TEXT,
      orders_amount INTEGER,
      r_orders_amount INTEGER,
      reviews_amount INTEGER,
      total_stock INTEGER,
      sku_stock INTEGER,
      price INTEGER,
      color TEXT,
      size TEXT,
      taken_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_product ON snapshots(product_id, taken_at);

    CREATE TABLE IF NOT EXISTS tracked_products (
      product_id INTEGER PRIMARY KEY,
      title TEXT,
      photo TEXT,
      added_at TEXT,
      last_refreshed TEXT
    );
    """)
    # Migration: weekly_buyers ustun
    cols = [r[1] for r in con.execute("PRAGMA table_info(tracked_products)").fetchall()]
    if "weekly_buyers" not in cols:
        con.execute("ALTER TABLE tracked_products ADD COLUMN weekly_buyers INTEGER")
    if "weekly_updated_at" not in cols:
        con.execute("ALTER TABLE tracked_products ADD COLUMN weekly_updated_at TEXT")
    con.commit()
    con.close()


init_db()


def add_tracking(p):
    """Mahsulotni avtomatik kuzatuvga qo'shadi (upsert)."""
    if not p or not p.get("id"):
        return
    now = datetime.utcnow().isoformat()
    photo = ""
    if p.get("photos"):
        photo = (p["photos"][0] or {}).get("link", {}).get("high", "")
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO tracked_products (product_id, title, photo, added_at, last_refreshed)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(product_id) DO UPDATE SET
             title=excluded.title,
             photo=excluded.photo,
             last_refreshed=excluded.last_refreshed""",
        (p.get("id"), p.get("title"), photo, now, now),
    )
    con.commit()
    con.close()


# ----- Helpers -----
def extract_variant_label(sku, characteristics):
    color = ""
    size = ""
    for ref in sku.get("characteristics", []):
        ci, vi = ref.get("charIndex"), ref.get("valueIndex")
        if ci is None or ci >= len(characteristics):
            continue
        ch = characteristics[ci]
        if vi is None or vi >= len(ch.get("values", [])):
            continue
        val = ch["values"][vi]
        if ch.get("titleType") == "COLOR" or "цвет" in ch.get("title", "").lower():
            color = val.get("title", "")
        else:
            size = val.get("title", "")
    return color, size


def fetch_product(pid):
    try:
        r = requests.get(f"https://api.uzum.uz/api/v2/product/{pid}", headers=uzum_headers(), timeout=10)
        if r.status_code != 200 or not r.text.strip():
            if r.status_code in (401, 403) or not r.text.strip():
                notify_token_expired()
            return None
        return r.json().get("payload", {}).get("data")
    except Exception:
        return None


def notify_token_expired():
    """Token eskirganda Telegram orqali admin ga xabar yuboradi."""
    try:
        cfg_file = DATA_DIR / "bot_settings.json"
        if not cfg_file.exists():
            return
        cfg = json.loads(cfg_file.read_text())
        chat_id = cfg.get("admin_chat_id")
        bot_token = os.environ.get("BOT_TOKEN", "")
        if not chat_id or not bot_token:
            return
        # Oxirgi ogohlantirish vaqtini tekshir (har 30 daqiqada bir marta)
        last = cfg.get("last_notified", 0)
        if time.time() - last < 1800:
            return
        cfg["last_notified"] = time.time()
        cfg_file.write_text(json.dumps(cfg, indent=2))
        msg = "⚠️ *Uzum token eskirdi!*\n\nMahsulotlar yangilanmayapti.\n\nuzum.uz ga kiring → F12 → Network → Authorization headerdan yangi tokenni nusxalab `/token <token>` yuboring."
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception:
        pass


def store_snapshot(p):
    if not p:
        return
    now = datetime.utcnow().isoformat()
    con = sqlite3.connect(DB_PATH)
    chars = p.get("characteristics", [])
    for sku in p.get("skuList", []):
        color, size = extract_variant_label(sku, chars)
        con.execute(
            "INSERT INTO snapshots (product_id, sku_id, title, orders_amount, r_orders_amount, reviews_amount, total_stock, sku_stock, price, color, size, taken_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.get("id"),
                sku.get("id"),
                p.get("title"),
                p.get("ordersAmount") or 0,
                p.get("rOrdersAmount") or 0,
                p.get("reviewsAmount") or 0,
                p.get("totalAvailableAmount") or 0,
                sku.get("availableAmount") or 0,
                sku.get("purchasePrice") or 0,
                color,
                size,
                now,
            ),
        )
    con.commit()
    con.close()


# ----- Routes -----
@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/api/update-token")
def update_token_via_url():
    """GET /api/update-token?t=<token>&secret=<SECRET_KEY> — tokenni URL orqali yangilash."""
    secret = os.environ.get("TOKEN_UPDATE_SECRET", "")
    if not secret:
        return jsonify({"error": "TOKEN_UPDATE_SECRET env var sozlanmagan"}), 403

    provided = request.args.get("secret", "")
    if provided != secret:
        return jsonify({"error": "Noto'g'ri secret"}), 403

    new_token = request.args.get("t", "").strip()
    if len(new_token) < 50:
        return jsonify({"error": "Token juda qisqa yoki yo'q"}), 400

    s = load_settings()
    s["token"] = new_token
    refresh_token = request.args.get("rt", "").strip()
    if refresh_token:
        s["refresh_token"] = refresh_token
    save_settings(s)

    # Admin ga xabar
    try:
        cfg_file = BASE_DIR / "bot_settings.json"
        if cfg_file.exists():
            cfg = json.loads(cfg_file.read_text())
            chat_id = cfg.get("admin_chat_id")
            bot_token = os.environ.get("BOT_TOKEN", "")
            if chat_id and bot_token:
                requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": "✅ Uzum token URL orqali yangilandi!", "parse_mode": "Markdown"},
                    timeout=5,
                )
    except Exception:
        pass

    return jsonify({"ok": True, "message": "Token yangilandi"})


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        save_settings(request.json or {})
        return jsonify({"ok": True})
    s = load_settings()
    return jsonify({"hasToken": bool(s.get("token")), "hasXiid": bool(s.get("xiid"))})


@app.route("/api/product/<int:pid>")
def product(pid):
    p = fetch_product(pid)
    if not p:
        return jsonify({"error": "Mahsulot topilmadi yoki token eskirgan"}), 404

    # Variant tahlili
    chars = p.get("characteristics", [])
    variants = []
    by_color = {}
    for sku in p.get("skuList", []):
        color, size = extract_variant_label(sku, chars)
        v = {
            "skuId": sku.get("id"),
            "color": color,
            "size": size,
            "stock": sku.get("availableAmount", 0),
            "price": sku.get("purchasePrice", 0),
            "fullPrice": sku.get("fullPrice", 0),
        }
        variants.append(v)
        if color:
            by_color.setdefault(color, {"stock": 0, "variants": 0})
            by_color[color]["stock"] += v["stock"]
            by_color[color]["variants"] += 1

    # Snapshot saqlash + avtomatik kuzatuvga qo'shish
    store_snapshot(p)
    add_tracking(p)

    # Tarix (oldingi snapshot bilan farq)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT DISTINCT taken_at, orders_amount, total_stock FROM snapshots WHERE product_id=? ORDER BY taken_at",
        (pid,),
    ).fetchall()
    con.close()

    history = [{"date": r[0], "orders": r[1], "stock": r[2]} for r in rows]

    return jsonify({
        "id": p.get("id"),
        "title": p.get("title"),
        "category": (p.get("category") or {}).get("title"),
        "rating": p.get("rating"),
        "reviewsAmount": p.get("reviewsAmount"),
        "ordersAmount": p.get("ordersAmount"),
        "rOrdersAmount": p.get("rOrdersAmount"),
        "totalAvailableAmount": p.get("totalAvailableAmount"),
        "seller": {
            "title": (p.get("seller") or {}).get("title"),
            "rating": (p.get("seller") or {}).get("rating"),
            "orders": (p.get("seller") or {}).get("orders"),
        },
        "variants": variants,
        "byColor": by_color,
        "history": history,
        "photo": (p.get("photos") or [{}])[0].get("link", {}).get("high", "") if p.get("photos") else "",
    })


@app.route("/api/products", methods=["POST"])
def products_batch():
    """Bir nechta mahsulot ID lari uchun batafsil ma'lumot."""
    ids = (request.json or {}).get("ids", [])
    if not isinstance(ids, list):
        return jsonify({"error": "ids ro'yxat bo'lishi kerak"}), 400

    results = []
    for pid in ids[:50]:  # max 50 ta — token limitini hisobga olib
        try:
            p = fetch_product(int(pid))
            if not p:
                continue
            chars = p.get("characteristics", [])
            stock_by_color = {}
            for sku in p.get("skuList", []):
                color, _ = extract_variant_label(sku, chars)
                if color:
                    stock_by_color[color] = stock_by_color.get(color, 0) + sku.get("availableAmount", 0)
            results.append({
                "id": p.get("id"),
                "title": p.get("title"),
                "orders": p.get("ordersAmount", 0),
                "rOrders": p.get("rOrdersAmount", 0),
                "reviews": p.get("reviewsAmount", 0),
                "stock": p.get("totalAvailableAmount", 0),
                "rating": p.get("rating", 0),
                "price": (p.get("skuList") or [{}])[0].get("purchasePrice", 0) if p.get("skuList") else 0,
                "seller": (p.get("seller") or {}).get("title", ""),
                "colors": stock_by_color,
                "photo": (p.get("photos") or [{}])[0].get("link", {}).get("high", "") if p.get("photos") else "",
            })
            time.sleep(0.15)  # rate limit
        except Exception as e:
            print(f"Error fetching {pid}: {e}")
    return jsonify({"products": results})


@app.route("/api/tracked")
def list_tracked():
    """Kuzatilayotgan barcha mahsulotlar va davriy sotuv farqi."""
    con = sqlite3.connect(DB_PATH)
    tracked = con.execute(
        "SELECT product_id, title, photo, added_at, last_refreshed, weekly_buyers, weekly_updated_at "
        "FROM tracked_products ORDER BY last_refreshed DESC"
    ).fetchall()

    products = []
    for pid, title, photo, added_at, last_refreshed, weekly_buyers, weekly_updated_at in tracked:
        # Eng oxirgi snapshot
        latest = con.execute(
            "SELECT orders_amount, total_stock, taken_at FROM snapshots WHERE product_id=? ORDER BY taken_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
        if not latest:
            continue
        orders_now, stock_now, last_seen = latest

        # N kun oldingi snapshot
        def orders_at(days_ago):
            row = con.execute(
                "SELECT orders_amount FROM snapshots WHERE product_id=? AND datetime(taken_at) <= datetime('now', ?) ORDER BY taken_at DESC LIMIT 1",
                (pid, f"-{days_ago} days"),
            ).fetchone()
            return row[0] if row else None

        o_1d = orders_at(1)
        o_7d = orders_at(7)
        o_30d = orders_at(30)

        products.append({
            "id": pid,
            "title": title,
            "photo": photo,
            "addedAt": added_at,
            "lastSeen": last_seen,
            "ordersNow": orders_now,
            "stockNow": stock_now,
            "today": (orders_now - o_1d) if o_1d is not None else None,
            "last7d": (orders_now - o_7d) if o_7d is not None else None,
            "last30d": (orders_now - o_30d) if o_30d is not None else None,
            "weeklyBuyers": weekly_buyers,
            "weeklyUpdatedAt": weekly_updated_at,
        })

    con.close()
    products.sort(key=lambda x: (x.get("weeklyBuyers") or x.get("today") or 0), reverse=True)
    return jsonify({"products": products, "count": len(products)})


@app.route("/api/raw/<int:pid>")
def raw_product(pid):
    """Debug: Uzum API dan kelgan to'liq xom javob."""
    try:
        r = requests.get(
            f"https://api.uzum.uz/api/v2/product/{pid}",
            headers=uzum_headers(), timeout=10,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/probe/<int:pid>")
def probe_endpoints(pid):
    """Turli endpoint'larni sinab, qaysi biri haftalik sotuv qaytaradi."""
    candidates = [
        f"/api/v2/product/{pid}/orders",
        f"/api/v2/product/{pid}/stats",
        f"/api/v2/product/{pid}/info",
        f"/api/v2/product/{pid}/analytics",
        f"/api/v2/product/{pid}/orders-info",
        f"/api/v2/product/{pid}/weekly",
        f"/api/v2/product/{pid}/buyers",
        f"/api/v2/product/{pid}/sales",
        f"/api/v1/product/{pid}/orders",
        f"/api/v1/product/{pid}/stats",
        f"/api/v1/product/{pid}/weekly-orders",
        f"/api/v2/product/{pid}/orders-count",
        f"/api/v2/products/{pid}/orders",
        f"/api/v2/products/{pid}/stats",
        f"/api/v2/product/{pid}/popularity",
        f"/api/v3/product/{pid}",
    ]
    results = {}
    for path in candidates:
        try:
            r = requests.get(f"https://api.uzum.uz{path}", headers=uzum_headers(), timeout=5)
            results[path] = {
                "status": r.status_code,
                "length": len(r.text or ""),
                "preview": (r.text or "")[:200],
            }
        except Exception as e:
            results[path] = {"error": str(e)[:100]}
    return jsonify(results)


@app.route("/api/weekly", methods=["POST"])
def weekly_batch():
    """Tanlangan mahsulotlar uchun 'Bu hafta N kishi' raqamlarini oladi (Playwright)."""
    ids = (request.json or {}).get("ids", [])
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids ro'yxat bo'lishi kerak"}), 400
    try:
        from weekly_scraper import fetch_weekly_batch
        # Max 30 ta - juda ko'p bo'lsa vaqt oladi
        ids_int = [int(x) for x in ids[:30]]
        data = fetch_weekly_batch(ids_int, delay=0.3)
        return jsonify({"weekly": {str(k): v for k, v in data.items()}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/track", methods=["POST"])
def track_batch():
    """Tanlangan mahsulotlarni kuzatuvga qo'shish."""
    ids = (request.json or {}).get("ids", [])
    if not isinstance(ids, list):
        return jsonify({"error": "ids ro'yxat bo'lishi kerak"}), 400
    added = 0
    for pid in ids[:50]:
        try:
            p = fetch_product(int(pid))
            if p:
                store_snapshot(p)
                add_tracking(p)
                added += 1
            time.sleep(0.15)
        except Exception as e:
            print(f"Track error {pid}: {e}")
    return jsonify({"added": added})


@app.route("/api/untrack/<int:pid>", methods=["DELETE"])
def untrack(pid):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM tracked_products WHERE product_id=?", (pid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


def refresh_all_tracked(fetch_weekly=True):
    """Barcha kuzatilayotgan mahsulotlarni Uzum API dan yangilaydi.

    fetch_weekly=True bo'lsa, "Bu hafta X kishi" ma'lumotini ham Playwright orqali oladi.
    """
    con = sqlite3.connect(DB_PATH)
    ids = [r[0] for r in con.execute("SELECT product_id FROM tracked_products").fetchall()]
    con.close()

    print(f"🔄 Avto-yangilash: {len(ids)} ta mahsulot")
    refreshed = 0
    for pid in ids:
        try:
            p = fetch_product(pid)
            if p:
                store_snapshot(p)
                add_tracking(p)
                refreshed += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"  ❌ {pid}: {e}")
    print(f"✅ Yangilandi: {refreshed}/{len(ids)}")

    # Haftalik xaridorlar sonini ham yangilaymiz (sekinroq)
    if fetch_weekly and ids:
        try:
            from weekly_scraper import fetch_weekly_batch
            print(f"📊 Haftalik ma'lumot yuklanmoqda ({len(ids)} ta)...")
            weekly_data = fetch_weekly_batch(ids, delay=0.3)
            now = datetime.utcnow().isoformat()
            con = sqlite3.connect(DB_PATH)
            for pid, count in weekly_data.items():
                if count is not None:
                    con.execute(
                        "UPDATE tracked_products SET weekly_buyers=?, weekly_updated_at=? WHERE product_id=?",
                        (count, now, pid),
                    )
            con.commit()
            con.close()
            ok = sum(1 for v in weekly_data.values() if v is not None)
            print(f"✅ Haftalik yangilandi: {ok}/{len(ids)}")
        except Exception as e:
            print(f"⚠️ Haftalik yangilash xato: {e}")

    return refreshed


@app.route("/api/refresh", methods=["POST"])
def refresh_endpoint():
    n = refresh_all_tracked()
    return jsonify({"refreshed": n})


def start_background_refresher(interval_hours=6):
    """Fon rejimida har N soatda kuzatilayotgan mahsulotlarni yangilaydi."""
    import threading

    def loop():
        time.sleep(60)  # startup dan keyin 1 daqiqa kutib turish
        while True:
            try:
                refresh_all_tracked()
            except Exception as e:
                print(f"Background refresh error: {e}")
            time.sleep(interval_hours * 3600)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    print(f"⏰ Background refresher har {interval_hours} soatda ishlaydi")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_background_refresher(6)
    print(f"🚀 Uzum Analitika serveri http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
