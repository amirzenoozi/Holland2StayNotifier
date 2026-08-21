"""
Holland2Stay notifier.

One cycle:
  sitemap -> new url_keys -> resolve city (cached) -> fetch details for the
  cities we care about -> post to a Telegram topic.

The first ever run records everything silently so the group does not get 200
messages at once.
"""

import json
import logging
import os
import sys
import time

import source
import store
from telegram import TelegramBot

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("h2s")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def house_to_msg(listing):
    lines = [f"🏠 {listing.get('address') or listing['url_key']}"]

    if listing.get("city"):
        lines.append(f"📍 {listing['city']}")

    excl, incl = listing.get("price_excl"), listing.get("price_incl")
    if excl and incl:
        lines.append(f"💶 €{excl} excl. / €{incl} incl.")
    elif excl or incl:
        lines.append(f"💶 €{excl or incl}")

    shape = " · ".join(
        part for part in (
            f"{listing['area']} m²" if listing.get("area") else None,
            listing.get("dwelling_type"),
            listing.get("occupancy"),
        ) if part
    )
    if shape:
        lines.append(f"📐 {shape}")

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
        lines.append(f"ℹ️ {', '.join(flags)}")

    lines.append(listing["url"])
    return "\n".join(lines)


def run_cycle(config, notifier, debug):
    targets = {city.strip().lower() for city in config["cities"]}
    max_new = config.get("max_new_per_cycle", 25)
    max_lookups = config.get("max_lookups_per_cycle", 15)

    store.init()
    known = store.known_keys()
    current = source.fetch_listing_keys()
    new_keys = sorted(current - known)

    log.info("sitemap: %d listings, %d new", len(current), len(new_keys))

    if not known:
        store.record_many(current, notified=True)
        log.info("first run: seeded %d listings silently", len(current))
        if debug:
            debug.send_simple_msg(
                f"H2Stay notifier started. Seeded {len(current)} existing listings "
                f"silently; you will only be notified about new ones.\n"
                f"Watching: {', '.join(config['cities'])}"
            )
        return

    if not new_keys:
        return

    if len(new_keys) > max_new:
        # Either the site republished everything or our DB is out of sync.
        # Record without spending credits and flag it rather than spamming.
        store.record_many(new_keys, notified=True)
        log.warning("%d new keys exceeds max_new_per_cycle=%d, absorbed silently",
                    len(new_keys), max_new)
        if debug:
            debug.send_simple_msg(
                f"H2Stay: {len(new_keys)} new listings appeared at once "
                f"(limit {max_new}). Absorbed without notifying to avoid spam."
            )
        return

    lookups = 0
    for url_key in new_keys:
        prefix = source.street_prefix(url_key)
        city = store.street_city(prefix)

        if city and city.lower() not in targets:
            # Known street in a city we do not watch: free to skip.
            store.record(url_key, city=city, notified=True)
            continue

        if lookups >= max_lookups:
            log.warning("lookup budget reached, deferring %s to next cycle", url_key)
            break

        try:
            listing = source.fetch_listing(url_key)
            lookups += 1
        except source.FetchError as exc:
            log.error("could not fetch %s: %s", url_key, exc)
            continue

        city = listing.get("city")
        store.learn_street(prefix, city)

        if city and city.lower() in targets:
            response = notifier.send_simple_msg(house_to_msg(listing))
            ok = getattr(response, "ok", False)
            log.info("notified %s (%s) ok=%s", url_key, city, ok)
            store.record(url_key, city=city, notified=ok)
            time.sleep(3)  # stay well under Telegram's ~20 msg/min group limit
        else:
            log.info("skipping %s (%s) - not a watched city", url_key, city)
            store.record(url_key, city=city, notified=True)


def main():
    config = load_config()

    api_key = os.environ.get("TELEGRAM_API_KEY")
    if not api_key:
        log.error("TELEGRAM_API_KEY is not set")
        return 1

    telegram_config = config["telegram"]
    notifier = TelegramBot(
        apikey=api_key,
        chat_id=telegram_config["chat_id"],
        message_thread_id=telegram_config.get("topic_id"),
    )

    debug_chat = os.environ.get("DEBUGGING_CHAT_ID")
    debug = TelegramBot(apikey=api_key, chat_id=debug_chat) if debug_chat else None

    try:
        run_cycle(config, notifier, debug)
    except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the loop
        log.exception("cycle failed")
        if debug:
            debug.send_simple_msg(f"H2Stay notifier error: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
