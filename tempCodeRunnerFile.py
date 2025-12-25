from flask import Flask, render_template, request, jsonify

from scraper.amazon_scraper import get_price_discount

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("main_page.html")    

@app.route("/query", methods=['POST'])
def query():
    query_url = request.form["url_query"]
    query_price_whole, query_price_fraction, query_discount = get_price_discount(query_url)
    return jsonify({"price_whole": query_price_whole,
                    "price_fraction": query_price_fraction,
                    "discount": query_discount})


if __name__ == '__main__':
    app.run(debug=True)