"""
Poll every enabled source once and notify about listings we have not seen.

Each source is diffed independently against the database, so enabling a new
one only floods that source's own first run - which we absorb silently.
After that seed every new listing is sent, however many turn up at once.
"""

import json
import logging
import os
import sys
import threading
from functools import partial

import control
import dispatch
import funda
import h2s
import huurwoningen
import ikwilhuren
import migration
import pararius
import registry
import store
import subscriptions
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


def _seed(source, keys, debug):
    """First run for a source: remember everything, tell nobody."""
    store.record_many(keys, notified=True, source=source)
    log.info("%s: first run, seeded %d listings silently", source, len(keys))
    if debug:
        debug.send_simple_msg(
            f"{SOURCE_LABEL.get(source, source)} enabled. Seeded {len(keys)} "
            "existing listings silently - you will only hear about new ones."
        )


def run_h2s(config, subs, bot, debug):
    """
    Holland2Stay: the sitemap gives keys only, so the city lives behind a
    per-listing fetch. We cache street -> city to keep that fetch rare, and
    only look up cities that at least one group watches.
    """
    targets = subscriptions.cities_for("holland2stay", subs)
    if not targets:
        return {"seen": 0, "new": 0, "sent": 0, "note": "no group watches it"}
    max_lookups = config.get("max_lookups_per_cycle", 15)

    known = store.known_keys(source=h2s.NAME)
    current = h2s.fetch_listing_keys()
    new_keys = sorted(current - known)
    log.info("h2s: %d listings, %d new", len(current), len(new_keys))

    if not known:
        _seed(h2s.NAME, current, debug)
        return {"seen": len(current), "new": 0, "sent": 0, "note": "seeded"}
    if not new_keys:
        return {"seen": len(current), "new": 0, "sent": 0}

    sent = 0
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
            sent += dispatch.fan_out(listing, subs, bot)
        else:
            log.info("h2s: skipping %s (%s) - not a watched city", url_key, city)
        store.record(url_key, city=city, notified=True, source=h2s.NAME)

    return {"seen": len(current), "new": len(new_keys), "sent": sent}


def run_search_source(module, key, config, subs, bot, debug):
    """
    Funda, Pararius and Huurwoningen put the full listing data on the search
    page. The searches are the deduplicated set every group needs (see
    subscriptions.rebuild_searches), so a city two groups watch is fetched once.

    A search seen for the first time is absorbed silently: adding a city to a
    group must not replay that city's whole back catalogue.
    """
    name = module.NAME
    searches = subscriptions.searches_for(key)
    if not searches:
        return {"seen": 0, "new": 0, "sent": 0, "note": "no group watches it"}

    known = store.known_keys(source=name)
    fetched = {}
    failed = 0
    for search in searches:
        try:
            fetched[search] = module.fetch_search(search.fetch_args())
        except FetchError as exc:
            failed += 1
            log.error("%s: search %r failed: %s", name, search.city, exc)

    if not any(fetched.values()):
        return {"seen": 0, "new": 0, "sent": 0, "note": "no results"}

    found = {}
    for search, listings in fetched.items():
        if search.seeded:
            found.update(listings)
    new_keys = sorted(set(found) - known)
    log.info("%s: %d listings, %d new", name, len(found), len(new_keys))

    sent = 0
    for url_key in new_keys:
        listing = found[url_key]
        sent += dispatch.fan_out(listing, subs, bot)
        store.record(url_key, city=listing.get("city"), notified=True, source=name)

    fresh = {search: listings for search, listings in fetched.items() if not search.seeded}
    fresh_keys = {k for listings in fresh.values() for k in listings}
    if fresh:
        _seed(name, fresh_keys, debug)
        for search in fresh:
            subscriptions.mark_seeded(key, search)

    note = f"{failed} search(es) failed" if failed else None
    return {"seen": len(set(found) | fresh_keys), "new": len(new_keys), "sent": sent, "note": note}


def run_ikwilhuren(config, subs, bot, debug):
    """
    ikwilhuren.nu hands us the whole country in one free request, so the work
    is deciding who each new listing is for. Each group's cities and radii
    (matcher) say which; the circles are the useful part - "within 40 km of
    Nijkerk" catches villages nobody would think to name.
    """
    name = ikwilhuren.NAME
    if not subscriptions.wants("ikwilhuren", subs):
        return {"seen": 0, "new": 0, "sent": 0, "note": "no group watches it"}

    known = store.known_keys(source=name)
    found = ikwilhuren.fetch_catalogue()
    new_keys = sorted(set(found) - known)
    log.info("%s: %d listings, %d new", name, len(found), len(new_keys))

    if not known:
        _seed(name, set(found), debug)
        return {"seen": len(found), "new": 0, "sent": 0, "note": "seeded"}
    if not new_keys:
        return {"seen": len(found), "new": 0, "sent": 0}

    sent = 0
    for url_key in new_keys:
        listing = found[url_key]
        sent += dispatch.fan_out(listing, subs, bot)
        store.record(url_key, city=listing.get("city"), notified=True, source=name)
    return {"seen": len(found), "new": len(new_keys), "sent": sent}


def run_cycle(config, bot, debug, force=False):
    """
    Poll every source someone wants once and return what each one did.

    `force` is what the owner's /check sets: the per-source interval that
    exists to save credits is stood down for that one run. Other groups' /check
    never forces, so they cannot burn credits.
    """
    store.init()
    subscriptions.rebuild_searches()
    subs = subscriptions.all_active()
    dispatch.retry_pending(subs, bot)
    failures = []
    results = {}

    runners = (
        ("holland2stay", run_h2s),
        ("funda", partial(run_search_source, funda, "funda")),
        ("huurwoningen", partial(run_search_source, huurwoningen, "huurwoningen")),
        ("pararius", partial(run_search_source, pararius, "pararius")),
        ("ikwilhuren", run_ikwilhuren),
    )
    for name, runner in runners:
        source_config = config.get(name, {})
        # config.json says which sources exist at all; groups say who wants them.
        if not source_config.get("enabled", False) or not subscriptions.wants(name, subs):
            results[name] = {"skipped": "off"}
            continue

        # Every fetch of a paid source costs a Firecrawl credit, so a source
        # may ask to be polled less often than the container's own loop.
        wait = source_config.get("min_interval_minutes")
        elapsed = store.minutes_since_run(name) if wait else None
        if not force and wait and elapsed is not None and elapsed < wait:
            log.info("%s: last run %.0f min ago, waiting for %d", name, elapsed, wait)
            results[name] = {"skipped": f"waiting {wait - int(elapsed)} min"}
            continue

        try:
            store.mark_run(name)
            results[name] = runner(source_config, subs, bot, debug) or {}
        except SiteUnavailable as exc:
            # Maintenance windows and 5xx blips clear on their own; there is
            # nothing to act on, so stay quiet and try again next cycle.
            log.warning("%s: source unavailable, skipping this cycle (%s)", name, exc)
            results[name] = {"skipped": "site unavailable"}
        except Exception as exc:  # one bad source must not stop the other
            log.exception("%s: cycle failed", name)
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            results[name] = {"error": type(exc).__name__}

    if failures and debug:
        debug.send_simple_msg("Notifier errors:\n" + "\n".join(failures))
    return results


def summarise(results):
    """Turn a cycle's results into the reply /check sends back."""
    lines = ["✅ Check finished"]
    total = 0
    for name, result in results.items():
        label = registry.label(name)
        if "error" in result:
            lines.append(f"⚠️ {label}: {result['error']}")
        elif "skipped" in result:
            lines.append(f"⏭ {label}: {result['skipped']}")
        else:
            sent = result.get("sent", 0)
            total += sent
            detail = f"{result.get('seen', 0)} listings, {result.get('new', 0)} new"
            if sent:
                detail += f", {sent} sent"
            if result.get("note"):
                detail += f" ({result['note']})"
            lines.append(f"{'📬' if sent else '•'} {label}: {detail}")

    if not total:
        lines.append("\nNothing new to send.")
    return "\n".join(lines)


class Scheduler:
    """
    Owns the scrape loop and makes sure only one cycle runs at a time.

    The timer and the /check button both want to start a cycle, and the
    control panel must stay responsive while one is in flight - so cycles
    run under a lock and /check simply declines when the lock is taken.
    """

    def __init__(self, config, bot, debug, interval, replies=None):
        self.config = config
        self.bot = bot
        self.debug = debug
        self.interval = interval
        # Shared with the control panel so a check report replaces the
        # "checking now" line instead of stacking another message on it.
        self.replies = replies or control.Replies(bot)
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        self.stop = threading.Event()
        # The reply slot to report into, set by /check and cleared once used.
        self.reply_to = None
        self.force = False

    def cycle(self, force=False):
        """Run one cycle if none is running. Returns False if one already is."""
        if not self.lock.acquire(blocking=False):
            return False
        reply_to, self.reply_to = self.reply_to, None
        force, self.force = force or self.force, False
        try:
            results = run_cycle(self.config, self.bot, self.debug, force=force)
            if reply_to:
                self.replies.send(reply_to, summarise(results))
        except Exception as exc:
            log.exception("cycle failed")
            if reply_to:
                self.replies.send(reply_to, f"⚠️ Check failed: {type(exc).__name__}: {exc}")
            if self.debug:
                self.debug.send_simple_msg(f"Notifier error: {type(exc).__name__}: {exc}")
        finally:
            self.lock.release()
        return True

    def request_cycle(self, reply_to=None, force=False):
        """Ask for a cycle now; used by /check. False means one is running."""
        if self.lock.locked():
            return False
        self.reply_to = reply_to
        self.force = force
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

    bot = TelegramBot(apikey)

    debug_chat = os.environ.get("DEBUGGING_CHAT_ID")
    debug = TelegramBot(apikey, chat_id=debug_chat) if debug_chat else None

    store.init()
    migration.migrate_from_config(config)
    interval = int(os.environ.get("RUN_INTERVAL", 3600))
    replies = control.Replies(bot)
    scheduler = Scheduler(config, bot, debug, interval, replies)

    # One-shot mode keeps `docker exec ... python main.py` working for a
    # manual check. It must not poll: two pollers on one token fight, and
    # Telegram answers the loser with 409.
    if os.environ.get("RUN_ONCE") == "1":
        scheduler.cycle()
        return 0

    if not control.admin_ids(config):
        log.warning("no telegram.admin_ids in config - nobody can issue invites")

    controller = control.Controller(bot, config, scheduler.request_cycle, replies)
    listener = threading.Thread(target=controller.listen_forever, daemon=True)
    listener.start()

    log.info("notifier started, checking every %ss", interval)
    scheduler.loop_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
