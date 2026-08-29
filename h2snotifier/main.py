"""
Poll every enabled source once and notify about listings we have not seen.

Each source is diffed independently against the database, so enabling a new
one only floods that source's own first run - which we absorb silently.
After that seed every new listing is sent, however many turn up at once.
"""

import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from functools import partial

import control
import funda
import h2s
import huurwoningen
import ikwilhuren
import registry
import store
from fetcher import FetchError, SiteUnavailable
from telegram import TelegramBot

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

SOURCE_LABEL = registry.LABELS


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


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
    response = notifier.send_simple_msg(
        listing_to_msg(listing), button=listing_button(listing)
    )
    ok = getattr(response, "ok", False)
    log.info("notified %s (%s) ok=%s", listing["url_key"], city or listing.get("city"), ok)
    time.sleep(3)
    return ok


def run_h2s(config, notifier, debug):
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

    lookups = 0
    for url_key in new_keys:
        prefix = h2s.street_prefix(url_key)
        city = store.street_city(prefix)

        if city and city.lower() not in targets:
            store.record(url_key, city=city, notified=True, source=h2s.NAME)
            continue
        if lookups >= max_lookups:
            # Deferred keys are deliberately left unrecorded so the next cycle
            # sees them as new and picks up where this one stopped.
            log.warning("h2s: lookup budget spent, deferring %d listings to the next cycle",
                        len(new_keys) - new_keys.index(url_key))
            break

        # Count the attempt, not the success: a page that fails still costs
        # us a minute or more, and a run of failures must not turn one cycle
        # into an hour of retries.
        lookups += 1
        try:
            listing = h2s.fetch_listing(url_key)
        except FetchError as exc:
            # Left unrecorded on purpose - most failures are transient and the
            # next cycle retries them.
            log.error("h2s: could not fetch %s: %s", url_key, exc)
            continue

        city = listing.get("city")
        if city:
            store.learn_street(prefix, city)

        if city and city.lower() in targets:
            ok = _notify(listing, notifier, city)
            store.record(url_key, city=city, notified=ok, source=h2s.NAME)
        else:
            log.info("h2s: skipping %s (%s) - not a watched city", url_key, city)
            store.record(url_key, city=city, notified=True, source=h2s.NAME)


def run_search_source(module, config, notifier, debug):
    """
    Funda and Huurwoningen both put the full listing data on the search page,
    so a cycle is one fetch per configured search and no detail lookups.
    """
    name = module.NAME
    searches = config.get("searches", [])
    if not searches:
        log.warning("%s: enabled but no searches configured", name)
        return

    known = store.known_keys(source=name)
    found = {}
    for search in searches:
        try:
            found.update(module.fetch_search(search))
        except FetchError as exc:
            log.error("%s: search %r failed: %s", name, search.get("name"), exc)

    if not found:
        return

    new_keys = sorted(set(found) - known)
    log.info("%s: %d listings, %d new", name, len(found), len(new_keys))

    if not known:
        return _seed(name, set(found), debug)
    if not new_keys:
        return

    # Everything new goes out, however many there are. _notify paces the sends
    # and telegram.py backs off when Telegram asks it to.
    for url_key in new_keys:
        listing = found[url_key]
        ok = _notify(listing, notifier)
        store.record(url_key, city=listing.get("city"), notified=ok, source=name)


def run_ikwilhuren(config, notifier, debug):
    """
    ikwilhuren.nu hands us the whole country in one free request, so the work
    is filtering rather than fetching: we keep a record of every listing we
    have seen (that is what makes the next diff correct) but only notify about
    the cities being watched.
    """
    name = ikwilhuren.NAME
    targets = {c.strip().lower() for c in config.get("cities", [])}

    known = store.known_keys(source=name)
    found = ikwilhuren.fetch_catalogue()
    new_keys = sorted(set(found) - known)
    log.info("%s: %d listings, %d new", name, len(found), len(new_keys))

    if not known:
        return _seed(name, set(found), debug)
    if not new_keys:
        return

    for url_key in new_keys:
        listing = found[url_key]
        city = listing.get("city") or ""
        if targets and city.strip().lower() not in targets:
            store.record(url_key, city=city, notified=True, source=name)
            continue
        ok = _notify(listing, notifier, city)
        store.record(url_key, city=city, notified=ok, source=name)


def run_cycle(config, notifier, debug):
    store.init()
    failures = []

    runners = (
        ("holland2stay", run_h2s),
        ("funda", partial(run_search_source, funda)),
        ("huurwoningen", partial(run_search_source, huurwoningen)),
        ("ikwilhuren", run_ikwilhuren),
    )
    for name, runner in runners:
        source_config = config.get(name, {})
        # config.json decides what exists; the control panel decides what
        # is listening right now.
        if not control.source_enabled(name, config):
            continue

        # Every fetch of a paid source costs a Firecrawl credit, so a source
        # may ask to be polled less often than the container's own loop.
        wait = source_config.get("min_interval_minutes")
        elapsed = store.minutes_since_run(name) if wait else None
        if wait and elapsed is not None and elapsed < wait:
            log.info("%s: last run %.0f min ago, waiting for %d", name, elapsed, wait)
            continue

        try:
            store.mark_run(name)
            runner(source_config, notifier, debug)
        except SiteUnavailable as exc:
            # Maintenance windows and 5xx blips clear on their own; there is
            # nothing to act on, so stay quiet and try again next cycle.
            log.warning("%s: source unavailable, skipping this cycle (%s)", name, exc)
        except Exception as exc:  # one bad source must not stop the other
            log.exception("%s: cycle failed", name)
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    if failures and debug:
        debug.send_simple_msg("Notifier errors:\n" + "\n".join(failures))


class Scheduler:
    """
    Owns the scrape loop and makes sure only one cycle runs at a time.

    The timer and the /check button both want to start a cycle, and the
    control panel must stay responsive while one is in flight - so cycles
    run under a lock and /check simply declines when the lock is taken.
    """

    def __init__(self, config, notifier, debug, interval):
        self.config = config
        self.notifier = notifier
        self.debug = debug
        self.interval = interval
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        self.stop = threading.Event()

    def cycle(self):
        """Run one cycle if none is running. Returns False if one already is."""
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if control.is_paused():
                log.info("paused, skipping cycle")
                return True
            run_cycle(self.config, self.notifier, self.debug)
        except Exception as exc:
            log.exception("cycle failed")
            if self.debug:
                self.debug.send_simple_msg(f"Notifier error: {type(exc).__name__}: {exc}")
        finally:
            self.lock.release()
        return True

    def request_cycle(self):
        """Ask for a cycle now; used by /check. False means one is running."""
        if self.lock.locked():
            return False
        self.wakeup.set()
        return True

    def loop_forever(self):
        while not self.stop.is_set():
            self.cycle()
            # wakeup lets /check cut the wait short without a second timer.
            self.wakeup.wait(self.interval)
            self.wakeup.clear()


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

    store.init()
    interval = int(os.environ.get("RUN_INTERVAL", 3600))
    scheduler = Scheduler(config, notifier, debug, interval)

    # One-shot mode keeps `docker exec ... python main.py` working for a
    # manual check. It must not poll: two pollers on one token fight, and
    # Telegram answers the loser with 409.
    if os.environ.get("RUN_ONCE") == "1":
        scheduler.cycle()
        return 0

    if not control.admin_ids(config):
        log.warning("no telegram.admin_ids in config - the control panel will refuse everyone")

    controller = control.Controller(notifier, config, scheduler.request_cycle)
    listener = threading.Thread(target=controller.listen_forever, daemon=True)
    listener.start()

    log.info("notifier started, checking every %ss", interval)
    scheduler.loop_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
