import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
import time
import logging
import random
from typing import Optional, Tuple
import importlib
from urllib3.util.retry import Retry

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2  
BASE_DELAY = 1.5  
JITTER_RATIO = 0.2

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


def get_product_details(
    url: str,
    delay: float = BASE_DELAY,
    session: Optional[requests.Session] = None,
) -> Tuple:

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    active_session = session
    created_session = False
    if active_session is None:
        active_session = create_session()
        created_session = True

    cookies = {'i18n-prefs': 'EUR'}

    try:
        # App-level retry loop (captcha / parse / timeout handling)
        for attempt in range(MAX_RETRIES):
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

                # Detect real Amazon captcha/robot-check pages only
                if is_captcha_page(response, soup):
                    logger.warning(f"Captcha detected per {url} - attempt {attempt + 1}")
                    if attempt < MAX_RETRIES - 1:
                        sleep_with_jitter(RETRY_DELAY * (attempt + 1))
                        continue
                    return "-3", "Error", "Error", "Error", "", "Error", "N/A"

                # Extract data
                asin = extract_asin(url)
                price_whole = extract_price_whole(soup)
                price_fraction = extract_price_fraction(soup)
                name = extract_name(soup)
                discount = extract_discount(soup)
                img_url = extract_image(soup)

                if all([asin, price_whole, price_fraction, name]):
                    logger.info(f"Scraped {asin}: EUR {price_whole}{price_fraction}")
                    sleep_with_jitter(delay)
                    return "0", asin, name, price_whole, price_fraction, discount, img_url

                logger.warning(f"Missing data for {url}")
                return "-2", "Error", "Error", "Error", "", "Error", "N/A"

            except requests.Timeout:
                logger.warning(f"Timeout for {url} - attempt {attempt + 1}")
                if attempt < MAX_RETRIES - 1:
                    sleep_with_jitter(RETRY_DELAY * (attempt + 1))
                    continue
                return "-1", "", "", "", "", "", ""

            except requests.RequestException as e:
                logger.error(f"Request error: {e}")
                return "-1", "", "", "", "", "", ""

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
    final_url = (response.url or "").lower()
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








