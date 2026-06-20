from flask import Flask, render_template, request, session, url_for, redirect, jsonify, g
from datetime import timedelta
from scraper.amazon_scraper import get_product_details
from flask_caching import Cache
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from email import utils as email_utils
from datetime import date, datetime as dt, timezone
import uuid
from urllib.parse import urlparse
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

load_dotenv()

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
# User accounts — Flask-Login. Bookmarks are owned either by a logged-in
# user (user_id) or, for anonymous visitors, by the session_id cookie.
# ---------------------------------------------------------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class User(UserMixin):
    def __init__(self, id, email, email_verified=False):
        self.id = str(id)
        self.email = email
        self.email_verified = bool(email_verified)


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, email, email_verified FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return User(row["id"], row["email"], row["email_verified"])


def _bookmark_owner():
    """Return (column, value) identifying who owns a bookmark row."""
    if current_user.is_authenticated:
        return "user_id", current_user.id
    session.permanent = True
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    return "session_id", session['session_id']


def _migrate_session_bookmarks(db, user_id):
    """Attach the current anonymous session's bookmarks to the account
    that just logged in/signed up, skipping any asin already saved there."""
    session_id = session.get("session_id")
    if not session_id:
        return
    cursor = db.cursor()
    cursor.execute(
        '''DELETE FROM user_bookmarks WHERE session_id = ? AND asin IN (
               SELECT asin FROM user_bookmarks WHERE user_id = ?
           )''',
        (session_id, user_id)
    )
    cursor.execute(
        "UPDATE user_bookmarks SET user_id = ?, session_id = NULL WHERE session_id = ?",
        (user_id, session_id)
    )
    db.commit()


# ---------------------------------------------------------------------------
# Email sending (SMTP) — used for email verification and password reset.
# Credentials come from environment variables, never hardcoded.
# ---------------------------------------------------------------------------
def send_email(to_address, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["MAIL_SENDER"]
    msg["To"] = to_address
    msg["Date"] = email_utils.formatdate(localtime=True)
    msg["Message-ID"] = email_utils.make_msgid()
    msg.set_content(body)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    login = os.environ["SMTP_LOGIN"]
    key = os.environ["SMTP_PASSWORD"]

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(login, key)
        server.send_message(msg)


def _new_token():
    return secrets.token_urlsafe(32)


def _send_verification_email(db, user_id, email):
    token = _new_token()
    expires = dt.now(timezone.utc) + timedelta(hours=24)
    cursor = db.cursor()
    cursor.execute(
        "UPDATE users SET verify_token = ?, verify_token_expires = ? WHERE id = ?",
        (token, expires.isoformat(), user_id)
    )
    db.commit()

    verify_url = url_for("verify_email", token=token, _external=True)
    send_email(
        email,
        "Verify your account - Amazon Price Tracker",
        f"Confirm your email by clicking this link (valid for 24 hours):\n\n{verify_url}"
    )


def _send_reset_email(db, user_id, email):
    token = _new_token()
    expires = dt.now(timezone.utc) + timedelta(hours=1)
    cursor = db.cursor()
    cursor.execute(
        "UPDATE users SET reset_token = ?, reset_token_expires = ? WHERE id = ?",
        (token, expires.isoformat(), user_id)
    )
    db.commit()

    reset_url = url_for("reset_password", token=token, _external=True)
    send_email(
        email,
        "Reset your password - Amazon Price Tracker",
        f"Reset your password by clicking this link (valid for 1 hour):\n\n{reset_url}\n\n"
        "If you didn't request this, you can safely ignore this email."
    )

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
    owner_col, owner_val = _bookmark_owner()

    db = get_db()
    cursor = db.cursor()

    # Load bookmarks owned by this account (or anonymous session) from the DB
    cursor.execute(
        f"SELECT p.asin, p.name, p.price, p.discount, p.img_src "
        f"FROM products p JOIN user_bookmarks ub ON p.asin = ub.asin "
        f"WHERE ub.{owner_col} = ? ORDER BY ub.created_at DESC",
        (owner_val,)
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


@app.route("/signup", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not _EMAIL_RE.match(email) or len(password) < 8:
            return render_template(
                "signup.html",
                error="Inserisci un'email valida e una password di almeno 8 caratteri."
            )

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        if cursor.fetchone():
            return render_template("signup.html", error="Email già registrata.")

        cursor.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, generate_password_hash(password))
        )
        db.commit()
        user_id = cursor.lastrowid

        _migrate_session_bookmarks(db, user_id)
        _send_verification_email(db, user_id, email)
        login_user(User(user_id, email, email_verified=False))
        return redirect(url_for("home"))

    return render_template("signup.html")


@app.route("/verify-email/<token>")
def verify_email(token):
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT id, verify_token_expires FROM users WHERE verify_token = ?",
        (token,)
    )
    row = cursor.fetchone()

    if row is None:
        return render_template("error.html", error_code="-5")

    expires = dt.fromisoformat(row["verify_token_expires"])
    if dt.now(timezone.utc) > expires:
        return render_template("error.html", error_code="-6")

    cursor.execute(
        "UPDATE users SET email_verified = 1, verify_token = NULL, verify_token_expires = NULL WHERE id = ?",
        (row["id"],)
    )
    db.commit()

    if current_user.is_authenticated and current_user.id == str(row["id"]):
        current_user.email_verified = True

    return redirect(url_for("home"))


@app.route("/resend-verification", methods=["POST"])
@login_required
@limiter.limit("5 per minute")
def resend_verification():
    if not current_user.email_verified:
        db = get_db()
        _send_verification_email(db, current_user.id, current_user.email)
    return redirect(url_for("home"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id, email, password_hash, email_verified FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()

        if row is None or not check_password_hash(row["password_hash"], password):
            return render_template("login.html", error="Email o password non corretti.")

        _migrate_session_bookmarks(db, row["id"])
        login_user(User(row["id"], row["email"], row["email_verified"]))
        return redirect(url_for("home"))

    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id, email FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        if row:
            _send_reset_email(db, row["id"], row["email"])

        # Always show the same message, whether or not the email exists,
        # so this endpoint can't be used to enumerate registered accounts.
        return render_template(
            "forgot_password.html",
            sent=True
        )

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def reset_password(token):
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT id, reset_token_expires FROM users WHERE reset_token = ?",
        (token,)
    )
    row = cursor.fetchone()

    if row is None:
        return render_template("error.html", error_code="-5")

    expires = dt.fromisoformat(row["reset_token_expires"])
    if dt.now(timezone.utc) > expires:
        return render_template("error.html", error_code="-6")

    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            return render_template(
                "reset_password.html",
                token=token,
                error="La password deve avere almeno 8 caratteri."
            )

        cursor.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expires = NULL WHERE id = ?",
            (generate_password_hash(password), row["id"])
        )
        db.commit()
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


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

    MAX_BOOKMARKS = 4 #Limit of Bookmarks for a user
    owner_col, owner_val = _bookmark_owner()

    prod_details = request.get_json() #Gets product details from product.html
    price_text = f"{prod_details.get('price_whole')}{prod_details.get('price_fraction')}"
    try:
        price = float(price_text.replace(",", "."))
    except (TypeError, ValueError):
        return jsonify({"status": "price_not_found"})
    # Check if this asin is already bookmarked by this owner
    cursor.execute(f'SELECT COUNT(*) AS cnt FROM user_bookmarks WHERE {owner_col} = ? AND asin = ?',
                   (owner_val, prod_details.get('ASIN')))
    exists = cursor.fetchone()['cnt']
    if exists:
        return jsonify({"status": "duplicate"})

    # Enforce per-user bookmark limit before inserting
    cursor.execute(f'SELECT COUNT(*) AS cnt FROM user_bookmarks WHERE {owner_col} = ?', (owner_val,))
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

    # Add mapping for this owner -> asin
    cursor.execute(
        f'INSERT INTO user_bookmarks ({owner_col}, asin, created_at) VALUES (?, ?, ?)',
        (owner_val, prod_details.get('ASIN'), dt.utcnow().isoformat())
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
    # Remove mapping only for this owner; delete product when unused
    db = get_db()
    cursor = db.cursor()

    owner_col, owner_val = _bookmark_owner()

    data = request.get_json(silent=True)
    if isinstance(data, str):
        asin_to_delete = data
    elif isinstance(data, dict):
        asin_to_delete = data.get("ASIN") or data.get("asin")
    else:
        asin_to_delete = None

    if not asin_to_delete:
        return jsonify({"error": "missing ASIN"}), 400

    # Remove only the mapping for this owner
    cursor.execute(f'DELETE FROM user_bookmarks WHERE {owner_col} = ? AND asin = ?', (owner_val, asin_to_delete))

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

