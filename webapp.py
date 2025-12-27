from flask import Flask, render_template, request, session, url_for, redirect, jsonify

from scraper.amazon_scraper import get_product_details

app = Flask(__name__)
app.secret_key = "ciao_mamma"
# Change secret key in prod :)

@app.route("/")
def home():
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
    MAX_BOOKMARKS = 4 #Limit of Bookmarks for a user 
    
    prod_details = request.get_json() #Gets product details from product.html
    
    bookmarks_list = session.get("bookmarks_list", []) #Gets the bookmarks list from the session (or creates a new one)
    
    if len(bookmarks_list) >= MAX_BOOKMARKS:
        return jsonify({"status": "full"})
    
    
    
    bookmark = {
        "img_src": prod_details.get("img_src"),
        "ASIN": prod_details.get("ASIN"),
        "name": prod_details.get("name"),
        "price": f"{prod_details.get('price_whole')}{prod_details.get('price_fraction')}",
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
    bookmarks_list = session.get("bookmarks_list", [])
    ASIN_to_delete = request.get_json()
    session["bookmarks_list"] = [b for b in bookmarks_list if b.get("ASIN") != ASIN_to_delete]
    session.modified = True
    
    return "", 204    
    
    
@app.route("/error")
def error_page():
    error_id = request.args.get("error_code")
    
    return render_template("error.html", error_code = error_id)
    


if __name__ == '__main__': 
    app.run(debug=True)