from flask import Flask, render_template, request, jsonify

from scraper.amazon_scraper import get_product_details

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("main_page.html")    

@app.route("/query", methods=['POST'])
def query():
    query_url = request.form["url_query"]
    err_id, query_price_whole, query_price_fraction, query_discount, query_img = get_product_details(query_url)
    if err_id != 0:
        return render_template("error.html", 
                                error_code = err_id,
                                price_whole = query_price_whole,
                                price_fraction = query_price_fraction,
                                discount = query_discount,
                                img_src = query_img)
        
    # Add code for err_id = 1 (Missing image)
        
        
    return render_template("product.html", 
                           error_code = 0,
                           price_whole = query_price_whole,
                           price_fraction = query_price_fraction,
                           discount = query_discount,
                           img_src = query_img)


if __name__ == '__main__': 
    app.run(debug=True)