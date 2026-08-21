"""
Funda source.

Funda blocks plain HTTP outright - even /robots.txt comes back as an anti-bot
interstitial - so every fetch goes through Firecrawl on the `basic` proxy.
(The `stealth` proxy returns HTTP 500 on Funda.)

The upside over Holland2Stay: a search results page carries the full listing
data, so one fetch (1 credit) yields ~15 listings and no per-listing detail
request is ever needed. Sorting by date_down keeps the newest on page 1.
"""

import json
import logging
import re
from urllib.parse import quote, urlencode

from fetcher import FetchError, firecrawl

NAME = "funda"
BASE = "https://www.funda.nl"

# /detail/huur/amersfoort/huis-de-marke-12/89577194/
DETAIL_RE = re.compile(r"/detail/(huur|koop)/([^/]+)/([^/]+)/(\d+)/")

# ## [De Marke 12 \ \ 3823 GP Amersfoort](https://www.funda.nl/detail/...)
HEADING_RE = re.compile(r"^#{1,6}\s*\[(.+?)\]\((https?://[^\s)]+)\)", re.MULTILINE)

PRICE_RE = re.compile(r"€\s*([\d.]+)\s*p\.m\.", re.IGNORECASE)
POSTCODE_RE = re.compile(r"(\d{4}\s?[A-Z]{2})\s+(.+)$")
AREA_RE = re.compile(r"^([\d.]+)\s*m²$")
ENERGY_RE = re.compile(r"^[A-G](\+{1,5})?$")

log = logging.getLogger(NAME)


def search_url(search):
    """
    Build a Funda search URL.

    A raw `url` in the config wins, so anyone can tune filters in the browser
    and paste the address in - that exposes every Funda filter without this
    module having to model them.
    """
    if search.get("url"):
        return search["url"]

    area = search.get("area", "amersfoort")
    radius = search.get("radius")
    selected = f"{area},{radius}" if radius else area

    params = {
        "selected_area": json.dumps([selected], separators=(",", ":")),
        "sort": '"date_down"',
    }
    price = search.get("price")
    if price:
        params["price"] = f'"{price}"'
    for key in ("object_type", "floor_area", "rooms", "bedrooms"):
        if search.get(key):
            params[key] = f'"{search[key]}"'

    kind = search.get("type", "huur")
    return f"{BASE}/zoeken/{kind}?{urlencode(params, quote_via=quote)}"


def _attributes(block):
    """Pull area, rooms and energy label out of a listing's bullet list."""
    result = {"area": None, "plot": None, "rooms": None, "energy": None}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        value = line[2:].strip()

        area_match = AREA_RE.match(value)
        if area_match:
            # A second m² figure is the plot size, not living space.
            if result["area"] is None:
                result["area"] = area_match.group(1)
            elif result["plot"] is None:
                result["plot"] = area_match.group(1)
            continue

        if value.isdigit():
            result["rooms"] = value
        elif ENERGY_RE.match(value):
            result["energy"] = value
    return result


def parse_search(markdown):
    """
    Turn a search results page into listing dicts keyed by Funda's numeric id.

    Every listing appears twice in the markdown (once as an image link, once as
    a heading), so results are de-duplicated by id.
    """
    listings = {}
    headings = list(HEADING_RE.finditer(markdown))

    for index, match in enumerate(headings):
        title, url = match.group(1), match.group(2)
        detail = DETAIL_RE.search(url)
        if not detail:
            continue

        listing_id = detail.group(4)
        if listing_id in listings:
            continue

        # Everything up to the next heading belongs to this listing.
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        block = markdown[match.end() : end]

        # "De Marke 12 \ \ 3823 GP Amersfoort" -> address + postcode + city
        cleaned = title.replace("\\", " ")
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        address, postcode, city = cleaned, None, detail.group(2).replace("-", " ").title()
        location = POSTCODE_RE.search(cleaned)
        if location:
            postcode = location.group(1)
            city = location.group(2).strip()
            address = cleaned[: location.start()].strip()

        price_match = PRICE_RE.search(block)
        listing = {
            "source": NAME,
            "url_key": f"funda-{listing_id}",
            "url": url,
            "address": address,
            "postcode": postcode,
            "city": city,
            "price_excl": price_match.group(1) if price_match else None,
            "price_incl": None,
            "available_from": None,
            "dwelling_type": detail.group(3).split("-")[0].title(),
            "occupancy": None,
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
        "search %r: %d listings (1 credit)", search.get("name", "funda"), len(listings)
    )
    return {listing["url_key"]: listing for listing in listings.values()}
