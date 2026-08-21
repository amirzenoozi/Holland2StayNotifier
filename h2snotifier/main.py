"""
Poll every enabled source once and notify about listings we have not seen.

Each source is diffed independently against the database, so enabling a new
one only floods that source's own first run - which we absorb silently.
"""

import json
import logging
import os
import sys
import time

import funda
import h2s
import store
from fetcher import FetchError
from telegram import TelegramBot

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

SOURCE_LABEL = {"h2s": "Holland2Stay", "funda": "Funda"}


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def listing_to_msg(listing):
    """Render one listing. Both sources produce the same field names."""
    label = SOURCE_LABEL.get(listing.get("source"), "")
    lines = [f"🏠 {listing.get('address') or listing['url_key']}"]

    where = listing.get("city") or ""
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

    facts = []
    if listing.get("area"):
        facts.append(f"{listing['area']} m²")
    if listing.get("rooms"):
        facts.append(f"{listing['rooms']} rooms")
    for key in ("dwelling_type", "occupancy"):
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

    lines.append(listing["url"])
    return "\n".join(lines)


def _seed(source, keys, debug):
    """First run for a source: remember everything, tell nobody."""
    store.record_many(keys, notified=True, source=source)
    log.info("%s: first run, seeded %d listings silently", source, len(keys))
    if debug:
        debug.send_simple_msg(
            f"{SOURCE_LABEL.get(source, source)} enabled. Seeded {len(keys)} "
            "existing listings silently - you will only hear about new ones."
        )


def _notify(listing, notifier, city=None):
    response = notifier.send_simple_msg(listing_to_msg(listing))
    ok = getattr(response, "ok", False)
    log.info("notified %s (%s) ok=%s", listing["url_key"], city or listing.get("city"), ok)
    time.sleep(3)
    return ok


def run_h2s(config, notifier, debug, max_new):
    """
    Holland2Stay: the sitemap gives keys only, so the city lives behind a
    per-listing fetch. We cache street -> city to keep that fetch rare.
    """
    targets = {c.strip().lower() for c in config.get("cities", [])}
    max_lookups = config.get("max_lookups_per_cycle", 15)

    known = store.known_keys(source=h2s.NAME)
    current = h2s.fetch_listing_keys()
    new_keys = sorted(current - known)
    log.info("h2s: %d listings, %d new", len(current), len(new_keys))

    if not known:
        return _seed(h2s.NAME, current, debug)
    if not new_keys:
        return
    if len(new_keys) > max_new:
        store.record_many(new_keys, notified=True, source=h2s.NAME)
        log.warning("h2s: %d new listings exceeds max_new_per_cycle", len(new_keys))
        if debug:
            debug.send_simple_msg(
                f"Holland2Stay returned {len(new_keys)} new listings at once - "
                "absorbed silently to avoid spam."
            )
        return

    lookups = 0
    for url_key in new_keys:
        prefix = h2s.street_prefix(url_key)
        city = store.street_city(prefix)

        if city and city.lower() not in targets:
            store.record(url_key, city=city, notified=True, source=h2s.NAME)
            continue
        if lookups >= max_lookups:
            log.warning("h2s: lookup budget reached, deferring the rest")
            break

        try:
            listing = h2s.fetch_listing(url_key)
        except FetchError as exc:
            log.error("h2s: could not fetch %s: %s", url_key, exc)
            continue
        lookups += 1

        city = listing.get("city")
        if city:
            store.learn_street(prefix, city)

        if city and city.lower() in targets:
            ok = _notify(listing, notifier, city)
            store.record(url_key, city=city, notified=ok, source=h2s.NAME)
        else:
            log.info("h2s: skipping %s (%s) - not a watched city", url_key, city)
            store.record(url_key, city=city, notified=True, source=h2s.NAME)


def run_funda(config, notifier, debug, max_new):
    """Funda: one search page carries everything, so no detail fetches."""
    searches = config.get("searches", [])
    if not searches:
        log.warning("funda: enabled but no searches configured")
        return

    known = store.known_keys(source=funda.NAME)
    found = {}
    for search in searches:
        try:
            found.update(funda.fetch_search(search))
        except FetchError as exc:
            log.error("funda: search %r failed: %s", search.get("name"), exc)

    if not found:
        return

    new_keys = sorted(set(found) - known)
    log.info("funda: %d listings, %d new", len(found), len(new_keys))

    if not known:
        return _seed(funda.NAME, set(found), debug)
    if not new_keys:
        return
    if len(new_keys) > max_new:
        store.record_many(new_keys, notified=True, source=funda.NAME)
        log.warning("funda: %d new listings exceeds max_new_per_cycle", len(new_keys))
        if debug:
            debug.send_simple_msg(
                f"Funda returned {len(new_keys)} new listings at once - "
                "absorbed silently to avoid spam."
            )
        return

    for url_key in new_keys:
        listing = found[url_key]
        ok = _notify(listing, notifier)
        store.record(url_key, city=listing.get("city"), notified=ok, source=funda.NAME)


def run_cycle(config, notifier, debug):
    store.init()
    max_new = config.get("max_new_per_cycle", 25)
    failures = []

    for name, runner in (("holland2stay", run_h2s), ("funda", run_funda)):
        source_config = config.get(name, {})
        if not source_config.get("enabled", False):
            continue
        try:
            runner(source_config, notifier, debug, max_new)
        except Exception as exc:  # one bad source must not stop the other
            log.exception("%s: cycle failed", name)
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    if failures and debug:
        debug.send_simple_msg("Notifier errors:\n" + "\n".join(failures))


def main():
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        log.error("could not read %s: %s", CONFIG_PATH, exc)
        return 1

    apikey = os.environ.get("TELEGRAM_API_KEY")
    if not apikey:
        log.error("TELEGRAM_API_KEY is not set")
        return 1

    telegram_config = config.get("telegram", {})
    notifier = TelegramBot(
        apikey,
        chat_id=telegram_config["chat_id"],
        message_thread_id=telegram_config.get("topic_id"),
    )

    debug_chat = os.environ.get("DEBUGGING_CHAT_ID")
    debug = TelegramBot(apikey, chat_id=debug_chat) if debug_chat else None

    try:
        run_cycle(config, notifier, debug)
    except Exception as exc:
        log.exception("cycle failed")
        if debug:
            debug.send_simple_msg(f"Notifier error: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
