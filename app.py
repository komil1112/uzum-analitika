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
DB_PATH = BASE_DIR / "uzum.db"

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)

# ----- Uzum credentials (sessiya tokeni — vaqti tugasa yangilab turish kerak) -----
SETTINGS_FILE = BASE_DIR / "settings.json"


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
    """)
    con.commit()
    con.close()


init_db()


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
    r = requests.get(f"https://api.uzum.uz/api/v2/product/{pid}", headers=uzum_headers(), timeout=10)
    if r.status_code != 200:
        return None
    return r.json().get("payload", {}).get("data")


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

    # Snapshot saqlash (tarix uchun)
    store_snapshot(p)

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
            store_snapshot(p)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Uzum Analitika serveri http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
