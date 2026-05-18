import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
import time
import logging
import random
from typing import Optional, Tuple
import importlib
from urllib3.util.retry import Retry

# Playwright import (optional, for fallback)
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not PLAYWRIGHT_AVAILABLE:
    logger.warning("Playwright not installed. Browser fallback disabled. Install with: pip install playwright && playwright install")

MAX_RETRIES = 3
RETRY_DELAY = 2  
BASE_DELAY = 1.5  
JITTER_RATIO = 0.2
USE_PLAYWRIGHT_FALLBACK = True  # Abilita fallback a Playwright se requests fallisce

FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

class ScraperError(Exception):
    pass


def create_session() -> requests.Session:
    """Create a session with connection pooling and HTTP retries for transient errors."""
    session = requests.Session()
    retry_strategy = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_product_details_playwright(url: str, delay: float = BASE_DELAY) -> Tuple:
    """
    Fallback scraper using Playwright browser automation.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright not available. Install with: pip install playwright && playwright install")
        return "-1", "", "", "", "", "", ""
    
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        for attempt in range(MAX_RETRIES):
            browser = None
            try:
                logger.info(f"Playwright attempt {attempt + 1}/{MAX_RETRIES} for {url}")
                
                with sync_playwright() as p:
                    # Launch browser with anti-detection measures
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",  # Hides automation
                            "--disable-dev-shm-usage",  # Prevents crashes on Linux servers
                            "--no-sandbox",  # Allows running without sandboxing on headless servers
                        ]
                    )
                    
                    # Create page with realistic settings
                    page = browser.new_page(
                        user_agent=get_random_user_agent(),
                        viewport={"width": 1920, "height": 1080},  # Real browser size
                    )
                    
                    # Set extra HTTP headers to look even more like real browser
                    page.set_extra_http_headers({
                        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Referer": "https://www.amazon.it/",
                    })
                    
                    # Set cookies
                    page.context.add_cookies([
                        {"name": "i18n-prefs", "value": "EUR", "url": "https://www.amazon.it"}
                    ])
                    
                    # Navigate to page - wait for network to be idle (all requests finished)
                    page.goto(url, wait_until="networkidle", timeout=20000)
                    
                    # Simulate human behavior - wait a bit before extracting data
                    sleep_with_jitter(random.uniform(1.0, 2.5))
                    
                    # Wait for price element to appear 
                    # If it doesn't appear in 5 seconds, it's not a real product page
                    try:
                        page.wait_for_selector("span.a-price-whole", timeout=5000)
                    except Exception as e:
                        logger.warning(f"Price element not found - possibly captcha/blocked page")
                        page.context.browser.close() if page.context.browser else None
                        continue
                    
                    # Get final HTML (now includes all JS-rendered content)
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # Check if we got captcha'd
                    if is_captcha_page(None, soup):
                        logger.warning(f"Captcha detected with Playwright for {url}")
                        page.context.browser.close() if page.context.browser else None
                        if attempt < MAX_RETRIES - 1:
                            sleep_with_jitter(RETRY_DELAY * (attempt + 1))
                        continue
                    
                    # Extract data
                    asin = extract_asin(url)
                    price_whole = extract_price_whole(soup)
                    price_fraction = extract_price_fraction(soup)
                    name = extract_name(soup)
                    discount = extract_discount(soup)
                    img_url = extract_image(soup)
                    
                    page.context.browser.close() if page.context.browser else None
                    
                    # Success criteria
                    if all([asin, price_whole, price_fraction, name]):
                        logger.info(f"Playwright SUCCESS: {asin}: EUR {price_whole}{price_fraction}")
                        sleep_with_jitter(delay)
                        return "0", asin, name, price_whole, price_fraction, discount, img_url
                    
                    logger.warning(f"Missing data from Playwright for {url}")
                    return "-2", "Error", "Error", "Error", "", "Error", "N/A"
            
            except Exception as e:
                logger.error(f"Playwright error on attempt {attempt + 1}: {str(e)}")
                if browser:
                    try:
                        browser.close()
                    except:
                        pass
                
                if attempt < MAX_RETRIES - 1:
                    sleep_with_jitter(RETRY_DELAY * (attempt + 1))
        
        return "-1", "", "", "", "", "", ""
    
    except Exception as e:
        logger.error(f"Playwright fatal error: {str(e)}")
        return "-1", "", "", "", "", "", ""


def get_product_details(
    url: str,
    delay: float = BASE_DELAY,
    session: Optional[requests.Session] = None,
) -> Tuple:
    """
    Primary scraper using HTTP requests (fast). 
    Falls back to Playwright if blocked/captcha detected.
    
    Returns: (status_code, asin, name, price_whole, price_fraction, discount, img_url)
    Status codes:
      "0"  = Success
      "-1" = Request/Network error
      "-2" = Missing data
      "-3" = Captcha detected
    """

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    active_session = session
    created_session = False
    if active_session is None:
        active_session = create_session()
        created_session = True

    cookies = {'i18n-prefs': 'EUR'}
    requests_failed = False

    try:
        # ========== PHASE 1: Try with HTTP requests (fast) ==========
        logger.info(f"🔍 Trying requests for {url}")
        for attempt in range(2):  # Only 2 attempts with requests
            try:
                headers = build_headers(attempt)
                response = active_session.get(
                    url,
                    headers=headers,
                    cookies=cookies,
                    timeout=15,
                )
                response.raise_for_status()

                soup = BeautifulSoup(response.text, "html.parser")

                # Detect real Amazon captcha/robot-check pages
                if is_captcha_page(response, soup):
                    logger.warning(f"Captcha detected via requests - will try Playwright")
                    requests_failed = True
                    break  # Exit requests loop, fallback to Playwright

                # Extract data
                asin = extract_asin(url)
                price_whole = extract_price_whole(soup)
                price_fraction = extract_price_fraction(soup)
                name = extract_name(soup)
                discount = extract_discount(soup)
                img_url = extract_image(soup)

                if all([asin, price_whole, price_fraction, name]):
                    logger.info(f"Requests SUCCESS: {asin}: EUR {price_whole}{price_fraction}")
                    sleep_with_jitter(delay)
                    return "0", asin, name, price_whole, price_fraction, discount, img_url

                logger.warning(f"Missing data from requests - will try Playwright")
                requests_failed = True
                break  # Try Playwright

            except requests.Timeout:
                logger.warning(f"Timeout from requests (attempt {attempt + 1}/2) - will try Playwright")
                requests_failed = True
                break  # Try Playwright

            except requests.RequestException as e:
                logger.warning(f"Request error: {e} - will try Playwright")
                requests_failed = True
                break  # Try Playwright

        # ========== PHASE 2: Fallback to Playwright if requests failed ==========
        if requests_failed and USE_PLAYWRIGHT_FALLBACK:
            logger.info(f"Fallback: Starting Playwright for {url}")
            result = get_product_details_playwright(url, delay)
            
            # Only return Playwright result if successful (status = "0")
            if result[0] == "0":
                return result
            
            # If Playwright also failed, return the Playwright error
            return result
        
        # If we get here, requests failed and Playwright is disabled
        return "-1", "", "", "", "", "", ""

    finally:
        if created_session and active_session is not None:
            active_session.close()


def build_headers(attempt: int) -> dict:
    """Build request headers with a rotated User-Agent and deterministic fallback."""
    user_agent = get_random_user_agent()

    if not user_agent:
        user_agent = FALLBACK_USER_AGENTS[attempt % len(FALLBACK_USER_AGENTS)]

    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.amazon.it/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="125", "Google Chrome";v="125"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }


def get_random_user_agent() -> str:
    """Return a random UA from fake-useragent when available, else use fallback list."""
    try:
        fake_ua_module = importlib.import_module("fake_useragent")
        user_agent = fake_ua_module.UserAgent().random
        if user_agent:
            return user_agent
    except Exception:
        pass

    return FALLBACK_USER_AGENTS[int(time.time()) % len(FALLBACK_USER_AGENTS)]


def sleep_with_jitter(base_seconds: float, jitter_ratio: float = JITTER_RATIO) -> None:
    """Sleep around base_seconds with symmetric jitter to avoid fixed request patterns."""
    if base_seconds <= 0:
        return

    min_factor = max(0.0, 1.0 - jitter_ratio)
    max_factor = 1.0 + jitter_ratio
    sleep_seconds = base_seconds * random.uniform(min_factor, max_factor)
    time.sleep(sleep_seconds)


def extract_asin(url: str) -> str:
    try:
        return url.split("/dp/")[1].split("/")[0]
    except IndexError:
        return ""

def extract_price_whole(soup) -> str:
    elem = soup.find("span", class_="a-price-whole")
    return elem.get_text(strip=True) if elem else ""

def extract_price_fraction(soup) -> str:
    elem = soup.find("span", class_="a-price-fraction")
    return elem.get_text(strip=True) if elem else ""

def extract_name(soup) -> str:
    # Fallback a più selettori
    elem = soup.find("span", id="productTitle")
    return elem.get_text(strip=True) if elem else ""

def extract_discount(soup) -> str:
    elem = soup.find("span", class_="savingsPercentage")
    return elem.get_text(strip=True) if elem else "Discount not found"

def extract_image(soup) -> str:
    img_tag = soup.find("img", id="landingImage")
    return img_tag.get('src', 'N/A') if img_tag else 'N/A'


def is_captcha_page(response, soup) -> bool:
    """Return True only when the response looks like Amazon's captcha challenge page."""
    # Handle case where response is None (e.g., when called from Playwright)
    final_url = (response.url or "").lower() if response else ""
    if "validatecaptcha" in final_url or "/errors/validatecaptcha" in final_url:
        return True

    title_text = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if "robot check" in title_text or "enter the characters you see below" in title_text:
        return True

    captcha_form = soup.find("form", action=lambda a: isinstance(a, str) and "validatecaptcha" in a.lower())
    if captcha_form:
        return True

    if soup.find("input", id="captchacharacters"):
        return True

    captcha_img = soup.find("img", src=lambda s: isinstance(s, str) and "captcha" in s.lower())
    if captcha_img:
        return True

    return False








