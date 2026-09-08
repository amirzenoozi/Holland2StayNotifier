"""
Pararius.com source.

Same shape as the Funda and Huurwoningen sources: search pages sit behind
Cloudflare (plain HTTP gets the "Just a moment..." interstitial) so they are
fetched through Firecrawl on the `basic` proxy, and one page carries the full
listing data - 30 listings for 1 credit, with no per-listing detail request.

Pararius sorts newest first by default, so page one is always the freshest and
pagination is never needed for polling.

Two things differ from Huurwoningen. Filters live in the URL *path* rather than
the query string (`/apartments/amersfoort/0-2000/radius-25`), and prices are
written in the English locale with a comma for thousands (`€1,995 pcm`). The
comma is swapped for a dot on the way out so every source renders alike.
"""

import logging
import re

from fetcher import FetchError, firecrawl

NAME = "pararius"
BASE = "https://www.pararius.com"

# /apartment-for-rent/utrecht/35283c71/karel-doormanlaan
DETAIL_RE = re.compile(r"/(\w+)-for-rent/([^/]+)/([0-9a-f]{6,})/([^/\s)]+)")

# ### [Flat Karel Doormanlaan](https://www.pararius.com/apartment-for-rent/...)
HEADING_RE = re.compile(r"^#{1,6}\s*\[(.+?)\]\((https?://[^\s)]+)\)", re.MULTILINE)

PRICE_RE = re.compile(r"€\s*([\d,.]+)\s*pcm", re.IGNORECASE)
ON_REQUEST_RE = re.compile(r"Price on request", re.IGNORECASE)
LOCATION_RE = re.compile(r"^(\d{4}\s?[A-Z]{2})\s+(.+)$", re.MULTILINE)
AREA_RE = re.compile(r"^([\d.]+)\s*m²$")
ROOMS_RE = re.compile(r"^(\d+)\s*rooms?$", re.IGNORECASE)

# The card's leading word is the dwelling type; the rest is the street.
# Pararius says "Flat" where the Dutch sites say "Appartement".
DWELLING_TYPES = {"flat", "house", "studio", "room", "apartment"}
INTERIORS = {"shell", "upholstered", "furnished", "upholstered or furnished"}

log = logging.getLogger(NAME)


def search_url(search):
    """
    Build a Pararius search URL.

    A raw `url` in the config wins, so anyone can tune the filters in the
    browser and paste the address in - that exposes every filter the site has
    without this module having to model them.

    Otherwise the pieces go together as a path, which is how Pararius spells
    its filters: /apartments/<place>/<type>/<min>-<max>/radius-<km>. The order
    matters, and `/apartments/` is the generic rental section - houses, studios
    and rooms all show up in it.
    """
    if search.get("url"):
        return search["url"]

    place = search.get("area", "amersfoort").strip().lower().replace(" ", "-")
    parts = [BASE, "apartments", place]

    if search.get("dwelling_type"):
        parts.append(str(search["dwelling_type"]).strip().lower())

    price = search.get("price")
    if price:
        # "1000-2000" is the shared config spelling; Pararius wants a bare
        # low end, so "0-2000" is how you say "up to 2000".
        parts.append(str(price).strip())

    radius = search.get("radius")
    if radius:
        # Config may say "25km" for symmetry with Funda; this site wants "25".
        parts.append(f"radius-{str(radius).lower().removesuffix('km')}")

    return "/".join(parts)


def _price(block):
    """"€1,995 pcm" -> "1.995", matching the Dutch sources' punctuation."""
    match = PRICE_RE.search(block)
    if not match:
        return None
    return match.group(1).replace(".", "").replace(",", ".")


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
    """"Flat Karel Doormanlaan" -> ("Flat", "Karel Doormanlaan")."""
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

        listing_id = detail.group(3)
        if listing_id in listings:
            continue

        # Everything up to the next heading belongs to this listing.
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        block = markdown[match.end() : end]

        dwelling_type, address = _split_title(title, detail.group(4))

        city = detail.group(2).replace("-", " ").title()
        postcode, district = None, None
        location = LOCATION_RE.search(block)
        if location:
            postcode = location.group(1)
            place = location.group(2).strip()
            # "Utrecht (Wijk C)" -> city + district
            neighbourhood = re.search(r"^(.*?)\s*\((.+)\)$", place)
            if neighbourhood:
                city, district = neighbourhood.group(1).strip(), neighbourhood.group(2)
            else:
                city = place

        price = _price(block)
        listing = {
            "source": NAME,
            "url_key": f"par-{listing_id}",
            "url": url,
            "address": address,
            "postcode": postcode,
            "district": district,
            "city": city,
            "price_excl": price,
            "price_incl": None,
            "price_on_request": not price and bool(ON_REQUEST_RE.search(block)),
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
        search.get("name", "pararius"),
        len(listings),
    )
    return {listing["url_key"]: listing for listing in listings.values()}
