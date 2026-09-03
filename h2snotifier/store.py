"""
Persistent state: which listings we have already seen, and which city each
street belongs to.

The street cache is what keeps the credit bill low. Resolving a listing's city
normally requires fetching its detail page, but every listing on a given street
is in the same city, so one lookup per street is enough forever.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/listings.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    url_key    TEXT PRIMARY KEY,
    city       TEXT,
    notified   INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'h2s'
);
CREATE TABLE IF NOT EXISTS streets (
    prefix     TEXT PRIMARY KEY,
    city       TEXT NOT NULL,
    learned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    source  TEXT PRIMARY KEY,
    ran_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS places (
    query       TEXT PRIMARY KEY,
    lat         REAL,
    lon         REAL,
    looked_up_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init():
    with _connect() as connection:
        connection.executescript(SCHEMA)
        # Databases seeded before Funda support lack the source column. Every
        # row already in them came from Holland2Stay, which is the default.
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(listings)")
        }
        if "source" not in columns:
            connection.execute(
                "ALTER TABLE listings ADD COLUMN source TEXT NOT NULL DEFAULT 'h2s'"
            )


def get_setting(key, default=None):
    """
    Read a runtime switch set from Telegram.

    Settings outrank config.json: the file says how the notifier starts,
    the buttons say how it is running right now.
    """
    with _connect() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value), _now()),
        )


def known_keys(source=None):
    """Every url_key we have recorded, optionally for one source only."""
    with _connect() as connection:
        if source is None:
            rows = connection.execute("SELECT url_key FROM listings").fetchall()
        else:
            rows = connection.execute(
                "SELECT url_key FROM listings WHERE source = ?", (source,)
            ).fetchall()
    return {row[0] for row in rows}


INSERT_SQL = (
    "INSERT OR IGNORE INTO listings (url_key, city, notified, first_seen, source) "
    "VALUES (?, ?, ?, ?, ?)"
)


def record(url_key, city=None, notified=False, source="h2s"):
    with _connect() as connection:
        connection.execute(
            INSERT_SQL, (url_key, city, 1 if notified else 0, _now(), source)
        )


def record_many(url_keys, city=None, notified=False, source="h2s"):
    stamp = _now()
    rows = [(key, city, 1 if notified else 0, stamp, source) for key in url_keys]
    with _connect() as connection:
        connection.executemany(INSERT_SQL, rows)


def counts_by_source():
    """How many listings we are tracking per source, for the status report."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT source, COUNT(*) FROM listings GROUP BY source"
        ).fetchall()
    return dict(rows)


def get_place(query):
    """
    Cached coordinates for a place name or postcode.

    Three distinct answers, which the caller has to tell apart: `None` means
    we have never looked, `(lat, lon)` means we know, and `(None, None)`
    means we looked and the geocoder had nothing - worth remembering so we
    do not ask again every cycle.
    """
    with _connect() as connection:
        row = connection.execute(
            "SELECT lat, lon FROM places WHERE query = ?", (query,)
        ).fetchone()
    return (row[0], row[1]) if row else None


def set_place(query, lat, lon):
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO places (query, lat, lon, looked_up_at)"
            " VALUES (?, ?, ?, ?)",
            (query, lat, lon, _now()),
        )


def street_city(prefix):
    with _connect() as connection:
        row = connection.execute(
            "SELECT city FROM streets WHERE prefix = ?", (prefix,)
        ).fetchone()
    return row[0] if row else None


def learn_street(prefix, city):
    if not city:
        return
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO streets (prefix, city, learned_at) VALUES (?, ?, ?)",
            (prefix, city, _now()),
        )


def minutes_since_run(source):
    """
    Minutes since a source last polled, or None if it never has.

    Sources that cost credits can be given a longer interval than the
    container's own loop, so a cheap source stays hourly while an expensive
    one runs every few hours.
    """
    with _connect() as connection:
        row = connection.execute(
            "SELECT ran_at FROM runs WHERE source = ?", (source,)
        ).fetchone()
    if not row:
        return None
    try:
        previous = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - previous).total_seconds() / 60


def mark_run(source):
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO runs (source, ran_at) VALUES (?, ?)",
            (source, _now()),
        )


def stats():
    with _connect() as connection:
        listings = connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        streets = connection.execute("SELECT COUNT(*) FROM streets").fetchone()[0]
    return {"listings": listings, "streets": streets}
