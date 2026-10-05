"""Render a listing as a Telegram message and its link button."""

import re
import unicodedata

import registry

SOURCE_LABEL = registry.LABELS


def city_tag(city):
    """
    Turn a city name into a Telegram hashtag you can tap to filter.

    Telegram only accepts letters, digits and underscores in a tag, so
    "Capelle aan den IJssel" has to become #CapelleAanDenIJssel. We upper
    the first letter of each word rather than title-casing, which would
    wreck the Dutch "IJ" digraph. Province suffixes like "(UT)" are
    dropped so the same town always produces the same tag.
    """
    if not city:
        return ""
    plain = re.sub(r"\(.*?\)", " ", str(city))
    plain = unicodedata.normalize("NFKD", plain)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    words = [w for w in re.split(r"[^0-9A-Za-z]+", plain) if w]
    if not words:
        return ""
    return "#" + "".join(w[0].upper() + w[1:] for w in words)


def listing_to_msg(listing):
    """Render one listing. Both sources produce the same field names."""
    label = SOURCE_LABEL.get(listing.get("source"), "")
    lines = [f"🏠 {listing.get('address') or listing['url_key']}"]

    # The city goes out as a hashtag so you can tap it to see every
    # listing we ever sent for that town.
    where = city_tag(listing.get("city"))
    if listing.get("postcode"):
        where = f"{listing['postcode']} {where}".strip()
    if where:
        lines.append(f"📍 {where}" + (f"  ·  {label}" if label else ""))
    elif label:
        lines.append(f"📍 {label}")

    excl, incl = listing.get("price_excl"), listing.get("price_incl")
    if excl and incl:
        lines.append(f"💶 €{excl} excl. / €{incl} incl.")
    elif excl or incl:
        lines.append(f"💶 €{excl or incl}")
    elif listing.get("price_on_request"):
        lines.append("💶 Price on request")

    facts = []
    if listing.get("area"):
        facts.append(f"{listing['area']} m²")
    if listing.get("rooms"):
        facts.append(f"{listing['rooms']} rooms")
    for key in ("dwelling_type", "occupancy", "interior"):
        if listing.get(key):
            facts.append(str(listing[key]))
    if listing.get("energy"):
        facts.append(f"energy {listing['energy']}")
    if facts:
        lines.append("📐 " + "  ·  ".join(facts))

    if listing.get("available_from"):
        lines.append(f"📅 Available from {listing['available_from']}")

    flags = []
    if listing.get("student_only"):
        flags.append("students only")
    if listing.get("lottery"):
        flags.append("lottery")
    if listing.get("signees"):
        flags.append(f"{listing['signees']} signees already")
    if flags:
        lines.append("ℹ️ " + ", ".join(flags))

    # The URL is not repeated in the body - it lives in the inline button.
    return "\n".join(lines)


def listing_button(listing):
    """Inline button taking you straight to the listing page."""
    label = SOURCE_LABEL.get(listing.get("source"), "listing")
    return (f"🔗 View on {label}", listing["url"])


