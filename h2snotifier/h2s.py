"""
Data source for Holland2Stay listings.

The old Magento GraphQL API (api.holland2stay.com/graphql) is closed to the
public and the whole site sits behind Cloudflare Turnstile, so nothing can be
fetched with plain HTTP any more. Everything here goes through Firecrawl, which
solves the challenge for us.

Two surfaces are used:
  1. /sitemap.xml      -> the set of currently listed url_keys   (1 credit)
  2. /residences/X.html -> details for one listing, as markdown  (1 credit)

Markdown is parsed with regexes rather than asking Firecrawl for JSON, because
LLM extraction costs 5 credits per page instead of 1.
"""

import logging
import re

from fetcher import FetchError, direct_get, firecrawl

NAME = "h2s"
BASE = "https://www.holland2stay.com"
SITEMAP_URL = f"{BASE}/sitemap.xml"

# Matches the English listing pages in the sitemap (the /nl/ mirrors are ignored).
LISTING_RE = re.compile(r"/residences/([^<\"]+?)\.html")

log = logging.getLogger(NAME)


def listing_url(url_key):
    return f"{BASE}/residences/{url_key}.html"


def street_prefix(url_key):
    """
    Reduce a listing key to the street it belongs to.

    'berg-en-dalseweg-79a110' -> 'berg-en-dalseweg'
    'bassin-184-a07'          -> 'bassin'
    'engelandlaan-306-f'      -> 'engelandlaan'

    Everything from the first digit-leading segment onwards is the house
    number and unit, which vary per listing; what remains identifies the
    building, and a building never moves to another city.
    """
    segments = []
    for segment in url_key.split("-"):
        if segment and segment[0].isdigit():
            break
        segments.append(segment)
    # Fall back to the whole key if it started with a digit.
    return "-".join(segments) if segments else url_key


def fetch_listing_keys():
    """Return the set of url_keys currently advertised in the sitemap."""
    xml = direct_get(SITEMAP_URL, expect="<loc>")
    if xml is not None:
        log.info("sitemap fetched directly (0 credits)")
    else:
        data = firecrawl(SITEMAP_URL, ["rawHtml"])
        xml = data.get("rawHtml") or ""
        log.info("sitemap fetched via Firecrawl (1 credit)")

    keys = set(LISTING_RE.findall(xml))
    if not keys:
        raise FetchError("sitemap contained no listings - the site layout may have changed")
    return keys


# --- detail page parsing --------------------------------------------------

CITY_RE = re.compile(r"^(.{2,40}?)\[\(Open in maps\)\]", re.MULTILINE)
ADDRESS_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)
PRICE_EXCL_RE = re.compile(r"€\s*([\d.,]+)\s*Excl", re.IGNORECASE)
PRICE_INCL_RE = re.compile(r"€\s*([\d.,]+)\s*Incl", re.IGNORECASE)
AREA_RE = re.compile(r"([\d]+(?:\.[\d]+)?)\s*m2")
AVAILABLE_RE = re.compile(r"Available per\s*([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")
SIGNEES_RE = re.compile(r"Currently\s+(\d+)\s+signees")

DWELLING_TYPES = {"studio", "apartment", "loft", "house", "room", "penthouse"}
OCCUPANCY = {"single", "two", "two (only couples)", "family", "couple", "sharing"}


def _bullets(markdown):
    """Short bullet-list values from the attribute list, ignoring page furniture."""
    values = []
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("- ") and len(line) < 60:
            values.append(line[2:].strip())
    return values


def parse_listing(markdown, url_key):
    """Turn a detail page's markdown into a flat dict. Missing fields stay None."""
    city = None
    match = CITY_RE.search(markdown)
    if match:
        candidate = match.group(1).strip()
        # The city sits alone on its line, right before the maps link.
        city = candidate.split("]")[-1].strip() or None

    address = None
    for heading in ADDRESS_RE.findall(markdown):
        if heading and not heading.startswith("!["):
            address = heading
            break

    bullets = _bullets(markdown)
    lowered = [b.lower() for b in bullets]

    dwelling = next((b for b, low in zip(bullets, lowered) if low in DWELLING_TYPES), None)
    occupancy = next((b for b, low in zip(bullets, lowered) if low in OCCUPANCY), None)

    def _first(pattern):
        found = pattern.search(markdown)
        return found.group(1) if found else None

    return {
        "url_key": url_key,
        "url": listing_url(url_key),
        "address": address,
        "city": city,
        "price_excl": _first(PRICE_EXCL_RE),
        "price_incl": _first(PRICE_INCL_RE),
        "area": _first(AREA_RE),
        "available_from": _first(AVAILABLE_RE),
        "dwelling_type": dwelling,
        "occupancy": occupancy,
        "signees": _first(SIGNEES_RE),
        "student_only": any("student only" in low for low in lowered),
        "lottery": any(low == "lottery" for low in lowered),
    }


def fetch_listing(url_key):
    """Fetch and parse one listing detail page. Costs 1 credit."""
    data = firecrawl(listing_url(url_key), ["markdown"])
    markdown = data.get("markdown") or ""
    if not markdown:
        raise FetchError(f"empty markdown for {url_key}")
    return parse_listing(markdown, url_key)
