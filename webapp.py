from flask import Flask, render_template, request, session, url_for, redirect, jsonify, g
from datetime import timedelta
from scraper.amazon_scraper import get_product_details
from flask_caching import Cache
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import sqlite3
import os
import re
from datetime import date, datetime as dt
import uuid
from urllib.parse import urlparse
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

app.config['APPLICATION_ROOT'] = os.environ.get('APPLICATION_ROOT', '/')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'local')


class PrefixMiddleware:
    def __init__(self, app, prefix=""):
        self.app = app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if not self.prefix:
            return self.app(environ, start_response)

        path = environ.get("PATH_INFO", "") or ""
        
        # Allow static files to pass through without prefix
        if path.startswith("/static/"):
            return self.app(environ, start_response)
        
        # Allow access to root "/" without prefix
        if path == "/":
            environ["SCRIPT_NAME"] = ""
            environ["PATH_INFO"] = "/"
            return self.app(environ, start_response)
        
        if path == self.prefix:
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = "/"
            return self.app(environ, start_response)

        if path.startswith(self.prefix + "/"):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            return self.app(environ, start_response)

        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found"]


# x_for=1 trusts exactly one proxy hop (Cloudflare). If traffic can reach nginx
# directly (bypassing Cloudflare), an attacker can spoof X-Forwarded-For.
# The definitive fix for that is in nginx — always overwrite the header before
# passing it upstream:
#   proxy_set_header X-Forwarded-For $remote_addr;
#   proxy_set_header X-Forwarded-Proto $scheme;
app.wsgi_app = PrefixMiddleware(
    ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0),
    prefix=os.environ.get('APP_PREFIX', ''),
)



if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY environment variable is required")


cache_folder = "cache_dir"
cache_path = os.path.join(cache_folder)
if not os.path.exists(cache_path):
    os.makedirs(cache_path)


cache = Cache(app, config={
    "CACHE_TYPE": "FileSystemCache",
    "CACHE_DIR": cache_path,
    "CACHE_DEFAULT_TIMEOUT": 900,
})

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


# Initialize CSRF protection
csrf = CSRFProtect()
csrf.init_app(app)

# ---------------------------------------------------------------------------
# Rate limiting — Cloudflare forwards real IP in X-Forwarded-For.
# ProxyFix (already configured above) rewrites REMOTE_ADDR before Limiter
# reads it, so get_remote_address() sees the correct client IP.
# ---------------------------------------------------------------------------
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],          # No global limit; apply per-route only.
    storage_uri="memory://",    # In-process store; swap for Redis in production.
)

# ---------------------------------------------------------------------------
# SSRF guard — only accept genuine Amazon Italy product URLs.
# Valid pattern: https://www.amazon.it/*/dp/<ASIN>[/...]
# ASIN is always exactly 10 uppercase alphanumeric characters.
# ---------------------------------------------------------------------------
_ALLOWED_AMAZON_HOSTS = {"www.amazon.it", "amazon.it"}
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

def _validate_amazon_url(url: str) -> bool:
    """Return True only if the URL is an amazon.it product page."""
    try:
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.hostname not in _ALLOWED_AMAZON_HOSTS:
        return False

    # Path must contain /dp/<ASIN>
    match = re.search(r"/dp/([A-Za-z0-9]{10})(?:/|$|\?)", parsed.path)
    if not match:
        return False

    asin = match.group(1).upper()
    return bool(_ASIN_RE.match(asin))


# ---------------------------------------------------------------------------
# Security headers — applied to every response.
# ---------------------------------------------------------------------------
@app.after_request
def set_security_headers(response):
    # Block clickjacking
    response.headers["X-Frame-Options"] = "DENY"
    # Block MIME sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Enforce HTTPS for 1 year (Cloudflare terminates TLS, but belt-and-suspenders)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP: templates use inline <script> blocks, so 'unsafe-inline' is required for now.
    # CDNs: cdn.jsdelivr.net (Chart.js, SweetAlert2), fonts.googleapis.com/gstatic.com.
    # Images: Amazon serves product photos from m.media-amazon.com and related origins.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
        "font-src fonts.gstatic.com; "
        "img-src 'self' data: *.amazon.it *.amazon.com m.media-amazon.com "
        "images-na.ssl-images-amazon.com; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    return response


# ---------------------------------------------------------------------------
# Cookie security — SESSION_COOKIE_SECURE requires HTTPS.
# Set SESSION_COOKIE_INSECURE=1 in your local .env to allow plain HTTP dev.
# ---------------------------------------------------------------------------
_insecure_cookie = os.environ.get("SESSION_COOKIE_INSECURE", "").lower() in ("1", "true", "yes")
app.config["SESSION_COOKIE_SECURE"] = not _insecure_cookie
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf)


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


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
@limiter.limit("10 per minute")
def query():
    query_url = request.form["url_query"].strip()

    if not _validate_amazon_url(query_url):
        return redirect(url_for("error_page", error_code="-4"))

    cache_key = f"scrape:{query_url}"
    cached_result = cache.get(cache_key)
    if cached_result is None:
        cached_result = get_product_details(query_url)
        if cached_result[0] == "0":
            cache.set(cache_key, cached_result)

    err_id, query_ASIN, query_name, query_price_whole, query_price_fraction, query_discount, query_img = cached_result
    if err_id != "0":
        return redirect(url_for("error_page",
                                error_code = err_id))

    # Save/update product so product page can be served via GET (PRG pattern)
    db = get_db()
    cursor = db.cursor()
    price_text = f"{query_price_whole}{query_price_fraction}"
    try:
        price = float(price_text.replace(",", "."))
    except (TypeError, ValueError):
        price = None
    cursor.execute(
        '''INSERT OR REPLACE INTO products (asin, name, price, discount, img_src)
           VALUES (?, ?, ?, ?, ?)''',
        (query_ASIN, query_name, price, query_discount, query_img)
    )
    db.commit()

    # POST/Redirect/GET avoids browser "resubmit form" prompt on back/refresh
    return redirect(url_for("product_page", asin=query_ASIN))


@app.route("/product/<asin>")
def product_page(asin):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT asin, name, price, discount, img_src FROM products WHERE asin = ?', (asin,))
    row = cursor.fetchone()

    if not row:
        return redirect(url_for("error_page", error_code="-3"))

    if row["price"] is None:
        whole, fraction = "Price not found", ""
    else:
        price_value = float(row["price"])
        price_str = f"{price_value:.2f}".replace(".", ",")
        whole, fraction = price_str.split(",")
        whole = f"{whole},"

    return render_template(
        "product.html",
        error_code="0",
        ASIN=row["asin"],
        name=row["name"],
        price_whole=whole,
        price_fraction=fraction,
        discount=row["discount"],
        img_src=row["img_src"]
    )
    
@app.route("/bookmark", methods=["POST"])
@limiter.limit("30 per minute")
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
    try:
        price = float(price_text.replace(",", "."))
    except (TypeError, ValueError):
        return jsonify({"status": "price_not_found"})
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

    cursor.execute("SELECT price, date FROM graph_data WHERE asin = ? ORDER BY date ASC", (asin,))
    rows = cursor.fetchall()

    prices = [float(row["price"]) for row in rows]
    # Ensure dates are strings (ISO) for JSON serialization
    dates = [str(row["date"]) for row in rows]
    
    return jsonify({
        "prices": prices,
        "dates": dates
    })
    
    

@app.route("/unbook", methods=["POST"])
@limiter.limit("30 per minute")
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
    app.run(debug=False)

