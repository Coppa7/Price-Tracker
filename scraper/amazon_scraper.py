import re
import os
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
import time
import logging
import random
from threading import Semaphore
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
PLAYWRIGHT_HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1").lower() not in {"0", "false", "no"}

# Max concurrent Playwright browser instances. Each one spawns a Chromium process
# (~150-300 MB RAM). Raise via env var only if the server has headroom.
_PLAYWRIGHT_MAX_CONCURRENT = int(os.environ.get("PLAYWRIGHT_MAX_CONCURRENT", "2"))
_playwright_semaphore = Semaphore(_PLAYWRIGHT_MAX_CONCURRENT)

# Hard cap on the HTML debug dump to prevent large pages from filling disk.
_MAX_HTML_DUMP_BYTES = int(os.environ.get("MAX_HTML_DUMP_BYTES", str(512 * 1024)))  # 512 KB

FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

DESKTOP_USER_AGENT = FALLBACK_USER_AGENTS[0]

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


def build_desktop_context_kwargs() -> dict:
    """Return a desktop-like browser context configuration."""
    return {
        "user_agent": DESKTOP_USER_AGENT,
        "viewport": {"width": 1366, "height": 768},
        "screen": {"width": 1366, "height": 768},
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "locale": "it-IT",
        "timezone_id": "Europe/Rome",
    }


def get_product_details_playwright(url: str, delay: float = BASE_DELAY) -> Tuple:
    """
    Fallback scraper using Playwright browser automation.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright not available. Install with: pip install playwright && playwright install")
        return "-1", "", "", "", "", "", ""

    # Reject immediately if too many Chromium processes are already running.
    if not _playwright_semaphore.acquire(blocking=False):
        logger.warning("Playwright concurrency limit (%d) reached, dropping request", _PLAYWRIGHT_MAX_CONCURRENT)
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
                        headless=PLAYWRIGHT_HEADLESS,
                        args=[
                            "--disable-blink-features=AutomationControlled",  # Hides automation
                            "--disable-dev-shm-usage",  # Prevents crashes on Linux servers
                            "--no-sandbox",  # Allows running without sandboxing on headless servers
                        ]
                    )

                    # Create a desktop-like browser context before opening the page
                    context = browser.new_context(**build_desktop_context_kwargs())
                    page = context.new_page()

                    # Set extra HTTP headers to look even more like real browser
                    page.set_extra_http_headers({
                        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Referer": "https://www.amazon.it/",
                    })

                    # Set cookies on the context so the whole browser session looks like a desktop shopper
                    context.add_cookies([
                        {"name": "i18n-prefs", "value": "EUR", "url": "https://www.amazon.it"}
                    ])

                    # Navigate to page - wait for network to be idle (all requests finished)
                    page.goto(url, wait_until="networkidle", timeout=20000)

                    # Simulate human behavior - wait a bit before extracting data
                    sleep_with_jitter(random.uniform(1.0, 2.5))

                    # Get final HTML (now includes all JS-rendered content)
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    logger.debug("[Playwright] page.url=%s page.title=%s html_length=%s", page.url, page.title(), len(html))
                    log_html_id_presence("Playwright", html)
                    log_html_selector_state("Playwright", url, soup)

                    # Check if we got captcha'd
                    if is_captcha_page(None, soup):
                        logger.warning(f"Captcha detected with Playwright for {url}")
                        context.close()
                        browser.close()
                        if attempt < MAX_RETRIES - 1:
                            sleep_with_jitter(RETRY_DELAY * (attempt + 1))
                        continue

                    # Extract data only from product containers
                    asin, name, price_whole, price_fraction, discount, img_url = extract_scoped_product_details(
                        url, soup, "Playwright"
                    )

                    context.close()
                    browser.close()

                    log_scrape_result("Playwright", url, asin, name, price_whole, price_fraction, discount, img_url)
                    sleep_with_jitter(delay)
                    return "0", asin, name, price_whole, price_fraction, discount, img_url

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
    finally:
        _playwright_semaphore.release()


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
        logger.info(f"Trying requests for {url}")
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
                logger.debug("[Requests] response.url=%s html_length=%s", response.url, len(response.text))
                log_html_id_presence("Requests", response.text)
                log_html_selector_state("Requests", url, soup)

                # Detect real Amazon captcha/robot-check pages
                if is_captcha_page(response, soup):
                    logger.warning(f"Captcha detected via requests - will try Playwright")
                    requests_failed = True
                    break  # Exit requests loop, fallback to Playwright

                # Extract data only from product containers
                asin, name, price_whole, price_fraction, discount, img_url = extract_scoped_product_details(
                    url, soup, "Requests"
                )

                log_scrape_result("Requests", url, asin, name, price_whole, price_fraction, discount, img_url)
                sleep_with_jitter(delay)
                return "0", asin, name, price_whole, price_fraction, discount, img_url

            except requests.Timeout:
                logger.warning(f"Timeout from requests (attempt {attempt + 1}/2) - will try Playwright")
                requests_failed = True
                break  # Try Playwright

            except requests.RequestException as e:
                logger.warning(f"Request error: {e} - will try Playwright")
                requests_failed = True
                break  # Try Playwright

        #Fallback to Playwright if requests failed 
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
    user_agent = DESKTOP_USER_AGENT

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
    elem = soup.find("span", id="productTitle")
    return elem.get_text(strip=True) if elem else ""

def extract_discount(soup) -> str:
    elem = soup.find("span", class_="savingsPercentage")
    return elem.get_text(strip=True) if elem else "Discount not found"

def extract_image(soup) -> str:
    img_tag = soup.find("img", id="landingImage")
    return img_tag.get('src', 'N/A') if img_tag else 'N/A'


def _find_container_by_id(soup, container_id: str):
    container = soup.find(id=container_id)
    if container:
        return container
    return soup.select_one(f"#{container_id}")


def extract_scoped_product_details(url: str, soup, source: str) -> Tuple[str, str, str, str, str, str]:
    """Extract only from Amazon's centerCol/leftCol containers."""
    main_container = _find_container_by_id(soup, "centerCol")
    img_container = _find_container_by_id(soup, "leftCol")

    if not main_container:
        logger.warning("[%s] missing main_container (div#centerCol) -> product text may be ambiguous or absent", source)
    if not img_container:
        logger.warning("[%s] missing img_container (div#leftCol) -> image may be unavailable", source)
    if not main_container or not img_container:
        log_raw_html_on_missing_container(source, str(soup))

    asin = normalize_not_found(extract_asin(url))

    if main_container:
        price_whole_tag = main_container.find("span", class_="a-price-whole")
        price_fraction_tag = main_container.find("span", class_="a-price-fraction")
        name_tag = main_container.find("span", id="productTitle")
        discount_tag = main_container.find("span", class_="savingsPercentage")

        price_whole = normalize_not_found(price_whole_tag.get_text(strip=True) if price_whole_tag else "")
        price_fraction = normalize_not_found(price_fraction_tag.get_text(strip=True) if price_fraction_tag else "", fallback="")
        name = normalize_not_found(name_tag.get_text(strip=True) if name_tag else "")
        discount = normalize_not_found(discount_tag.get_text(strip=True) if discount_tag else "")
    else:
        price_whole = "Not found"
        price_fraction = ""
        name = "Not found"
        discount = "Not found"

    if img_container:
        img_tag = img_container.find("img", id="landingImage")
        img_url = normalize_not_found(img_tag.get("src", "") if img_tag else "", fallback="N/A")
    else:
        img_url = "N/A"

    return asin, name, price_whole, price_fraction, discount, img_url


def normalize_not_found(value: str, fallback: str = "Not found") -> str:
    value = (value or "").strip()
    return value if value else fallback


def log_html_selector_state(source: str, url: str, soup) -> None:
    """Log which important HTML objects are present and what happens if they are missing."""
    main_container = _find_container_by_id(soup, "centerCol")
    img_container = _find_container_by_id(soup, "leftCol")

    checks = {
        "asin_from_url": (bool(extract_asin(url)), "If missing, ASIN will be 'Not found'"),
        "centerCol": (bool(main_container), "If missing, name/price/discount may be 'Not found'"),
        "leftCol": (bool(img_container), "If missing, image URL will be 'N/A'"),
        "productTitle": (bool(main_container and main_container.find("span", id="productTitle")), "If missing, product name will be 'Not found'"),
        "a-price-whole": (bool(main_container and main_container.find("span", class_="a-price-whole")), "If missing, price whole will be 'Not found'"),
        "a-price-fraction": (bool(main_container and main_container.find("span", class_="a-price-fraction")), "If missing, price fraction will be empty"),
        "savingsPercentage": (bool(main_container and main_container.find("span", class_="savingsPercentage")), "If missing, discount will be 'Not found'"),
        "landingImage": (bool(img_container and img_container.find("img", id="landingImage")), "If missing, image URL will be 'N/A'"),
    }

    logger.debug("[%s] HTML selector state for %s", source, url)
    for name, (present, consequence) in checks.items():
        if present:
            logger.debug("[%s] selector present: %s", source, name)
        else:
            logger.warning("[%s] selector missing: %s -> %s", source, name, consequence)

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title:
        logger.debug("[%s] page title: %s", source, title)
    else:
        logger.warning("[%s] page title missing -> page may be partial or blocked", source)


def log_raw_html_on_missing_container(source: str, html: str, limit: int = 0) -> None:
    """Dump the received HTML when the expected product containers are missing."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dump_path = os.path.join(project_root, "amazon_raw.html")
    cap = limit if limit and limit > 0 else _MAX_HTML_DUMP_BYTES
    payload = html[:cap]

    try:
        with open(dump_path, "w", encoding="utf-8") as dump_file:
            dump_file.write(f"<!-- source: {source} -->\n")
            dump_file.write(payload)
        logger.warning("[%s] raw HTML saved to %s (%d/%d bytes)", source, dump_path, len(payload), len(html))
    except OSError as exc:
        logger.warning("[%s] failed to save raw HTML to %s: %s", source, dump_path, exc)


def log_html_id_presence(source: str, html: str) -> None:
    """Log whether important ids appear in the raw HTML when BeautifulSoup misses them."""
    for container_id in ("centerCol", "leftCol"):
        found_in_html = bool(re.search(rf'id=["\']{re.escape(container_id)}["\']', html))
        if found_in_html:
            logger.debug("[%s] raw HTML contains id=%s", source, container_id)
        else:
            logger.warning("[%s] raw HTML missing id=%s", source, container_id)


def log_scrape_result(source: str, url: str, asin: str, name: str, price_whole: str, price_fraction: str, discount: str, img_url: str) -> None:
    logger.info(
        "[%s] scraped url=%s asin=%s name=%s price=%s%s discount=%s img=%s",
        source,
        url,
        asin,
        name,
        price_whole,
        price_fraction,
        discount,
        img_url,
    )


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








