"""Uzum Market analytics — Flask backend."""
import json
import os
import sqlite3
import threading
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
    """settings.json dan o'qiydi. Env var faqat zaxira sifatida."""
    xiid = os.environ.get("UZUM_XIID", "")
    
    if SETTINGS_FILE.exists():
        s = json.loads(SETTINGS_FILE.read_text())
        token = s.get("token", "")
        xiid = s.get("xiid", xiid)
        if token:
            return {"token": token, "xiid": xiid}
    
    # settings.json bo'sh yoki token yo'q bo'lsa env var dan ol
    token = os.environ.get("UZUM_TOKEN", "")
    return {"token": token, "xiid": xiid}


def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def _token_is_valid(token: str) -> bool:
    """JWT eskirmaganmi tekshiradi. Eskirgan/buzuq bo'lsa False."""
    try:
        raw = (token or "").strip('"')
        if not raw or '.' not in raw:
            return False
        import base64 as _b64
        payload_b64 = raw.split('.')[1]
        pad = 4 - len(payload_b64) % 4
        if pad != 4:
            payload_b64 += '=' * pad
        payload = json.loads(_b64.b64decode(payload_b64))
        exp = payload.get("exp", 0)
        # 60s zaxira bilan: hali amal qilsa True
        return bool(exp) and (time.time() < exp - 60)
    except Exception:
        return False


def uzum_headers():
    """Uzum public API uchun header.

    MUHIM: api.uzum.uz/api/v2/product/* — OCHIQ (public) API, token shart emas.
    Eskirgan/yaroqsiz token yuborilsa API bo'sh javob qaytaradi (buziladi).
    Shuning uchun token faqat AMAL QILSA qo'shamiz — aks holda Authorization'siz
    yuboramiz va public ma'lumot baribir keladi. Bu token eskirsa ham kuzatuv
    to'xtamasligini ta'minlaydi.
    """
    s = load_settings()
    h = {
        "Accept": "application/json",
        "Accept-Language": "ru-RU",
        "User-Agent": "UzumMarket/2.5.0",
        "x-iid": s.get("xiid", "9499b4e3-636a-416e-8c9a-30ecfae50e55"),
    }
    token = s.get("token", "")
    if _token_is_valid(token):
        h["Authorization"] = f"Bearer {token.strip(chr(34))}"
    return h


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
      product_id INTEGER,
      user_id INTEGER DEFAULT 0,
      title TEXT,
      photo TEXT,
      added_at TEXT,
      last_refreshed TEXT,
      weekly_buyers INTEGER,
      weekly_updated_at TEXT,
      PRIMARY KEY (product_id, user_id)
    );
    """)

    # Migration: eski sxemada user_id yo'q edi (product_id PRIMARY KEY)
    cols = [r[1] for r in con.execute("PRAGMA table_info(tracked_products)").fetchall()]
    if "user_id" not in cols:
        # weekly_buyers/weekly_updated_at bo'lmasa avval qo'shamiz
        if "weekly_buyers" not in cols:
            con.execute("ALTER TABLE tracked_products ADD COLUMN weekly_buyers INTEGER")
        if "weekly_updated_at" not in cols:
            con.execute("ALTER TABLE tracked_products ADD COLUMN weekly_updated_at TEXT")
        con.commit()
        # Yangi sxemaga ko'chiramiz (composite PK)
        con.executescript("""
        CREATE TABLE IF NOT EXISTS tracked_products_new (
          product_id INTEGER,
          user_id INTEGER DEFAULT 0,
          title TEXT,
          photo TEXT,
          added_at TEXT,
          last_refreshed TEXT,
          weekly_buyers INTEGER,
          weekly_updated_at TEXT,
          PRIMARY KEY (product_id, user_id)
        );
        INSERT OR IGNORE INTO tracked_products_new
          (product_id, user_id, title, photo, added_at, last_refreshed,
           weekly_buyers, weekly_updated_at)
          SELECT product_id, 0, title, photo, added_at, last_refreshed,
                 weekly_buyers, weekly_updated_at
          FROM tracked_products;
        DROP TABLE tracked_products;
        ALTER TABLE tracked_products_new RENAME TO tracked_products;
        """)

    con.commit()

    # Migration: user_id=0 bo'lgan eski yozuvlarni admin ga ko'chirish
    # (avvalgi versiyada user_id yo'q edi, hammasi 0 bo'lib saqlangan)
    orphan_count = con.execute(
        "SELECT COUNT(*) FROM tracked_products WHERE user_id=0"
    ).fetchone()[0]
    if orphan_count > 0:
        admin_chat_id = 0
        # authorized_users.json dan admin chat_id ni olamiz
        auth_file = DATA_DIR / "authorized_users.json"
        if not auth_file.exists():
            auth_file = BASE_DIR / "authorized_users.json"
        if auth_file.exists():
            try:
                auth_data = json.loads(auth_file.read_text())
                admin_chat_id = auth_data.get("admin_chat_id", 0)
            except Exception:
                pass
        # bot_settings.json dan ham tekshiramiz
        if not admin_chat_id:
            bot_cfg = DATA_DIR / "bot_settings.json"
            if not bot_cfg.exists():
                bot_cfg = BASE_DIR / "bot_settings.json"
            if bot_cfg.exists():
                try:
                    admin_chat_id = json.loads(bot_cfg.read_text()).get("admin_chat_id", 0)
                except Exception:
                    pass
        if admin_chat_id:
            # Avval dublikat bo'ladiganlarni o'chiramiz (admin allaqachon kuzatayotganlar)
            con.execute(
                """DELETE FROM tracked_products WHERE user_id=0
                   AND product_id IN (
                       SELECT product_id FROM tracked_products WHERE user_id=?
                   )""",
                (admin_chat_id,),
            )
            # Qolganlarni admin ga ko'chiramiz
            con.execute(
                "UPDATE OR IGNORE tracked_products SET user_id=? WHERE user_id=0",
                (admin_chat_id,),
            )
            con.commit()
            print(f"✅ Migration: {orphan_count} ta kuzatuv admin ({admin_chat_id}) ga ko'chirildi")

    con.close()


init_db()


def add_tracking(p, user_id: int = 0):
    """Mahsulotni foydalanuvchi kuzatuviga qo'shadi (upsert)."""
    if not p or not p.get("id"):
        return
    now = datetime.utcnow().isoformat()
    photo = extract_photo(p)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO tracked_products
             (product_id, user_id, title, photo, added_at, last_refreshed)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(product_id, user_id) DO UPDATE SET
             title=excluded.title,
             photo=excluded.photo,
             last_refreshed=excluded.last_refreshed""",
        (p.get("id"), user_id, p.get("title"), photo, now, now),
    )
    con.commit()
    con.close()


def update_product_meta(p):
    """Background refresh: mavjud barcha foydalanuvchilar uchun metadata yangilaydi."""
    if not p or not p.get("id"):
        return
    now = datetime.utcnow().isoformat()
    photo = extract_photo(p)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE tracked_products SET title=?, photo=?, last_refreshed=? WHERE product_id=?",
        (p.get("title"), photo, now, p.get("id")),
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


def extract_photo(p):
    """Uzum mahsulot rasmini oladi: photos[0].photo['240'].high
    Uzum API tuzilmasi: photos=[{photo:{'120':{high,low}, '240':{...}, ...}}]"""
    try:
        photos = p.get("photos") or []
        if not photos:
            return ""
        ph = (photos[0] or {}).get("photo") or {}
        # Eski format bilan ham ishlasin (link)
        if not ph:
            return (photos[0] or {}).get("link", {}).get("high", "")
        # O'rta o'lcham afzal: 240 → 480 → 540 → birinchi mavjud
        for size in ("240", "480", "540", "120", "720", "800"):
            if size in ph:
                node = ph[size] or {}
                url = node.get("high") or node.get("low") or ""
                if url:
                    return url
        # Hech biri bo'lmasa birinchi mavjudni ol
        for node in ph.values():
            url = (node or {}).get("high") or (node or {}).get("low") or ""
            if url:
                return url
        return ""
    except Exception:
        return ""


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
        msg = (
            "⚠️ *Uzum token eskirdi!*\n\n"
            "Mahsulotlar yangilanmayapti.\n\n"
            "👇 *Qayta login qilish:*\n"
            "/login — SMS orqali avtomatik login"
        )
        markup = {
            "inline_keyboard": [[
                {"text": "🔐 Login qilish", "callback_data": "do_login"}
            ]]
        }
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "reply_markup": markup},
            timeout=5,
        )
    except Exception:
        pass


def _notify_session_warning(msg: str, cooldown_hours: int = 24):
    """Sessiya muammosi haqida admin ga xabar (kuniga 1 marta)."""
    try:
        cfg_file = DATA_DIR / "bot_settings.json"
        if not cfg_file.exists():
            return
        cfg = json.loads(cfg_file.read_text())
        chat_id = cfg.get("admin_chat_id")
        bot_token = os.environ.get("BOT_TOKEN", "")
        if not chat_id or not bot_token:
            return
        # Spam oldini olish — har cooldown_hours soatda 1 marta
        last = cfg.get("last_session_warning", 0)
        if time.time() - last < cooldown_hours * 3600:
            return
        cfg["last_session_warning"] = time.time()
        cfg_file.write_text(json.dumps(cfg, indent=2))
        markup = {
            "inline_keyboard": [[
                {"text": "🔐 Login qilish", "callback_data": "do_login"}
            ]]
        }
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "reply_markup": markup},
            timeout=5,
        )
    except Exception:
        pass


def _check_session_health():
    """Sessiya fayli yoshini tekshiradi — eskirsa ogohlantiradi."""
    session_file = DATA_DIR / "uzum_session.json"
    if not session_file.exists():
        _notify_session_warning(
            "❌ *Uzum sessiyasi topilmadi!*\n\n"
            "Haftalik xaridorlar ma'lumoti olinmayapti.\n\n"
            "👇 Qayta login qiling:\n/login — SMS orqali",
            cooldown_hours=24,
        )
        return
    age_days = (time.time() - session_file.stat().st_mtime) / 86400
    if age_days > 390:
        _notify_session_warning(
            f"❌ *Uzum sessiyasi eskirdi!*\n\n"
            f"Sessiya {int(age_days)} kun oldin yangilangan.\n"
            "Haftalik ma'lumotlar olinmayapti.\n\n"
            "👇 Qayta login qiling:\n/login — SMS orqali",
            cooldown_hours=24,
        )
    elif age_days > 350:
        _notify_session_warning(
            f"⏳ *Uzum sessiyasi tez eskiradi!*\n\n"
            f"Sessiya {int(age_days)} kun oldin yangilangan "
            f"(~{int(398 - age_days)} kun qoldi).\n\n"
            "Hozircha hammasi ishlayapti, lekin tez orada "
            "login tavsiya qilinadi.\n\n"
            "👇 Yangilash uchun:\n/login — SMS orqali",
            cooldown_hours=72,  # 3 kunda 1 marta
        )
    else:
        print(f"✅ Sessiya sog'lom: {int(age_days)} kun eski, ~{int(398 - age_days)} kun qoldi")


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


@app.route("/api/upload-session", methods=["POST"])
def upload_session():
    """POST /api/upload-session?secret=<SECRET_KEY> — uzum_session.json ni yuklaish."""
    secret = os.environ.get("TOKEN_UPDATE_SECRET", "")
    if not secret or request.args.get("secret") != secret:
        return jsonify({"error": "Ruxsat yo'q"}), 403
    data = request.get_data()
    if len(data) < 100:
        return jsonify({"error": "Fayl bo'sh yoki juda kichik"}), 400
    session_path = DATA_DIR / "uzum_session.json"
    session_path.write_bytes(data)
    return jsonify({"ok": True, "size": len(data), "path": str(session_path)})


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
    user_id = int(request.args.get("user_id", 0))
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

    # Snapshot saqlash + foydalanuvchi kuzatuviga qo'shish
    store_snapshot(p)
    add_tracking(p, user_id)

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
        "photo": extract_photo(p),
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
                "photo": extract_photo(p),
            })
            time.sleep(0.15)  # rate limit
        except Exception as e:
            print(f"Error fetching {pid}: {e}")
    return jsonify({"products": results})


def stock_sold_delta(con, pid, days_ago):
    """Stock kamayishi asosida sotuvni hisoblaydi (orders_amount o'rniga).

    Uzum API ordersAmount ni real vaqtda yangilamaydi, lekin total_stock
    har snaphotda yangilanadi. Shuning uchun stock delta = sotuvlar.
    """
    # Window ichidagi snapshotlar (distinct taken_at bo'yicha total_stock)
    rows = con.execute(
        "SELECT taken_at, MIN(total_stock) as ts FROM snapshots "
        "WHERE product_id=? AND taken_at >= datetime('now', ?) "
        "GROUP BY taken_at ORDER BY taken_at ASC",
        (pid, f"-{days_ago} days"),
    ).fetchall()

    if not rows:
        return None

    # Window boshidan oldingi oxirgi snapshot (baseline)
    baseline = con.execute(
        "SELECT total_stock FROM snapshots "
        "WHERE product_id=? AND taken_at < datetime('now', ?) "
        "ORDER BY taken_at DESC LIMIT 1",
        (pid, f"-{days_ago} days"),
    ).fetchone()

    stocks = [r[1] for r in rows]
    if baseline:
        stocks = [baseline[0]] + stocks

    if len(stocks) < 2:
        return None

    sold = 0
    for i in range(1, len(stocks)):
        delta = stocks[i] - stocks[i - 1]
        if delta < 0:
            sold += abs(delta)
    return sold


@app.route("/api/tracked")
def list_tracked():
    """Kuzatilayotgan mahsulotlar — faqat shu foydalanuvchiniki."""
    user_id = int(request.args.get("user_id", 0))
    con = sqlite3.connect(DB_PATH)
    tracked = con.execute(
        "SELECT product_id, title, photo, added_at, last_refreshed, weekly_buyers, weekly_updated_at "
        "FROM tracked_products WHERE user_id=? ORDER BY last_refreshed DESC",
        (user_id,),
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

        # Stock delta asosida sotuvlar (ordersAmount real vaqtda yangilanmaydi)
        s_1d  = stock_sold_delta(con, pid, 1)
        s_7d  = stock_sold_delta(con, pid, 7)
        s_30d = stock_sold_delta(con, pid, 30)

        products.append({
            "id": pid,
            "title": title,
            "photo": photo,
            "addedAt": added_at,
            "lastSeen": last_seen,
            "ordersNow": orders_now,
            "stockNow": stock_now,
            "today": s_1d,
            "last7d": s_7d,
            "last30d": s_30d,
            "weeklyBuyers": weekly_buyers,
            "weeklyUpdatedAt": weekly_updated_at,
        })

    con.close()
    products.sort(key=lambda x: (x.get("weeklyBuyers") or x.get("today") or 0), reverse=True)
    return jsonify({"products": products, "count": len(products)})


@app.route("/api/backfill-photos", methods=["POST", "GET"])
def backfill_photos():
    """Bir martalik: photo bo'sh bo'lgan kuzatuvdagi mahsulotlarga rasm to'ldiradi."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT DISTINCT product_id FROM tracked_products WHERE photo IS NULL OR photo=''"
    ).fetchall()
    con.close()
    updated, failed = 0, 0
    for (pid,) in rows:
        p = fetch_product(int(pid))
        photo = extract_photo(p) if p else ""
        if photo:
            con = sqlite3.connect(DB_PATH)
            con.execute(
                "UPDATE tracked_products SET photo=? WHERE product_id=?",
                (photo, pid),
            )
            con.commit()
            con.close()
            updated += 1
        else:
            failed += 1
    return jsonify({"updated": updated, "failed": failed, "total": len(rows)})


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
        from weekly_scraper import fetch_weekly_parallel
        # Max 50 ta — 3 ta parallel brauzer bilan ~1.5 daqiqa
        ids_int = [int(x) for x in ids[:50]]
        data = fetch_weekly_parallel(ids_int, workers=3, delay=0.3)
        return jsonify({"weekly": {str(k): v for k, v in data.items()}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/track", methods=["POST"])
def track_batch():
    """Tanlangan mahsulotlarni foydalanuvchi kuzatuviga qo'shish."""
    body    = request.json or {}
    ids     = body.get("ids", [])
    user_id = int(body.get("user_id", 0))
    if not isinstance(ids, list):
        return jsonify({"error": "ids ro'yxat bo'lishi kerak"}), 400
    added = 0
    for pid in ids[:50]:
        try:
            p = fetch_product(int(pid))
            if p:
                store_snapshot(p)
                add_tracking(p, user_id)
                added += 1
            time.sleep(0.15)
        except Exception as e:
            print(f"Track error {pid}: {e}")
    return jsonify({"added": added})


@app.route("/api/untrack/<int:pid>", methods=["DELETE"])
def untrack(pid):
    user_id = int(request.args.get("user_id", 0))
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM tracked_products WHERE product_id=? AND user_id=?", (pid, user_id))
    con.commit()
    con.close()
    return jsonify({"ok": True})


def refresh_all_tracked(fetch_weekly=True, progress_cb=None):
    """Barcha kuzatilayotgan mahsulotlarni Uzum API dan yangilaydi.

    fetch_weekly=True bo'lsa, "Bu hafta X kishi" ma'lumotini ham Playwright orqali oladi.
    progress_cb(done, total) — haftalik scraping jarayonida chaqiriladi.
    """
    con = sqlite3.connect(DB_PATH)
    ids = [r[0] for r in con.execute("SELECT DISTINCT product_id FROM tracked_products").fetchall()]
    con.close()

    print(f"🔄 Avto-yangilash: {len(ids)} ta mahsulot")
    refreshed = 0
    for pid in ids:
        try:
            p = fetch_product(pid)
            if p:
                store_snapshot(p)
                update_product_meta(p)  # barcha foydalanuvchilar uchun metadata yangilanadi
                refreshed += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"  ❌ {pid}: {e}")
    print(f"✅ Yangilandi: {refreshed}/{len(ids)}")

    # Haftalik xaridorlar sonini ham yangilaymiz (sekinroq)
    if fetch_weekly and ids:
        try:
            from weekly_scraper import fetch_weekly_parallel
            print(f"📊 Haftalik ma'lumot yuklanmoqda ({len(ids)} ta, 3 parallel)...")
            weekly_data = fetch_weekly_parallel(ids, workers=3, delay=0.3, progress_cb=progress_cb)
            now = datetime.utcnow().isoformat()
            con = sqlite3.connect(DB_PATH)
            for pid, count in weekly_data.items():
                if count is not None and count > 0:  # 0 va None eski raqamni o'chirmasin
                    con.execute(
                        "UPDATE tracked_products SET weekly_buyers=?, weekly_updated_at=? WHERE product_id=?",
                        (count, now, pid),
                    )
            con.commit()
            con.close()
            ok = sum(1 for v in weekly_data.values() if v is not None and v > 0)
            total = len(ids)
            print(f"✅ Haftalik yangilandi: {ok}/{total}")
            # Agar ko'pchiligi muvaffaqiyatsiz bo'lsa — sessiya muammosi
            if total >= 3 and ok == 0:
                _notify_session_warning(
                    "⚠️ *Haftalik ma'lumotlar olinmadi!*\n\n"
                    f"0/{total} ta mahsulot uchun scraping ishlamadi.\n"
                    "Sessiya muammosi bo'lishi mumkin.\n\n"
                    "👇 Tekshirish uchun:\n/login — SMS orqali",
                    cooldown_hours=24,
                )
            elif total >= 5 and ok < total * 0.3:
                _notify_session_warning(
                    f"⚠️ *Haftalik ma'lumotlarda muammo!*\n\n"
                    f"Faqat {ok}/{total} ta mahsulot olindi.\n"
                    "Sessiya muammosi bo'lishi mumkin.\n\n"
                    "👇 Tekshirish uchun:\n/login — SMS orqali",
                    cooldown_hours=24,
                )
        except Exception as e:
            print(f"⚠️ Haftalik yangilash xato: {e}")

    return refreshed


# Jonli yangilash holati (refresh tugmasi uchun)
_refresh_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "phase": "",        # "products" | "weekly" | "done"
    "started_at": 0,
    "finished_at": 0,
}
_refresh_lock = threading.Lock()


def _run_live_refresh(fetch_weekly=False):
    """Background thread: mahsulot ma'lumotini yangilaydi (stock, orders).
    fetch_weekly=True faqat background avtomatik chaqiruvda ishlatiladi.
    """
    try:
        refresh_all_tracked(fetch_weekly=fetch_weekly)
    except Exception as e:
        print(f"⚠️ Live refresh xato: {e}")
    finally:
        _refresh_state.update({
            "running": False,
            "phase": "done",
            "finished_at": time.time(),
        })


@app.route("/api/refresh", methods=["POST"])
def refresh_endpoint():
    """Tezkor yangilash: stock/orders yangilanadi, haftalik scraping EMAS.
    weekly=1 parametri bilan haftalik scraping ham qo'shiladi (faqat background uchun).
    """
    fetch_weekly = request.args.get("weekly", "0") == "1"
    with _refresh_lock:
        if _refresh_state["running"]:
            return jsonify({"started": False, **_refresh_state})
        _refresh_state.update({
            "running": True, "done": 0, "total": 0,
            "phase": "products", "started_at": time.time(), "finished_at": 0,
        })
    threading.Thread(target=_run_live_refresh, args=(fetch_weekly,), daemon=True).start()
    return jsonify({"started": True, **_refresh_state})


@app.route("/api/refresh-status")
def refresh_status_endpoint():
    return jsonify(dict(_refresh_state))


@app.route("/api/debug-weekly")
def debug_weekly():
    """DB dagi weekly_buyers ni ko'rish uchun (vaqtinchalik debug)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT product_id, weekly_buyers, weekly_updated_at FROM tracked_products ORDER BY weekly_buyers DESC NULLS LAST LIMIT 20"
    ).fetchall()
    con.close()
    return jsonify([{"pid": r[0], "weekly": r[1], "updated": r[2]} for r in rows])


def get_color_sales_delta(pid, days=7):
    """Snapshot delta asosida rang bo'yicha aniq sotuvni hisoblaydi.

    Mantiq: har sku_id uchun sku_stock vaqt seriyasidan
    faqat kamayishlarni (sotuvlarni) yig'amiz.
    Restock (stock oshishi) o'tkazib yuboriladi.
    """
    con = sqlite3.connect(DB_PATH)

    # Window ichidagi snapshotlar
    rows = con.execute(
        "SELECT sku_id, color, size, sku_stock, taken_at "
        "FROM snapshots "
        "WHERE product_id=? AND taken_at >= datetime('now', ?) "
        "ORDER BY sku_id, taken_at ASC",
        (pid, f"-{days} days"),
    ).fetchall()

    # Window boshidan oldingi oxirgi snapshot (baseline) — har SKU uchun bittasi
    baseline_rows = con.execute(
        "SELECT sku_id, color, size, sku_stock, taken_at "
        "FROM snapshots "
        "WHERE product_id=? AND taken_at < datetime('now', ?) "
        "ORDER BY taken_at DESC LIMIT 200",
        (pid, f"-{days} days"),
    ).fetchall()

    # Jami snapshotlar soni
    total_snaps = (con.execute(
        "SELECT COUNT(DISTINCT taken_at) FROM snapshots WHERE product_id=?", (pid,)
    ).fetchone() or [0])[0]

    con.close()

    # Baseline: har SKU uchun eng yangi (DESC tartibda keldi)
    baseline_by_sku = {}
    for sku_id, color, size, stock, ts in baseline_rows:
        if sku_id not in baseline_by_sku:
            baseline_by_sku[sku_id] = stock

    # Window qatorlarini SKU bo'yicha guruhlaymiz
    from collections import defaultdict, OrderedDict
    sku_series = defaultdict(list)
    sku_info = {}
    for sku_id, color, size, stock, ts in rows:
        sku_series[sku_id].append(stock)
        sku_info[sku_id] = (color or "", size or "")

    if not sku_series:
        return {
            "method": "none",
            "colors": [],
            "totalSold": 0,
            "snapshotCount": total_snaps,
            "days": days,
            "note": "Snapshot yo'q — kuzatuvga qo'shilgan vaqtdan hisob boshlanadi",
        }

    results = []
    total_sold = 0

    for sku_id, series in sku_series.items():
        color, size = sku_info[sku_id]

        # Baseline mavjud bo'lsa seriyaning boshiga qo'shamiz
        full = list(series)
        if sku_id in baseline_by_sku:
            full = [baseline_by_sku[sku_id]] + full

        # Faqat stock kamayishlari = sotuvlar
        sold = 0
        for i in range(1, len(full)):
            delta = full[i] - full[i - 1]
            if delta < 0:
                sold += abs(delta)

        current_stock = series[-1] if series else 0
        results.append({
            "skuId": sku_id,
            "color": color,
            "size": size,
            "sold": sold,
            "currentStock": current_stock,
        })
        total_sold += sold

    # Foizlarni qo'shamiz va saralaymiz
    for r in results:
        r["soldPct"] = round(r["sold"] / total_sold * 100, 1) if total_sold else 0
    results.sort(key=lambda x: -x["sold"])

    snap_count = len({r[4] for r in rows})  # distinct taken_at in window

    if total_sold == 0 and snap_count < 2:
        note = f"Ma'lumot to'planmoqda — {snap_count} ta snapshot (kamida 2 ta kerak)"
    elif total_sold == 0:
        note = f"So'nggi {days} kunda sotuv aniqlanmadi ({snap_count} ta o'lchov)"
    else:
        note = f"{days} kunlik aniq sotuv — {snap_count} ta o'lchov asosida"

    return {
        "method": "delta",
        "colors": results,
        "totalSold": total_sold,
        "snapshotCount": snap_count,
        "totalSnapshotCount": total_snaps,
        "days": days,
        "note": note,
    }


@app.route("/api/product/<int:pid>/color-sales")
def color_sales(pid):
    """Rang bo'yicha sotuv (snapshot delta asosida)."""
    days = min(int(request.args.get("days", 7)), 90)
    result = get_color_sales_delta(pid, days)
    return jsonify(result)


def try_auto_refresh_token() -> bool:
    """Refresh token yordamida yangi access token olishga harakat qiladi.
    Muvaffaqiyatli bo'lsa True, bo'lmasa False qaytaradi."""
    try:
        s = load_settings()
        refresh_token = s.get("refresh_token", "")
        if not refresh_token:
            print("⚠️ Refresh token yo'q — qayta login kerak")
            return False

        # Uzum auth SDK refresh endpoint
        # JWT iss (issuer) dan bazani aniqlaymiz
        base_url = "https://id.uzum.uz"
        try:
            import base64 as _b64
            raw = s.get("token", "").strip('"')
            if '.' in raw:
                payload_b64 = raw.split('.')[1]
                pad = 4 - len(payload_b64) % 4
                if pad != 4:
                    payload_b64 += '=' * pad
                payload = json.loads(_b64.b64decode(payload_b64))
                iss = payload.get("iss", "")
                if iss.startswith("http"):
                    from urllib.parse import urlparse
                    parsed = urlparse(iss)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

        endpoints = [
            f"{base_url}/api/oauth/v1/token",
            f"{base_url}/oauth/token",
            f"{base_url}/api/v1/token",
            "https://auth.uzum.uz/api/oauth/v1/token",
            "https://auth.uzum.uz/oauth/token",
        ]

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "UzumMarket/2.5.0",
        }
        body = f"grant_type=refresh_token&refresh_token={refresh_token.strip(chr(34))}"

        for endpoint in endpoints:
            try:
                r = requests.post(endpoint, data=body, headers=headers, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    new_token = data.get("access_token") or data.get("token", "")
                    new_refresh = data.get("refresh_token", "")
                    if new_token and len(new_token) > 50:
                        s["token"] = new_token
                        if new_refresh:
                            s["refresh_token"] = new_refresh
                        # Yangi expiry vaqtini hisoblash
                        expires_in = data.get("expires_in", 14400)  # default 4 soat
                        s["token_expires_at"] = int(time.time()) + int(expires_in)
                        save_settings(s)
                        print(f"✅ Token avtomatik yangilandi (endpoint: {endpoint})")
                        # Admin ga xabar
                        _notify_token_refreshed()
                        return True
            except Exception as ex:
                continue

        print("❌ Barcha refresh endpoint'lar ishlamadi")
        return False

    except Exception as e:
        print(f"Auto refresh xato: {e}")
        return False


def _notify_token_refreshed():
    """Token muvaffaqiyatli yangilanganda admin ga xabar."""
    try:
        cfg_file = DATA_DIR / "bot_settings.json"
        if not cfg_file.exists():
            return
        cfg = json.loads(cfg_file.read_text())
        chat_id = cfg.get("admin_chat_id")
        bot_token = os.environ.get("BOT_TOKEN", "")
        if not chat_id or not bot_token:
            return
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": "🔄 Uzum token avtomatik yangilandi ✅"},
            timeout=5,
        )
    except Exception:
        pass


def _is_token_expiring_soon(threshold_minutes: int = 60) -> bool:
    """Token kelgusi N daqiqada eskiradimi?"""
    try:
        s = load_settings()
        # settings.json dan expires_at
        expires_at = s.get("token_expires_at", 0)
        if expires_at:
            return time.time() > expires_at - threshold_minutes * 60

        # Yoki JWT dan o'qiymiz
        import base64 as _b64
        raw = s.get("token", "").strip('"')
        if '.' in raw:
            payload_b64 = raw.split('.')[1]
            pad = 4 - len(payload_b64) % 4
            if pad != 4:
                payload_b64 += '=' * pad
            payload = json.loads(_b64.b64decode(payload_b64))
            exp = payload.get("exp", 0)
            if exp:
                return time.time() > exp - threshold_minutes * 60
    except Exception:
        pass
    return False


def start_background_refresher():
    """Snapshot: har 1 soat. Haftalik scraper: har 6 soat.
    Token yangilash: expiry ga 60 daqiqa qolganda avtomatik."""
    import threading

    def loop():
        time.sleep(60)  # startup dan keyin 1 daqiqa kutib turish
        cycle = 0
        while True:
            try:
                # Sessiya fayli yoshini kuniga 1 marta tekshiramiz (24-soatlik cycle)
                if cycle % 24 == 0:
                    _check_session_health()

                fetch_weekly = (cycle % 6 == 0)  # har 6 soatda bir marta
                refresh_all_tracked(fetch_weekly=fetch_weekly)
            except Exception as e:
                print(f"Background refresh error: {e}")
            time.sleep(3600)  # har 1 soatda
            cycle += 1

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    print("⏰ Background: snapshot har 1 soat, haftalik har 6 soat, sessiya monitoringi yoqilgan")



@app.route("/api/sales-history")
def sales_history():
    """Sotuvlar tarixi - bugungi barcha sotilgan mahsulotlar."""
    try:
        import sqlite3
        date = request.args.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
        
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        
        # Bugungi barcha snapshotlarni product_id, color bo'yicha guruhlash
        rows = con.execute("""
            SELECT 
                product_id,
                title,
                color,
                MIN(orders_amount) as first_orders,
                MAX(orders_amount) as last_orders,
                MAX(orders_amount) - MIN(orders_amount) as sold_count,
                MIN(taken_at) as first_check,
                MAX(taken_at) as last_check
            FROM snapshots
            WHERE date(taken_at) = ?
            GROUP BY product_id, color
            HAVING sold_count > 0
            ORDER BY last_check DESC
        """, (date,))
        
        sales = []
        for row in rows:
            # Har bir sku uchun alohida qator
            count = int(row['sold_count'])
            last_time = row['last_check'].split('T')[1][:5] if 'T' in row['last_check'] else row['last_check'][11:16]
            
            sales.append({
                "product_id": row['product_id'],
                "title": row['title'],
                "color": row['color'] or "",
                "count": count,
                "time": last_time
            })
        
        con.close()
        
        total = sum(s['count'] for s in sales)
        unique_products = len(set(s['product_id'] for s in sales))
        
        return jsonify({
            "success": True,
            "date": date,
            "total": total,
            "unique_products": unique_products,
            "sales": sales
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/product/<int:pid>/sales-history")
def product_sales_history(pid):
    """Mahsulot sotuvlari tarixi — har SKU uchun stock kamayish alohida event.

    Har sku_id bo'yicha consecutive snapshot juftida sku_stock kamayganda
    sotuv deb hisoblanadi. Rang va o'lcham ham qaytariladi.
    Returns: [{time, qty, color, size, isoTime}, ...]  sorted desc.
    """
    days = min(int(request.args.get("days", 7)), 90)
    con = sqlite3.connect(DB_PATH)

    # Window ichidagi snapshotlar — har SKU uchun
    rows = con.execute(
        "SELECT sku_id, color, size, sku_stock, taken_at FROM snapshots "
        "WHERE product_id=? AND taken_at >= datetime('now', ?) "
        "ORDER BY sku_id, taken_at ASC",
        (pid, f"-{days} days"),
    ).fetchall()

    # Baseline — har SKU uchun window oldidan oxirgi qiymat
    baseline_rows = con.execute(
        "SELECT sku_id, color, size, sku_stock FROM snapshots "
        "WHERE product_id=? AND taken_at < datetime('now', ?) "
        "ORDER BY taken_at DESC LIMIT 500",
        (pid, f"-{days} days"),
    ).fetchall()

    con.close()

    if not rows:
        return jsonify({"events": [], "total": 0, "days": days,
                        "note": "Snapshot yo'q — kuzatuvga qo'shilgan vaqtdan hisob boshlanadi"})

    # Baseline: har SKU uchun eng yangi (DESC keldi)
    baseline_by_sku = {}
    for sku_id, color, size, stock in baseline_rows:
        if sku_id not in baseline_by_sku:
            baseline_by_sku[sku_id] = (stock, color or "", size or "")

    # SKU bo'yicha guruhlab seriya tuzish
    from collections import defaultdict
    sku_series = defaultdict(list)   # sku_id -> [(taken_at, stock)]
    sku_info   = {}                  # sku_id -> (color, size)
    for sku_id, color, size, stock, taken_at in rows:
        sku_series[sku_id].append((taken_at, stock))
        sku_info[sku_id] = (color or "", size or "")

    def fmt_dt(iso):
        try:
            dt = datetime.fromisoformat(iso)
            months = ["","yan","fev","mar","apr","may","iyn","iyl","avg","sen","okt","noy","dek"]
            return f"{dt.day}-{months[dt.month]}", dt.strftime("%H:%M")
        except Exception:
            return (iso or "")[:10], (iso or "")[11:16]

    events = []
    for sku_id, series in sku_series.items():
        color, size = sku_info[sku_id]

        # Baseline qo'shish
        full = list(series)
        if sku_id in baseline_by_sku:
            bl_stock, bl_color, bl_size = baseline_by_sku[sku_id]
            full = [(None, bl_stock)] + full
            if not color:
                color = bl_color
            if not size:
                size = bl_size

        for i in range(1, len(full)):
            delta = full[i][1] - full[i - 1][1]
            if delta < 0:
                iso = full[i][0]
                day_str, time_str = fmt_dt(iso) if iso else ("—", "—")
                events.append({
                    "isoTime": iso or "",
                    "day":     day_str,
                    "time":    time_str,
                    "qty":     abs(delta),
                    "color":   color,
                    "size":    size,
                })

    events.sort(key=lambda e: e["isoTime"], reverse=True)
    total = sum(e["qty"] for e in events)
    return jsonify({"events": events, "total": total, "days": days,
                    "note": f"{days} kun ichida {len(events)} ta sotuv hodisasi"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_background_refresher()
    print(f"🚀 Uzum Analitika serveri http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
