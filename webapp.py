from flask import Flask, render_template, request, session, url_for, redirect, jsonify, g
from datetime import timedelta
from scraper.amazon_scraper import get_product_details
import sqlite3
import os
from datetime import date, datetime as dt
import uuid

app = Flask(__name__)
app.secret_key = "123 stella"
# Change secret key in prod :)

app.permanent_session_lifetime = timedelta(days=3650)
#Cookies can get removed by the user, otherwise they're semi-permanent

#We create a global database (all the bookmarked products shared between the users)
folder = 'database_dir'
path = os.path.join(folder, 'bookmarks.db')

if not os.path.exists(folder):
    os.makedirs(folder)


# Per-request database connection helpers
def get_db():
    if 'db' not in g:
        # Use a simple sqlite3 connection stored on the request context
        g.db = sqlite3.connect(path)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()
# Reset DB schema: remove old session-based tables and create new schema
connection = sqlite3.connect(path)
cursor = connection.cursor()


@app.route("/")
def home():
    session.permanent = True
    # Ensure a stable session identifier for anonymous users
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())

    db = get_db()
    cursor = db.cursor()

    # Load bookmarks for this session_id from the DB
    cursor.execute(
        "SELECT p.asin, p.name, p.price, p.discount, p.img_src "
        "FROM products p JOIN user_bookmarks ub ON p.asin = ub.asin "
        "WHERE ub.session_id = ? ORDER BY ub.created_at DESC",
        (session['session_id'],)
    )
    rows = cursor.fetchall()

    bookmarks = []
    for r in rows:
        price_text = str(r['price']) if r['price'] is not None else ""
        bookmarks.append({
            "img_src": r['img_src'] or "N/A",
            "ASIN": r['asin'],
            "name": r['name'],
            "price": price_text,
            "discount": r['discount']
        })

    return render_template("main_page.html", bookmarks = bookmarks)

@app.route("/query", methods=['POST'])
def query():
    query_url = request.form["url_query"]
    err_id, query_ASIN, query_name, query_price_whole, query_price_fraction, query_discount, query_img = get_product_details(query_url)
    if err_id != "0":
        return redirect(url_for("error_page",
                                error_code = err_id))
        
    # Add code for err_id = 1 (Missing image), which is not an actual error
        
        
    return render_template("product.html", 
                           error_code = "0",
                           ASIN = query_ASIN,
                           name = query_name,
                           price_whole = query_price_whole,
                           price_fraction = query_price_fraction,
                           discount = query_discount,
                           img_src = query_img)
    
@app.route("/bookmark", methods=["POST"])
def bookmark_func():
    db = get_db()
    cursor = db.cursor()

    session.permanent = True #Permanent cookies
    MAX_BOOKMARKS = 4 #Limit of Bookmarks for a user

    # Ensure session_id exists
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())

    prod_details = request.get_json() #Gets product details from product.html
    price_text = f"{prod_details.get('price_whole')}{prod_details.get('price_fraction')}"
    price = float(price_text.replace(",", "."))
    # Check if this asin is already bookmarked by this session
    cursor.execute('SELECT COUNT(*) AS cnt FROM user_bookmarks WHERE session_id = ? AND asin = ?',
                   (session['session_id'], prod_details.get('ASIN')))
    exists = cursor.fetchone()['cnt']
    if exists:
        return jsonify({"status": "duplicate"})

    # Enforce per-user bookmark limit before inserting
    cursor.execute('SELECT COUNT(*) AS cnt FROM user_bookmarks WHERE session_id = ?', (session['session_id'],))
    cnt = cursor.fetchone()['cnt']
    if cnt >= MAX_BOOKMARKS:
        return jsonify({"status": "full"})

    # Insert or update product info
    cursor.execute(
        '''INSERT OR REPLACE INTO products (asin, name, price, discount, img_src)
           VALUES (?, ?, ?, ?, ?)''',
        (
            prod_details.get("ASIN"),
            prod_details.get("name"),
            price,
            prod_details.get("discount"),
            prod_details.get("img_src")
        )
    )

    # Add mapping for this session -> asin
    cursor.execute(
        'INSERT INTO user_bookmarks (session_id, asin, created_at) VALUES (?, ?, ?)',
        (session['session_id'], prod_details.get('ASIN'), dt.utcnow().isoformat())
    )

    # Add a point for graphing
    cursor.execute(
        'INSERT INTO graph_data (asin, price, date) VALUES (?, ?, ?)',
        (prod_details.get("ASIN"), price, date.today().isoformat())
    )

    db.commit()

    return jsonify({"status": "ok"})

@app.route("/bookmark_info/<asin>")
def give_bookmark_info(asin):
    db = get_db()
    cursor = db.cursor()

    sql_query = "SELECT price, date FROM graph_data WHERE asin = ?"

    cursor.execute("SELECT price, date FROM graph_data WHERE asin = ? ORDER BY date ASC", (asin,))
    rows = cursor.fetchall()

    prices = [float(row["price"]) for row in rows]
    # Ensure dates are strings (ISO) for JSON serialization
    dates = [str(row["date"]) for row in rows]
    
    return jsonify({
        "prices": prices,
        "dates": dates
    })
    
    

@app.route("/unbook", methods = ["POST"])
def unbook_func():
    # Remove mapping only for this anonymous session; delete product when unused
    db = get_db()
    cursor = db.cursor()

    # Ensure session_id exists
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())

    data = request.get_json(silent=True)
    if isinstance(data, str):
        asin_to_delete = data
    elif isinstance(data, dict):
        asin_to_delete = data.get("ASIN") or data.get("asin")
    else:
        asin_to_delete = None

    if not asin_to_delete:
        return jsonify({"error": "missing ASIN"}), 400

    # Remove only the mapping for this session
    cursor.execute('DELETE FROM user_bookmarks WHERE session_id = ? AND asin = ?', (session['session_id'], asin_to_delete))

    # If no other mappings exist for this asin, remove product and its graph data
    cursor.execute('SELECT COUNT(*) AS cnt FROM user_bookmarks WHERE asin = ?', (asin_to_delete,))
    cnt = cursor.fetchone()['cnt']
    if cnt == 0:
        cursor.execute('DELETE FROM graph_data WHERE asin = ?', (asin_to_delete,))
        cursor.execute('DELETE FROM products WHERE asin = ?', (asin_to_delete,))

    db.commit()

    return "", 204
    
    
    
@app.route("/error")
def error_page():
    error_id = request.args.get("error_code")
    
    return render_template("error.html", error_code = error_id)
    


if __name__ == '__main__': 
    app.run(debug=True)

