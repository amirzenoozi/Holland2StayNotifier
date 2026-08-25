"""
Huurwoningen.nl source.

Same shape as the Funda source: search pages sit behind Cloudflare (plain HTTP
gets the "Just a moment..." interstitial) so they are fetched through Firecrawl
on the `basic` proxy, and one page carries the full listing data - 25 listings
for 1 credit, with no per-listing detail request.

The site sorts by "Nieuwste eerst" by default, so page one is always the newest
and pagination is never needed for polling.
"""

import logging
import re
from urllib.parse import urlencode

from fetcher import FetchError, firecrawl

NAME = "huurwoningen"
BASE = "https://www.huurwoningen.nl"

# /huren/amersfoort/d4420770/zeeuwsestraat/
DETAIL_RE = re.compile(r"/huren/([^/]+)/([0-9a-f]{6,})/([^/]+)/")

# ### [Appartement Zeeuwsestraat](https://www.huurwoningen.nl/huren/...)
HEADING_RE = re.compile(r"^#{1,6}\s*\[(.+?)\]\((https?://[^\s)]+)\)", re.MULTILINE)

PRICE_RE = re.compile(r"€\s*([\d.]+)\s*per maand", re.IGNORECASE)
ON_REQUEST_RE = re.compile(r"Prijs op aanvraag", re.IGNORECASE)
LOCATION_RE = re.compile(r"^(\d{4}\s?[A-Z]{2})\s+(.+)$", re.MULTILINE)
AREA_RE = re.compile(r"^([\d.]+)\s*m²$")
ROOMS_RE = re.compile(r"^(\d+)\s*kamers?$", re.IGNORECASE)

# The card's leading word is the dwelling type; the rest is the street.
DWELLING_TYPES = {
    "appartement", "huis", "studio", "kamer", "penthouse", "villa",
    "bungalow", "woonboot", "zolderkamer", "maisonnette", "loft",
}
INTERIORS = {"gemeubileerd", "gestoffeerd", "kaal", "gemeubeld"}

log = logging.getLogger(NAME)


def search_url(search):
    """
    Build a Huurwoningen search URL.

    A raw `url` in the config wins, so anyone can tune the filters in the
    browser and paste the address in - that exposes every filter the site has
    without this module having to model them.
    """
    if search.get("url"):
        return search["url"]

    area = search.get("area", "amersfoort").strip().lower().replace(" ", "-")

    params = {}
    if search.get("price"):
        params["price"] = search["price"]
    radius = search.get("radius")
    if radius:
        # Config may say "10km" for symmetry with Funda; this site wants "10".
        params["radius"] = str(radius).lower().removesuffix("km")
    params.update(search.get("params", {}))

    url = f"{BASE}/in/{area}/"
    return f"{url}?{urlencode(params)}" if params else url


def _attributes(block):
    """Pull area, rooms and furnishing out of a listing card's bullet list."""
    result = {"area": None, "rooms": None, "interior": None}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        value = line[2:].strip()

        area_match = AREA_RE.match(value)
        if area_match:
            if result["area"] is None:
                result["area"] = area_match.group(1)
            continue

        rooms_match = ROOMS_RE.match(value)
        if rooms_match:
            result["rooms"] = rooms_match.group(1)
        elif value.lower() in INTERIORS:
            result["interior"] = value
    return result


def _split_title(title, slug):
    """"Appartement Baak van Ouddorp" -> ("Appartement", "Baak van Ouddorp")."""
    head, _, rest = title.strip().partition(" ")
    if head.lower() in DWELLING_TYPES and rest:
        return head, rest
    return None, title.strip() or slug.replace("-", " ").title()


def parse_search(markdown):
    """Turn a search results page into listing dicts keyed by the site's id."""
    listings = {}
    headings = list(HEADING_RE.finditer(markdown))

    for index, match in enumerate(headings):
        title, url = match.group(1), match.group(2)
        detail = DETAIL_RE.search(url)
        if not detail:
            continue

        listing_id = detail.group(2)
        if listing_id in listings:
            continue

        # Everything up to the next heading belongs to this listing.
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        block = markdown[match.end() : end]

        dwelling_type, address = _split_title(title, detail.group(3))

        city = detail.group(1).replace("-", " ").title()
        postcode, district = None, None
        location = LOCATION_RE.search(block)
        if location:
            postcode = location.group(1)
            place = location.group(2).strip()
            # "Amersfoort (Puntenburg)" -> city + district
            neighbourhood = re.search(r"^(.*?)\s*\((.+)\)$", place)
            if neighbourhood:
                city, district = neighbourhood.group(1).strip(), neighbourhood.group(2)
            else:
                city = place

        price_match = PRICE_RE.search(block)
        listing = {
            "source": NAME,
            "url_key": f"hw-{listing_id}",
            "url": url,
            "address": address,
            "postcode": postcode,
            "district": district,
            "city": city,
            "price_excl": price_match.group(1) if price_match else None,
            "price_incl": None,
            "price_on_request": not price_match and bool(ON_REQUEST_RE.search(block)),
            "available_from": None,
            "dwelling_type": dwelling_type,
            "occupancy": None,
            "energy": None,
            "signees": None,
            "student_only": False,
            "lottery": False,
        }
        listing.update(_attributes(block))
        listings[listing_id] = listing

    return listings


def fetch_search(search):
    """Fetch one configured search and return {url_key: listing}."""
    url = search_url(search)
    data = firecrawl(url, ["markdown"])
    markdown = data.get("markdown") or ""
    if not markdown:
        raise FetchError(f"empty markdown for {url}")

    listings = parse_search(markdown)
    log.info(
        "search %r: %d listings (1 credit)",
        search.get("name", "huurwoningen"),
        len(listings),
    )
    return {listing["url_key"]: listing for listing in listings.values()}
