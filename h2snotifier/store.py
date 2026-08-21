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
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streets (
    prefix     TEXT PRIMARY KEY,
    city       TEXT NOT NULL,
    learned_at TEXT NOT NULL
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


def known_keys():
    """Every url_key we have ever recorded."""
    with _connect() as connection:
        rows = connection.execute("SELECT url_key FROM listings").fetchall()
    return {row[0] for row in rows}


def record(url_key, city=None, notified=False):
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO listings (url_key, city, notified, first_seen) "
            "VALUES (?, ?, ?, ?)",
            (url_key, city, 1 if notified else 0, _now()),
        )


def record_many(url_keys, city=None, notified=False):
    stamp = _now()
    rows = [(key, city, 1 if notified else 0, stamp) for key in url_keys]
    with _connect() as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO listings (url_key, city, notified, first_seen) "
            "VALUES (?, ?, ?, ?)",
            rows,
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


def stats():
    with _connect() as connection:
        listings = connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        streets = connection.execute("SELECT COUNT(*) FROM streets").fetchone()[0]
    return {"listings": listings, "streets": streets}
