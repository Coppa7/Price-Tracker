import requests
from bs4 import BeautifulSoup

def get_product_details(url):
    
    '''
    Uses scraping to find the price and discount (if there is one) of an item on amazon.it.
    This scraping is done only for learning purposes. A request is only sent once the program
    is ran. There's no multiple requests.
    '''
    
    cookies = {'i18n-prefs': 'EUR'}
        

    # Header for the HTTP request
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.amazon.it/",
        "DNT": "1", # Do Not Track
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        # -1 is connection error code
        response = requests.get(url, headers=headers, cookies=cookies, timeout=10)
        response.raise_for_status()        
    except requests.RequestException as e:
        print(f"Error: {e}")
        return "-1", "", "", "", "", "", ""
    
    # Possible to add a check for captchas 
    
    
    html = response.text 
    soup = BeautifulSoup(html, "html.parser")

    main_container = soup.find("div", id = "centerCol")
    img_container = soup.find("div", id = "leftCol")
    
    if main_container:
        #Find the ASIN of the amazon product (unique id) from the url 
        # .../name/dp/ASIN/...
        #We find the ASIN by splitting the url twice
        
        ASIN = ((url.split("/dp/"))[1].split("/"))[0]
        
        price_whole = main_container.find("span", class_="a-price-whole")
        price_fraction = main_container.find("span", class_="a-price-fraction")
        name = main_container.find("span", id = "productTitle")

        discount = main_container.find("span", class_="savingsPercentage")
        
        
        if ASIN and price_whole and price_fraction and name:
            price_whole = price_whole.get_text(strip=True)
            price_fraction = price_fraction.get_text(strip=True)
            name = name.get_text(strip=True)
        else:
            ASIN  = "Product not found"
            price_whole = "Price not found"
            price_fraction = ""
            name = "Name not found"
        if discount:
            discount = discount.get_text(strip=True)
        else:
            discount = "Discount not found"
            
        #Searching for image
        
        if img_container:
            img_tag = soup.find("img", id = "landingImage")
            if img_tag:
                img_url = img_tag['src']
                return "0", ASIN, name, price_whole, price_fraction, discount, img_url
            
        
        return "1", ASIN, name, price_whole, price_fraction, discount, "N/A"
    else:
        return "-2", "Error", "Error", "Error", "", "Error", "N/A"
        
    
    
    




