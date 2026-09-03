"""
Where a listing actually is, and whether that is close enough to care about.

Matching towns by name works until you want "anywhere within 40 km of
Nijkerk". That question is about geography, not spelling: the answer includes
villages nobody would think to list, and the list would go stale the moment a
source started naming a suburb differently.

So we put the listing on the map instead. PDOK - the Dutch government's own
geocoder - answers for free, needs no key, and is authoritative about Dutch
place names and postcodes. Every answer is cached in the database, so each
postcode area is looked up once and never again.

Listings are located by the four-digit part of their postcode. That is a
neighbourhood-sized area, which is far more precision than a 20 km radius
needs, and it keeps the cache small: a few hundred entries rather than one
per address.
"""

import logging
import math
import re

import requests

import store

PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
POINT_RE = re.compile(r"POINT\(([-\d.]+)\s+([-\d.]+)\)")
PC4_RE = re.compile(r"\b(\d{4})\s?[A-Za-z]{2}\b")
EARTH_RADIUS_KM = 6371.0

log = logging.getLogger("geo")


def distance_km(first, second):
    """Great-circle distance between two (lat, lon) pairs."""
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _ask_pdok(term, kind, timeout=20):
    """One geocoder call. Returns (lat, lon), or None when nothing matches."""
    params = {"q": term, "fq": f"type:{kind}", "rows": 1, "fl": "centroide_ll"}
    try:
        response = requests.get(PDOK_URL, params=params, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("geocoder unreachable for %r: %s", term, exc)
        return None
    if response.status_code != 200:
        log.warning("geocoder returned HTTP %d for %r", response.status_code, term)
        return None

    docs = response.json().get("response", {}).get("docs", [])
    if not docs:
        return None
    match = POINT_RE.search(docs[0].get("centroide_ll") or "")
    if not match:
        return None
    # PDOK writes points longitude-first.
    return float(match.group(2)), float(match.group(1))


def _cached(key, term, kind):
    """Geocode once, remember forever - including the misses."""
    hit = store.get_place(key)
    if hit is not None:
        return hit if hit[0] is not None else None

    point = _ask_pdok(term, kind)
    store.set_place(key, *(point or (None, None)))
    if point:
        log.info("located %s at %.4f, %.4f", term, *point)
    else:
        log.info("could not locate %s", term)
    return point


def locate_town(name):
    """Coordinates of a Dutch town, by name."""
    name = (name or "").strip()
    if not name:
        return None
    return _cached(f"town:{name.lower()}", name, "woonplaats")


def locate_listing(postcode, city=None):
    """
    Coordinates of a listing, preferring its postcode.

    A postcode is unambiguous; a town name is not - the Netherlands has more
    than one Huis ter Heide - so the name is only a fallback for sources that
    do not give us a postcode.
    """
    match = PC4_RE.search(postcode or "")
    if match:
        pc4 = match.group(1)
        point = _cached(f"pc4:{pc4}", pc4, "postcode")
        if point:
            return point
    return locate_town(city)


def resolve_areas(areas):
    """
    Turn the configured areas into anchors we can measure against.

    Anything we cannot place on the map is dropped with a warning rather than
    silently ignored, because an unplaceable anchor means a whole region of
    the search quietly stops working.
    """
    resolved = []
    for area in areas or []:
        place = area.get("place")
        radius = area.get("radius_km")
        if not place or not radius:
            log.warning("area %r needs both 'place' and 'radius_km'", area)
            continue
        point = locate_town(place)
        if not point:
            log.warning("skipping area %r: could not locate it", place)
            continue
        resolved.append({"place": place, "radius_km": float(radius), "point": point})
    return resolved


def area_match(listing, areas):
    """
    The first area this listing falls inside, or None.

    Returns the area dict itself so the caller can say which one matched.
    """
    if not areas:
        return None
    point = locate_listing(listing.get("postcode"), listing.get("city"))
    if not point:
        return None
    for area in areas:
        if distance_km(point, area["point"]) <= area["radius_km"]:
            return area
    return None
