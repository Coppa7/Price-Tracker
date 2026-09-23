import os
import sqlite3

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DB_PATH = os.path.join("database_dir", "bookmarks.db")
CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/gsheets_service_account.json"
)
SHEET_ID = os.environ.get("GOOGLE_SHEETS_ID")

# table -> columns to export. Only public product/price data goes to the
# sheet; users and user_bookmarks stay out of it on purpose.
TABLE_TABS = {
    "products": ["asin", "name", "price", "discount", "img_src"],
    "graph_data": ["id", "asin", "price", "date"],
}


def _get_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def sync_tables_to_sheets():
    if not SHEET_ID:
        print("GOOGLE_SHEETS_ID not set, skipping Sheets sync.")
        return

    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    for table, columns in TABLE_TABS.items():
        cur.execute(f"SELECT {', '.join(columns)} FROM {table}")
        rows = cur.fetchall()

        try:
            worksheet = spreadsheet.worksheet(table)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=table, rows=1, cols=len(columns))

        values = [columns] + [
            ["" if row[col] is None else row[col] for col in columns] for row in rows
        ]

        if worksheet.row_count < len(values):
            worksheet.resize(rows=len(values))

        worksheet.clear()
        worksheet.update(values=values)
        print(f"Synced {len(rows)} rows to '{table}' tab.")

    conn.close()


if __name__ == "__main__":
    sync_tables_to_sheets()
