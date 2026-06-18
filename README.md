# Amazon Price Tracker

A self-hosted web application for tracking Amazon Italy product prices over time. Paste a product URL, save it as a bookmark, and the app records the price daily so you can visualize how it changes. The bookmarks are currenly stored via sessions to avoid having to create an account; Might add Sign up in the future for the possibility to keep stored products permanently.

## Features

- Price history charts for bookmarked products
- Dual-strategy scraper: fast HTTP requests with a Playwright browser fallback for bot-protected pages
- Per-user bookmarks stored server-side via anonymous sessions (no account required)
- Daily background job that refreshes prices for all tracked products
- Global request throttle shared across all worker processes to avoid triggering Amazon rate limits

## Stack

- **Backend**: Python, Flask, Gunicorn (gevent workers)
- **Scraping**: requests + BeautifulSoup, Playwright (fallback)
- **Database**: SQLite
- **Frontend**: Jinja2 templates, Tailwind CSS, Chart.js
- **Reverse proxy**: nginx
- **CDN / tunnel**: Cloudflare

## Requirements

- Python 3.10+
- Node.js (for Tailwind CSS compilation)
- Playwright browsers (`playwright install chromium`)

## Setup

```bash
git clone https://github.com/Coppa7/Price-Tracker.git
cd Price-Tracker

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

npm install
npm run build

python3 init_db.py
```

Create a `.env` file in the project root:

```
FLASK_SECRET_KEY=your_secret_key_here
APP_PREFIX=/PriceTracker
APPLICATION_ROOT=/PriceTracker
AMAZON_MIN_INTERVAL=4.0
PLAYWRIGHT_MAX_CONCURRENT=2
```

Generate a secure secret key with:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## Running

**Development:**

```bash
source venv/bin/activate
set -a && source .env && set +a
flask --app webapp run --debug
```

**Production (Gunicorn):**

```bash
source venv/bin/activate
set -a && source .env && set +a
gunicorn -w 2 -k gevent -b 127.0.0.1:8000 webapp:app
```

## Daily Price Update

The `daily_graphs_update.py` script scrapes current prices for all tracked products and appends a new data point to the price history. Run it via cron:

```bash
0 9 * * * /home/user/Price-Tracker/run_scraper_daily.sh
```

## Deployment Notes

The application is designed to run behind nginx and Cloudflare. The nginx configuration should proxy requests to Gunicorn and pass Cloudflare headers through:

```nginx
location /PriceTracker/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $http_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
}
```

## Security

- CSRF protection on all state-changing endpoints
- Rate limiting per IP (10 requests/minute on the scrape endpoint)
- SSRF guard: only amazon.it product URLs with a valid ASIN are accepted
- Security headers on every response (CSP, HSTS, X-Frame-Options, X-Content-Type-Options)
- Secure session cookies (HttpOnly, SameSite=Lax, Secure)
- Cross-process Amazon request throttle via fcntl file locks (prevents multiple workers from hitting Amazon simultaneously)

## Possible future additions

- Signup / Login for permanent bookmarks
- IP rotation
- Better Price Graphs
- Notifications for product price decrease
- Research by ASIN
- Research by name (list of possible products)

## License

This project is built for personal and educational purposes only. 
It is not intended for commercial use or mass data collection.
All data is fetched from publicly accessible Amazon Italy product pages.
Use responsibly and in accordance with Amazon's terms of service.
