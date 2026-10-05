"""
Does one listing belong in one group's chat?

Pure apart from the geocoder cache: no Telegram, no fetching. Everything a
group can configure is decided here and nowhere else.
"""

import re

import geo
import registry


def parse_price(value):
    """
    Whole euros from whatever the sites print.

    Handles "1.250", "1,250", "1.250,00", "1550.00" and "€ 950". A separator
    followed by one or two digits at the end is cents (a thousands group always
    has three); every other separator is a thousands mark.
    """
    if value is None:
        return None
    text = re.sub(r"[^\d.,]", "", str(value))
    text = re.sub(r"[.,]\d{1,2}$", "", text)
    digits = re.sub(r"[.,]", "", text)
    return int(digits) if digits else None


def _price_ok(listing, sub):
    price = parse_price(listing.get("price_excl"))
    if price is None:
        price = parse_price(listing.get("price_incl"))
    if price is None:
        return True
    if sub.price_min is not None and price < sub.price_min:
        return False
    if sub.price_max is not None and price > sub.price_max:
        return False
    return True


def _within(listing, location):
    city = (listing.get("city") or "").strip().lower()
    if city and city == location.city.strip().lower():
        return True
    # Holland2Stay only tells us a city name, so a radius means nothing there.
    if not location.radius_km or listing.get("source") == "h2s":
        return False
    anchor = geo.locate_town(location.city)
    point = geo.locate_listing(listing.get("postcode"), listing.get("city"))
    if not anchor or not point:
        return False
    return geo.distance_km(point, anchor) <= location.radius_km


def matches(listing, sub):
    config_key = registry.STORE_TO_CONFIG.get(listing.get("source"))
    if not sub.active or sub.paused or config_key not in sub.sources:
        return False
    if not _price_ok(listing, sub):
        return False
    return any(_within(listing, loc) for loc in sub.locations_for(config_key))
