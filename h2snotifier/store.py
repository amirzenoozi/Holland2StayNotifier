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
