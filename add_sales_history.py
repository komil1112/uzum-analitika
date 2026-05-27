# app.py ga sales-history endpoint qo'shish
import re

with open('/tmp/uzum-analitika/app.py', 'r') as f:
    content = f.read()

# Yangi endpoint
new_endpoint = '''
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
'''

# Oxirgi @app.route dan keyin qo'shish
# app.run dan oldin qo'shamiz
if "if __name__" in content:
    content = content.replace("if __name__", new_endpoint + "\n\nif __name__")
else:
    content += new_endpoint

with open('/tmp/uzum-analitika/app.py', 'w') as f:
    f.write(content)

print("✅ /api/sales-history endpoint qo'shildi")
