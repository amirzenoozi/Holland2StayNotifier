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
