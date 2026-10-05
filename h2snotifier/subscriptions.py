"""
Per-group subscriptions: who gets alerts, where they go, and which filters apply.

A group is a row in `subscriptions`; its cities live in `sub_locations` and the
sources it listens to in `sub_sources`. Everything else in the notifier asks
this module rather than touching those tables.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, replace

import registry
import store

DEFAULT_TIMEZONE = "Europe/Amsterdam"
MAX_CITIES = int(os.environ.get("MAX_CITIES_PER_GROUP", "5"))
MAX_SEARCHES = int(os.environ.get("MAX_DISTINCT_SEARCHES", "30"))

# Sources that cost a Firecrawl credit per fetch.
PAID_SOURCES = ("funda", "huurwoningen", "pararius")


class LimitError(Exception):
    """A group asked for more than the limits allow."""


@dataclass(frozen=True)
class Location:
    city: str
    radius_km: float = None
    # "" applies to every source; otherwise one config key. Only the migrated
    # owner group uses a specific source - it keeps the per-source lists the
    # old config.json had.
    source: str = ""


@dataclass
class Subscription:
    chat_id: int
    topic_id: int
    title: str
    price_min: int
    price_max: int
    timezone: str
    paused: bool
    active: bool
    locations: list = field(default_factory=list)
    sources: set = field(default_factory=set)

    def locations_for(self, config_key):
        return [loc for loc in self.locations if loc.source in ("", config_key)]


_COLUMNS = "chat_id, topic_id, title, price_min, price_max, timezone, paused, active"


def _build(connection, row):
    chat_id = row[0]
    locations = [
        Location(city, radius, source)
        for city, radius, source in connection.execute(
            "SELECT city, radius_km, source FROM sub_locations WHERE chat_id = ? ORDER BY rowid",
            (chat_id,),
        )
    ]
    sources = {
        source
        for (source,) in connection.execute(
            "SELECT source FROM sub_sources WHERE chat_id = ? AND enabled = 1", (chat_id,)
        )
    }
    return Subscription(
        chat_id=row[0], topic_id=row[1], title=row[2], price_min=row[3],
        price_max=row[4], timezone=row[5], paused=bool(row[6]), active=bool(row[7]),
        locations=locations, sources=sources,
    )


def get(chat_id):
    with store.connect() as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM subscriptions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return _build(connection, row) if row else None


def get_active(chat_id):
    sub = get(chat_id)
    return sub if sub and sub.active else None


def all_active():
    with store.connect() as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM subscriptions WHERE active = 1 ORDER BY created_at, chat_id"
        ).fetchall()
        return [_build(connection, row) for row in rows]


def has_any():
    with store.connect() as connection:
        return connection.execute("SELECT 1 FROM subscriptions LIMIT 1").fetchone() is not None


def _create(connection, chat_id, topic_id, title, timezone):
    connection.execute("DELETE FROM sub_locations WHERE chat_id = ?", (chat_id,))
    connection.execute("DELETE FROM sub_sources WHERE chat_id = ?", (chat_id,))
    connection.execute(
        "INSERT OR REPLACE INTO subscriptions"
        " (chat_id, topic_id, title, timezone, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, topic_id, title, timezone, store.now()),
    )
    connection.executemany(
        "INSERT INTO sub_sources (chat_id, source, enabled) VALUES (?, ?, 1)",
        [(chat_id, key) for key in registry.CONFIG_KEYS],
    )


def create(chat_id, topic_id, title, timezone=DEFAULT_TIMEZONE):
    """Create (or reset) a group's subscription with every source on."""
    with store.connect() as connection:
        _create(connection, chat_id, topic_id, title, timezone)
    return get(chat_id)


def create_invite():
    code = secrets.token_urlsafe(6)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO invites (code, created_at) VALUES (?, ?)", (code, store.now())
        )
    return code


def activate(code, chat_id, topic_id, title):
    """
    Redeem an invite and create the subscription in one transaction.

    The UPDATE only matches an unused code, so two groups racing for the same
    code cannot both win. Returns None for an unknown or used code.
    """
    with store.connect() as connection:
        redeemed = connection.execute(
            "UPDATE invites SET used_by_chat = ?, used_at = ?"
            " WHERE code = ? AND used_by_chat IS NULL",
            (chat_id, store.now(), code),
        ).rowcount
        if redeemed != 1:
            return None
        _create(connection, chat_id, topic_id, title, DEFAULT_TIMEZONE)
    return get(chat_id)


def _update(chat_id, column, value):
    with store.connect() as connection:
        connection.execute(
            f"UPDATE subscriptions SET {column} = ? WHERE chat_id = ?", (value, chat_id)
        )


def set_topic(chat_id, topic_id):
    _update(chat_id, "topic_id", topic_id)


def set_paused(chat_id, paused):
    _update(chat_id, "paused", 1 if paused else 0)


def set_timezone(chat_id, name):
    _update(chat_id, "timezone", name)


def deactivate(chat_id):
    _update(chat_id, "active", 0)


def set_price(chat_id, low, high):
    with store.connect() as connection:
        connection.execute(
            "UPDATE subscriptions SET price_min = ?, price_max = ? WHERE chat_id = ?",
            (low, high, chat_id),
        )


def set_source(chat_id, source, enabled):
    with store.connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO sub_sources (chat_id, source, enabled) VALUES (?, ?, ?)",
            (chat_id, source, 1 if enabled else 0),
        )


def move_chat(old_id, new_id):
    """
    A group upgraded to a supergroup gets a new chat id. Carry everything over.

    Reactivates the row: the upgrade proves the bot is still in the group, and a
    stray "left" update for the old id may have deactivated it first.
    """
    with store.connect() as connection:
        for table in ("sub_locations", "sub_sources", "deliveries"):
            connection.execute(
                f"UPDATE {table} SET chat_id = ? WHERE chat_id = ?", (new_id, old_id)
            )
        connection.execute(
            "UPDATE subscriptions SET chat_id = ?, active = 1 WHERE chat_id = ?",
            (new_id, old_id),
        )


# The radius choices each site's own search offers. A requested radius is
# rounded UP to the next step so the fetch never misses a listing; the exact
# distance is enforced afterwards by matcher. Funda's steps are its UI's;
# Pararius/Huurwoningen reuse the values the project's config already used
# (10, 15, 20, 25) - verify against the sites if a search 404s.
RADIUS_STEPS = {
    "funda": (1, 2, 5, 10, 15, 30, 50),
    "pararius": (1, 2, 5, 10, 15, 20, 25, 50),
    "huurwoningen": (1, 2, 5, 10, 15, 20, 25, 50),
}


def radius_bucket(source, km):
    """The radius to fetch with. 0.0 means "just that city"."""
    if not km:
        return 0.0
    steps = RADIUS_STEPS.get(source)
    if not steps:
        return float(km)
    for step in steps:
        if km <= step:
            return float(step)
    return float(steps[-1])


@dataclass(frozen=True)
class Search:
    city: str          # lowercase
    radius_km: float   # 0.0 = exact city
    seeded: bool

    def fetch_args(self):
        """The dict the source modules' fetch_search() expects."""
        args = {
            "name": f"{self.city} {self.radius_km:g}km" if self.radius_km else self.city,
            "area": self.city.replace(" ", "-"),
        }
        if self.radius_km:
            args["radius"] = f"{self.radius_km:g}km"
        return args


def _search_set(subs):
    keys = set()
    for sub in subs:
        for source in PAID_SOURCES:
            if source not in sub.sources:
                continue
            for loc in sub.locations_for(source):
                keys.add((source, loc.city.strip().lower(), radius_bucket(source, loc.radius_km)))
    return keys


def add_location(chat_id, city, radius_km=None, source="", enforce=True):
    """
    Add or replace one city. `enforce` applies the per-group and global caps;
    migration passes False so an existing setup is never rejected.
    """
    city = city.strip()
    if enforce:
        sub = get_active(chat_id)
        if sub is not None:
            same = lambda loc: loc.city.lower() == city.lower() and loc.source == source
            if not any(same(loc) for loc in sub.locations) and source == "":
                owned = [loc for loc in sub.locations if loc.source == ""]
                if len(owned) >= MAX_CITIES:
                    raise LimitError(f"A group can watch at most {MAX_CITIES} cities.")
            others = [s for s in all_active() if s.chat_id != chat_id]
            kept = [loc for loc in sub.locations if not same(loc)]
            before = _search_set(others + [sub])
            after = _search_set(
                others + [replace(sub, locations=kept + [Location(city, radius_km, source)])]
            )
            if len(after) > MAX_SEARCHES and len(after) > len(before):
                raise LimitError(
                    "The bot is at its limit of distinct searches - ask the owner to raise it."
                )
    with store.connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO sub_locations (chat_id, city, radius_km, source)"
            " VALUES (?, ?, ?, ?)",
            (chat_id, city, radius_km, source),
        )


def remove_location(chat_id, city):
    """Remove a city from every source it was set for. False if it was not there."""
    with store.connect() as connection:
        return connection.execute(
            "DELETE FROM sub_locations WHERE chat_id = ? AND city = ?", (chat_id, city.strip())
        ).rowcount > 0


def rebuild_searches():
    """Make the `searches` table match what active groups currently need."""
    wanted = _search_set(all_active())
    with store.connect() as connection:
        have = {
            (source, city.lower(), radius)
            for source, city, radius in connection.execute(
                "SELECT source, city, radius_km FROM searches"
            )
        }
        for key in have - wanted:
            connection.execute(
                "DELETE FROM searches WHERE source = ? AND city = ? AND radius_km = ?", key
            )
        for key in wanted - have:
            connection.execute(
                "INSERT INTO searches (source, city, radius_km, seeded) VALUES (?, ?, ?, 0)", key
            )
    return wanted


def searches_for(config_key):
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT city, radius_km, seeded FROM searches WHERE source = ? ORDER BY city, radius_km",
            (config_key,),
        ).fetchall()
    return [Search(city.lower(), radius, bool(seeded)) for city, radius, seeded in rows]


def mark_seeded(config_key, search):
    with store.connect() as connection:
        connection.execute(
            "UPDATE searches SET seeded = 1 WHERE source = ? AND city = ? AND radius_km = ?",
            (config_key, search.city, search.radius_km),
        )


def mark_source_seeded(config_key):
    """For migration: the source already has history, so nothing needs seeding."""
    with store.connect() as connection:
        connection.execute("UPDATE searches SET seeded = 1 WHERE source = ?", (config_key,))


def wants(config_key, subs):
    """Does any group want this source at all? If not, skip fetching it."""
    return any(config_key in sub.sources and sub.locations_for(config_key) for sub in subs)


def cities_for(config_key, subs):
    """Lowercase city names any group watches for this source."""
    return {
        loc.city.strip().lower()
        for sub in subs
        if config_key in sub.sources
        for loc in sub.locations_for(config_key)
    }
