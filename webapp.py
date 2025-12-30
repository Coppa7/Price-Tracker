from flask import Flask, render_template, request, session, url_for, redirect, jsonify
from datetime import timedelta
from scraper.amazon_scraper import get_product_details
import sqlite3
import os

app = Flask(__name__)
app.secret_key = "ciao_mamma"
# Change secret key in prod :)

app.permanent_session_lifetime = timedelta(days=3650)
#Cookies can get removed by the user, otherwise they're semi-permanent

#We create a global database (all the bookmarked products shared between the users)
folder = 'database_dir'
path = os.path.join(folder, 'database.db')

if not os.path.exists(folder):
    os.makedirs(folder)

connection = sqlite3.connect(path)

cursor = connection.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS bookmarks (
    asin TEXT PRIMARY KEY,
    name TEXT,
    price FLOAT,
    discount TEXT,
    img_src TEXT
)
''')

#Database for graph data
cursor.execute('''
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT,
    price FLOAT,
    date DATETIME,
    FOREIGN KEY (asin) REFERENCES products (asin)
)
''')

connection.commit()
connection.close()

@app.route("/")
def home():
    session.permanent = True
    bookmarks = session.get("bookmarks_list", [])
    
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
    connection = sqlite3.connect(path)
    cursor = connection.cursor()
    
    session.permanent = True #Permanent cookies
    MAX_BOOKMARKS = 4 #Limit of Bookmarks for a user 
    
    prod_details = request.get_json() #Gets product details from product.html
    price_text = f"{prod_details.get('price_whole')}{prod_details.get('price_fraction')}"
    price = float(price_text.replace(",", "."))
    sql_query = '''
    INSERT OR REPLACE INTO bookmarks (asin, name, price, discount, img_src)
    VALUES(?, ?, ?, ?, ?)
    '''
    
    cursor.execute(sql_query, (
    prod_details.get("ASIN"), 
    prod_details.get("name"), 
    price, 
    prod_details.get("discount"), 
    prod_details.get("img_src")
    ))
    connection.commit()
    connection.close()
    
    bookmarks_list = session.get("bookmarks_list", []) #Gets the bookmarks list from the session (or creates a new one)
    
    if len(bookmarks_list) >= MAX_BOOKMARKS:
        return jsonify({"status": "full"})
    
    
    
    bookmark = {
        "img_src": prod_details.get("img_src"),
        "ASIN": prod_details.get("ASIN"),
        "name": prod_details.get("name"),
        "price": price_text,
        "discount": prod_details.get("discount")
    }
    
    if any(b.get("ASIN") == bookmark["ASIN"] for b in bookmarks_list):
        return jsonify({"status": "duplicate"})
    
    bookmarks_list.append(bookmark)
    session["bookmarks_list"] = bookmarks_list
    session.modified = True
    
    return jsonify({"status": "ok"})

@app.route("/unbook", methods = ["POST"])
def unbook_func():
    connection = sqlite3.connect(path)
    cursor = connection.cursor()

    bookmarks_list = session.get("bookmarks_list", [])
    ASIN_to_delete = request.get_json()
    session["bookmarks_list"] = [b for b in bookmarks_list if b.get("ASIN") != ASIN_to_delete]
    session.modified = True
    
    sql_query = "DELETE FROM bookmarks WHERE asin = ?"
    
    cursor.execute(sql_query, (ASIN_to_delete))
    
    connection.commit()
    connection.close()
    
    
    return "", 204    
    
    
    
@app.route("/error")
def error_page():
    error_id = request.args.get("error_code")
    
    return render_template("error.html", error_code = error_id)
    


if __name__ == '__main__': 
    app.run(debug=True)

