"""
Turn the old single-chat config.json into subscription #1.

Before multi-group support the whole config described one chat. On the first
start after upgrading, that chat becomes an ordinary subscription so nothing
changes for its members. Cities are filed per source (Location.source) because
the old config kept separate lists per source - merging them would multiply
Firecrawl searches. The price filter, which used to live inside each paid
source's searches, becomes the group's single widest range; /price changes it.

Runs only when no subscription exists, so it never overwrites later edits.
"""

import logging

import registry
import store
import subscriptions
from subscriptions import PAID_SOURCES

log = logging.getLogger("migration")


def _km(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).lower().removesuffix("km"))
    except ValueError:
        return None


def _price_range(text):
    try:
        low, high = str(text).split("-", 1)
        return int(low or 0), int(high) if high else None
    except ValueError:
        return None


def migrate_from_config(config):
    if subscriptions.has_any():
        return False
    telegram = config.get("telegram", {})
    chat_id = telegram.get("chat_id")
    if chat_id is None:
        return False

    timezone = (
        store.get_setting("timezone") or telegram.get("timezone") or subscriptions.DEFAULT_TIMEZONE
    )
    chat_id = int(chat_id)
    subscriptions.create(chat_id, telegram.get("topic_id"), "Owner group", timezone)

    for key in registry.CONFIG_KEYS:
        enabled = config.get(key, {}).get("enabled", False)
        enabled = enabled and store.get_setting(f"source:{key}", "1") == "1"
        subscriptions.set_source(chat_id, key, enabled)
    if store.get_setting("paused", "0") == "1":
        subscriptions.set_paused(chat_id, True)

    def add(city, radius, source):
        subscriptions.add_location(chat_id, str(city).strip(), radius, source, enforce=False)

    for city in config.get("holland2stay", {}).get("cities", []):
        add(city, None, "holland2stay")

    lows, highs = [], []
    for key in PAID_SOURCES:
        block = config.get(key, {})
        for search in block.get("searches", []):
            if not search.get("area"):
                log.warning("%s search %r has no 'area' (raw url?) - not migrated", key, search.get("name"))
                continue
            add(search["area"].title(), _km(search.get("radius")), key)
            span = _price_range(search.get("price")) if block.get("enabled") else None
            if span:
                lows.append(span[0])
                highs.append(span[1])
    if lows:
        low = min(lows) or None
        high = None if None in highs else max(highs)
        subscriptions.set_price(chat_id, low, high)

    ikwilhuren = config.get("ikwilhuren", {})
    for area in ikwilhuren.get("areas", []):
        if area.get("place") and area.get("radius_km"):
            add(area["place"], float(area["radius_km"]), "ikwilhuren")
    for city in ikwilhuren.get("cities", []):
        add(city, None, "ikwilhuren")

    subscriptions.rebuild_searches()
    for key in PAID_SOURCES:
        if store.known_keys(source=registry.CONFIG_TO_STORE[key]):
            subscriptions.mark_source_seeded(key)
    log.info("migrated config.json into subscription for chat %s", chat_id)
    return True
