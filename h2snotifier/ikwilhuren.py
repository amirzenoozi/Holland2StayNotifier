"""
ikwilhuren.nu (MVGM) - the cheapest source we have.

The site is plain HTTP with no bot protection, so it never costs a Firecrawl
credit. Its search form is a POST, but the server ignores the filter fields:
posting a city and a price range returns the same national catalogue as an
empty post. So we do the opposite of what the form intends - ask for
everything once (~330 listings in one request) and filter locally.

Every field we need is already on the result card, so there are no detail
fetches either. One cycle is exactly one HTTP request.
"""

import logging
import re

import requests

from fetcher import BROWSER_UA, FetchError

NAME = "ikwilhuren"
BASE = "https://ikwilhuren.nu"
SEARCH_URL = f"{BASE}/aanbod/"

TOKEN_RE = re.compile(r'name="_token"\s+value="([^"]+)"')

# One card: the stretched link carries the slug and the 32-hex id that is our
# stable key, then the postcode/city span, then a facts block with price and
# floor area. The tail is everything up to the end of the card.
CARD_RE = re.compile(
    r'<a class="stretched-link" href="(/object/([a-z0-9-]+)-([0-9a-f]{32})/)"[^>]*>'
    r'\s*(.*?)\s*</a>'
    r'.*?<span>([0-9]{4}\s?[A-Z]{2})\s+([^<]+)</span>'
    r'(.*?)</div>\s*</div>\s*</div>',
    re.S,
)
AVAIL_RE = re.compile(r"Beschikbaar vanaf\s*([\d-]+)")
PRICE_RE = re.compile(r"€\s*([\d.]+)")
AREA_RE = re.compile(r"(\d+)\s*m<sup>2</sup>")

DWELLING_TYPES = {
    "appartement",
    "eengezinswoning",
    "studio",
    "kamer",
    "woonhuis",
    "penthouse",
    "maisonnette",
    "bovenwoning",
    "benedenwoning",
    "tussenwoning",
    "hoekwoning",
    "parkeerplaats",
    "garage",
    "berging",
}

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

log = logging.getLogger(NAME)


def _readable_date(value):
    """Turn the site's 01-09-2026 into September 1, 2026."""
    parts = value.split("-")
    if len(parts) != 3:
        return value
    day, month, year = parts
    try:
        return f"{MONTHS[int(month) - 1]} {int(day)}, {year}"
    except (ValueError, IndexError):
        return value


def _split_title(title):
    """'Appartement Marie Vierdagstraat 206' -> ('Appartement', 'Marie ...')."""
    title = re.sub(r"\s+", " ", title).strip()
    head, _, rest = title.partition(" ")
    if rest and head.lower() in DWELLING_TYPES:
        return head, rest
    return None, title


def _fetch_catalogue(timeout=60):
    """POST the search form once and return the whole result page."""
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})
    try:
        page = session.get(SEARCH_URL, timeout=timeout)
        page.raise_for_status()
        token = TOKEN_RE.search(page.text)
        if not token:
            raise FetchError("ikwilhuren: no CSRF token on the search page")
        # The filter fields are sent because the form expects them, not
        # because the server honours them - we filter the results ourselves.
        response = session.post(
            SEARCH_URL,
            data={"_token": token.group(1), "postrequest": "1"},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"ikwilhuren: {exc}") from exc
    return response.text


def parse_catalogue(html):
    """Return {url_key: listing} for every card on the page."""
    listings = {}
    for path, slug, key, title, postcode, city, tail in CARD_RE.findall(html):
        dwelling_type, address = _split_title(title)
        price = PRICE_RE.search(tail)
        area = AREA_RE.search(tail)
        available = AVAIL_RE.search(tail)

        listings[f"iwh-{key}"] = {
            "source": NAME,
            "url_key": f"iwh-{key}",
            "url": f"{BASE}{path}",
            "address": address or slug.replace("-", " "),
            "postcode": postcode.strip(),
            "city": city.strip(),
            "price_excl": price.group(1) if price else None,
            "price_incl": None,
            "price_on_request": not price,
            "area": area.group(1) if area else None,
            "rooms": None,
            "energy": None,
            "available_from": _readable_date(available.group(1)) if available else None,
            "dwelling_type": dwelling_type,
            "occupancy": None,
            "signees": None,
            "student_only": False,
            "lottery": False,
        }
    return listings


def fetch_catalogue():
    html = _fetch_catalogue()
    listings = parse_catalogue(html)
    if not listings:
        raise FetchError("ikwilhuren: no listings found, the layout may have changed")
    log.info("catalogue: %d listings (0 credits)", len(listings))
    return listings
