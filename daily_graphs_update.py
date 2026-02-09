from scraper.amazon_scraper import get_product_details
import sqlite3
import os
import time
from datetime import date

folder = "database_dir"
path = os.path.join(folder, "bookmarks.db")

if not os.path.exists(folder):
    os.makedirs(folder)


def update_all(delay_seconds: float = 1.0):
    """Iterate all products in the DB, rescrape their current price/info,
    update `products` and append a new row in `graph_data` for today if missing.

    delay_seconds: sleep between requests to avoid hammering Amazon.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute('SELECT asin FROM products')
    rows = cur.fetchall()
    if not rows:
        print('No products found in DB.')
        conn.close()
        return

    for r in rows:
        asin = r['asin']
        if not asin:
            continue

        url = f'https://www.amazon.it/dp/{asin}'
        print(f'Updating {asin}...')
        try:
            err_id, got_asin, name, price_whole, price_fraction, discount, img = get_product_details(url)
        except Exception as e:
            print(f'  Scrape error for {asin}: {e}')
            time.sleep(delay_seconds)
            continue

        if err_id != "0":
            print(f'  Skipped {asin}, scraper returned err_id={err_id}')
            time.sleep(delay_seconds)
            continue

        price_text = f"{price_whole}{price_fraction}"
        try:
            price = float(price_text.replace(',', '.'))
        except Exception:
            price = None

        # Update product info
        cur.execute(
            '''INSERT OR REPLACE INTO products (asin, name, price, discount, img_src)
               VALUES (?, ?, ?, ?, ?)''',
            (got_asin, name, price, discount, img)
        )

        # Insert graph point for today if not exists
        today = date.today().isoformat()
        cur.execute('SELECT COUNT(*) AS cnt FROM graph_data WHERE asin = ? AND date = ?', (got_asin, today))
        exists = cur.fetchone()[0]
        if not exists:
            cur.execute('INSERT INTO graph_data (asin, price, date) VALUES (?, ?, ?)', (got_asin, price, today))
            print(f'  Added graph point for {asin} -> {price} on {today}')
        else:
            print(f'  Graph point already exists for {asin} on {today}')

        conn.commit()
        time.sleep(delay_seconds)

    conn.close()


if __name__ == '__main__':
    update_all()