from flask import Flask, render_template, request, session, url_for, redirect, jsonify

from scraper.amazon_scraper import get_product_details

app = Flask(__name__)
app.secret_key = "ciao_mamma"
# Change secret key in prod :)

@app.route("/")
def home():
    bookmark_dic = session.get("bookmark_session")
    
    return render_template("main_page.html", bookmark_dic = bookmark_dic)    

@app.route("/query", methods=['POST'])
def query():
    query_url = request.form["url_query"]
    err_id, query_price_whole, query_price_fraction, query_discount, query_img = get_product_details(query_url)
    if err_id != "0":
        return redirect(url_for("error_page",
                                error_code = err_id))
        
    # Add code for err_id = 1 (Missing image), which is not an actual error
        
        
    return render_template("product.html", 
                           error_code = "0",
                           price_whole = query_price_whole,
                           price_fraction = query_price_fraction,
                           discount = query_discount,
                           img_src = query_img)
    
@app.route("/bookmark", methods=["POST"])
def bookmark_func():
    details = request.get_json()
    
    session["bookmark_session"] = {
        "img_src": details.get("img_src"),
        "price": f"{details.get('price_whole')}{details.get('price_fraction')}",
        "discount": details.get("discount")
    }
    
    return jsonify({"status": "ok"})
    
    
    
@app.route("/error")
def error_page():
    error_id = request.args.get("error_code")
    
    return render_template("error.html", error_code = error_id)
    


if __name__ == '__main__': 
    app.run(debug=True)