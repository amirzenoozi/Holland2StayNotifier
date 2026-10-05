# Multi-group Subscriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let friends add the bot to their own Telegram group and receive rental alerts (in a topic or General) filtered by their own cities/radius, price range and sources, while the owner's existing group keeps working unchanged.

**Architecture:** One container, one bot token. Each group is a row in SQLite (`subscriptions` + children). A cycle splits into *collect* (fetch each source / each unique paid search once, diff against the global `listings` catalogue) and *dispatch* (for each new listing, `matcher.matches` every active subscription and send via a per-chat paced sender, recording `deliveries`). Control commands are routed per `chat_id`; onboarding uses owner-issued one-time invite codes.

**Tech Stack:** Python 3.12 (Docker) — local venv is Python 3.9, so no `X | None` runtime annotations, no `match`; stdlib `sqlite3`, `requests`, `pytest` (new, dev only).

**Spec:** `docs/superpowers/specs/2026-10-05-multi-group-subscriptions-design.md`

## Global Constraints

- Flat module layout in `h2snotifier/` (no package). New modules must be added to the `COPY` line in `h2snotifier/Dockerfile` (Task 10) — it lists files by name.
- All new code must run on Python 3.9 **and** 3.12: use `from __future__ import annotations` in files with dataclasses; no `X | Y` types evaluated at runtime.
- `listings.source` and `deliveries.source` use the **store** key (`h2s`, `funda`, ...); `runs`, `sub_sources`, `sub_locations.source`, `searches.source` use the **config** key (`holland2stay`, `funda`, ...). Convert with `registry.STORE_TO_CONFIG` / `registry.CONFIG_TO_STORE`.
- Telegram topic id is taken from a message **only when `message["is_topic_message"]` is true**; otherwise it is `None` (General / non-forum group).
- A group with no matching cities receives nothing.
- Max 5 cities per group (`MAX_CITIES_PER_GROUP`), global cap on distinct paid searches `MAX_DISTINCT_SEARCHES` (default 30), `/check` cooldown for non-owners 10 min (`CHECK_COOLDOWN_MINUTES`) and never bypasses `min_interval_minutes` for non-owners.
- A listing with no price always passes the price filter.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Tests make no network calls (stub Telegram, PDOK/geo and Firecrawl fetchers).
- Run tests from `h2snotifier/`: `venv/bin/python -m pytest -q`.

## Review Focus

1. **Group with no cities configured** gets nothing (not everything) — pinned in Task 3.
2. **Non-forum group where a plain reply carries `message_thread_id`** must yield `topic_id=None` — pinned in Task 9.
3. **Same invite code redeemed twice** (two groups, or a double-tap) — only one wins — pinned in Task 1.
4. **A group adds a new city after the source already has history** must not receive the historical listings — pinned in Task 6.
5. **Group upgraded to a supergroup** (chat id changes, e.g. when someone enables topics) must keep its subscription instead of failing silently — pinned in Tasks 1, 4, 9.
6. **Bot kicked / chat gone mid-fan-out** must stop sending to that group without blocking the others — pinned in Task 4.
7. **Price strings** like `1.250`, `1,250`, `1.250,00`, `1550.00` must parse to the same euro amount — pinned in Task 3.

## File Structure

| File | Responsibility |
|---|---|
| `h2snotifier/store.py` (modify) | New tables, public `connect`/`now`, deliveries helpers |
| `h2snotifier/registry.py` (modify) | `STORE_TO_CONFIG`, `CONFIG_TO_STORE` |
| `h2snotifier/subscriptions.py` (new) | Subscription CRUD, invites, locations, search-set derivation, caps |
| `h2snotifier/matcher.py` (new) | Pure `matches(listing, sub)` + `parse_price` |
| `h2snotifier/render.py` (new) | `city_tag`, `listing_to_msg`, `listing_button` (moved out of `main.py`) |
| `h2snotifier/dispatch.py` (new) | Pacing, per-group send, fan-out, retry, chat-gone handling |
| `h2snotifier/migration.py` (new) | `config.json` → subscription #1 |
| `h2snotifier/commands.py` (new) | Text-only handlers for `/city`, `/price`, `/filters`, help texts |
| `h2snotifier/control.py` (modify) | Per-group Controller, panels, setup/invite flow |
| `h2snotifier/telegram.py` (modify) | `get_chat_member`, `my_chat_member` updates, optional default chat |
| `h2snotifier/main.py` (modify) | Collect/dispatch runners, scheduler, startup |
| `h2snotifier/tests/` (new) | pytest suite |

---

### Task 1: Schema, test scaffolding, subscriptions core

**Files:**
- Create: `h2snotifier/pytest.ini`, `h2snotifier/requirements-dev.txt`, `h2snotifier/tests/conftest.py`, `h2snotifier/subscriptions.py`, `h2snotifier/tests/test_subscriptions.py`
- Modify: `h2snotifier/store.py` (SCHEMA + public helpers), `h2snotifier/registry.py`

**Interfaces:**
- Produces (`registry`): `STORE_TO_CONFIG: dict`, `CONFIG_TO_STORE: dict`.
- Produces (`store`): `connect()` (context manager yielding sqlite3 connection, commits on exit), `now() -> str`, `delivery_exists(chat_id, source, url_key) -> bool`, `record_delivery(chat_id, source, url_key, ok, payload=None)`, `pending_deliveries(max_attempts) -> list[(chat_id, source, url_key, attempts, payload)]`, `update_delivery(chat_id, source, url_key, ok, attempts)`.
- Produces (`subscriptions`): dataclasses `Location(city, radius_km=None, source="")`, `Subscription(chat_id, topic_id, title, price_min, price_max, timezone, paused, active, locations, sources)` with `.locations_for(config_key) -> list[Location]`; `LimitError`; `create(chat_id, topic_id, title, timezone=DEFAULT_TIMEZONE) -> Subscription`; `create_invite() -> str`; `activate(code, chat_id, topic_id, title) -> Subscription|None`; `get(chat_id)`, `get_active(chat_id)`, `all_active() -> list`, `has_any() -> bool`; `set_topic(chat_id, topic_id)`, `set_paused(chat_id, paused)`, `set_price(chat_id, low, high)`, `set_timezone(chat_id, name)`, `set_source(chat_id, source, enabled)`, `deactivate(chat_id)`, `move_chat(old_id, new_id)`.

- [ ] **Step 1: Test scaffolding**

`h2snotifier/requirements-dev.txt`:
```
pytest==8.3.3
```
`h2snotifier/pytest.ini`:
```
[pytest]
pythonpath = .
testpaths = tests
```
`h2snotifier/tests/conftest.py`:
```python
import pytest

import store
import subscriptions


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """Every test gets its own empty database."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init()


@pytest.fixture
def make_sub():
    """Create an active subscription. cities: [(name, radius_km|None)]."""

    def build(chat_id=-100, cities=(("Utrecht", None),), topic_id=None, **fields):
        subscriptions.create(chat_id, topic_id, "Test group")
        for name, radius in cities:
            subscriptions.add_location(chat_id, name, radius, enforce=False)
        if "price_min" in fields or "price_max" in fields:
            subscriptions.set_price(chat_id, fields.get("price_min"), fields.get("price_max"))
        if fields.get("paused"):
            subscriptions.set_paused(chat_id, True)
        return subscriptions.get(chat_id)

    return build
```
Run: `cd h2snotifier && python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt`

- [ ] **Step 2: Write the failing tests** — `h2snotifier/tests/test_subscriptions.py`:

```python
import registry
import store
import subscriptions


def test_create_has_all_sources_on_and_no_locations():
    sub = subscriptions.create(-100, 5, "Friends", "Europe/Amsterdam")
    assert (sub.chat_id, sub.topic_id, sub.title) == (-100, 5, "Friends")
    assert sub.active and not sub.paused
    assert sub.sources == set(registry.CONFIG_KEYS)
    assert sub.locations == []


def test_invite_is_single_use():
    code = subscriptions.create_invite()
    first = subscriptions.activate(code, -1, None, "A")
    second = subscriptions.activate(code, -2, None, "B")
    assert first is not None and first.chat_id == -1
    assert second is None
    assert subscriptions.get(-2) is None


def test_unknown_invite_is_rejected():
    assert subscriptions.activate("nope", -1, None, "A") is None
    assert subscriptions.get(-1) is None


def test_setters_round_trip():
    subscriptions.create(-1, None, "A")
    subscriptions.set_topic(-1, 77)
    subscriptions.set_paused(-1, True)
    subscriptions.set_price(-1, 800, 1800)
    subscriptions.set_timezone(-1, "Asia/Tehran")
    subscriptions.set_source(-1, "funda", False)
    sub = subscriptions.get(-1)
    assert (sub.topic_id, sub.paused, sub.price_min, sub.price_max, sub.timezone) == (
        77, True, 800, 1800, "Asia/Tehran")
    assert "funda" not in sub.sources and "pararius" in sub.sources


def test_deactivate_hides_from_active_lists():
    subscriptions.create(-1, None, "A")
    subscriptions.deactivate(-1)
    assert subscriptions.get_active(-1) is None
    assert subscriptions.all_active() == []
    assert subscriptions.get(-1) is not None
    assert subscriptions.has_any()


def test_delivery_helpers():
    assert not store.delivery_exists(-1, "funda", "funda-1")
    store.record_delivery(-1, "funda", "funda-1", ok=False, payload='{"x": 1}')
    assert store.delivery_exists(-1, "funda", "funda-1")
    assert store.pending_deliveries(3) == [(-1, "funda", "funda-1", 1, '{"x": 1}')]
    store.update_delivery(-1, "funda", "funda-1", ok=True, attempts=2)
    assert store.pending_deliveries(3) == []
```
(`move_chat` is tested in Task 2, once `add_location` exists.)

- [ ] **Step 3: Run to verify failure**

Run: `venv/bin/python -m pytest tests/test_subscriptions.py -q`
Expected: FAIL (`ModuleNotFoundError: subscriptions`).

- [ ] **Step 4: Implement `registry.py` additions** — append to `h2snotifier/registry.py`:

```python
STORE_TO_CONFIG = {source["store"]: source["config"] for source in SOURCES}
CONFIG_TO_STORE = {source["config"]: source["store"] for source in SOURCES}
```

- [ ] **Step 5: Implement `store.py` changes**

Append these tables inside the `SCHEMA` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id    INTEGER PRIMARY KEY,
    topic_id   INTEGER,
    title      TEXT NOT NULL DEFAULT '',
    price_min  INTEGER,
    price_max  INTEGER,
    timezone   TEXT NOT NULL DEFAULT 'Europe/Amsterdam',
    paused     INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sub_locations (
    chat_id   INTEGER NOT NULL,
    city      TEXT NOT NULL COLLATE NOCASE,
    radius_km REAL,
    source    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chat_id, city, source)
);
CREATE TABLE IF NOT EXISTS sub_sources (
    chat_id INTEGER NOT NULL,
    source  TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, source)
);
CREATE TABLE IF NOT EXISTS invites (
    code         TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    used_by_chat INTEGER,
    used_at      TEXT
);
CREATE TABLE IF NOT EXISTS searches (
    source    TEXT NOT NULL,
    city      TEXT NOT NULL COLLATE NOCASE,
    radius_km REAL NOT NULL DEFAULT 0,
    seeded    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, city, radius_km)
);
CREATE TABLE IF NOT EXISTS deliveries (
    chat_id  INTEGER NOT NULL,
    source   TEXT NOT NULL,
    url_key  TEXT NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    payload  TEXT,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (chat_id, source, url_key)
);
```

Directly after `_connect` is defined add:

```python
# Public names for the other modules; the underscore versions stay for the
# helpers below that predate them.
connect = _connect
now = _now
```

Append at the end of `store.py`:

```python
def delivery_exists(chat_id, source, url_key):
    with _connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM deliveries WHERE chat_id = ? AND source = ? AND url_key = ?",
            (chat_id, source, url_key),
        ).fetchone()
    return row is not None


def record_delivery(chat_id, source, url_key, ok, payload=None):
    """Remember that a listing went (or failed to go) to one group."""
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO deliveries"
            " (chat_id, source, url_key, ok, attempts, payload, sent_at)"
            " VALUES (?, ?, ?, ?, 1, ?, ?)",
            (chat_id, source, url_key, 1 if ok else 0, None if ok else payload, _now()),
        )


def pending_deliveries(max_attempts):
    """Failed sends still worth retrying, with the listing to resend."""
    with _connect() as connection:
        return connection.execute(
            "SELECT chat_id, source, url_key, attempts, payload FROM deliveries"
            " WHERE ok = 0 AND attempts < ? AND payload IS NOT NULL",
            (max_attempts,),
        ).fetchall()


def update_delivery(chat_id, source, url_key, ok, attempts):
    with _connect() as connection:
        connection.execute(
            "UPDATE deliveries SET ok = ?, attempts = ?, sent_at = ?,"
            " payload = CASE WHEN ? THEN NULL ELSE payload END"
            " WHERE chat_id = ? AND source = ? AND url_key = ?",
            (1 if ok else 0, attempts, _now(), 1 if ok else 0, chat_id, source, url_key),
        )
```

- [ ] **Step 6: Implement `subscriptions.py` (core part)**

```python
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
```

- [ ] **Step 7: Run tests**

Run: `venv/bin/python -m pytest tests/test_subscriptions.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add h2snotifier docs
git commit -m "Add subscription schema and core subscriptions module" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Locations, limits and the shared search set

**Files:**
- Modify: `h2snotifier/subscriptions.py`
- Test: `h2snotifier/tests/test_locations.py`

**Interfaces:**
- Consumes: Task 1 (`Subscription`, `Location`, `get_active`, `all_active`, `LimitError`, `PAID_SOURCES`).
- Produces: `add_location(chat_id, city, radius_km=None, source="", enforce=True)`; `remove_location(chat_id, city) -> bool`; `radius_bucket(source, km) -> float` (0.0 = exact city); `Search(city, radius_km, seeded)` with `.fetch_args() -> dict` (`name`, `area`, optional `radius`); `rebuild_searches() -> set[(source, city_lower, radius)]`; `searches_for(config_key) -> list[Search]`; `mark_seeded(config_key, search)`; `mark_source_seeded(config_key)`; `wants(config_key, subs) -> bool`; `cities_for(config_key, subs) -> set[str]` (lowercase).

- [ ] **Step 1: Write the failing tests** — `h2snotifier/tests/test_locations.py`:

```python
import pytest

import subscriptions
from subscriptions import LimitError, Search


def test_radius_bucket_rounds_up_to_supported_step():
    assert subscriptions.radius_bucket("funda", 7) == 10.0
    assert subscriptions.radius_bucket("funda", 10) == 10.0
    assert subscriptions.radius_bucket("funda", 12) == 15.0
    assert subscriptions.radius_bucket("funda", 80) == 50.0   # beyond the top step
    assert subscriptions.radius_bucket("funda", None) == 0.0
    assert subscriptions.radius_bucket("somesource", 7) == 7.0


def test_add_and_remove_location_is_case_insensitive(make_sub):
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Utrecht", 10)
    subscriptions.add_location(-1, "utrecht", 20)           # replaces, not duplicates
    assert [(l.city, l.radius_km) for l in subscriptions.get(-1).locations] == [("utrecht", 20.0)]
    assert subscriptions.remove_location(-1, "UTRECHT") is True
    assert subscriptions.remove_location(-1, "Utrecht") is False


def test_city_limit(monkeypatch, make_sub):
    monkeypatch.setattr(subscriptions, "MAX_CITIES", 2)
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Utrecht")
    subscriptions.add_location(-1, "Zeist")
    with pytest.raises(LimitError):
        subscriptions.add_location(-1, "Hilversum")
    subscriptions.add_location(-1, "Zeist", 5)               # editing an existing one is fine


def test_search_cap_counts_distinct_paid_searches(monkeypatch, make_sub):
    monkeypatch.setattr(subscriptions, "MAX_SEARCHES", 3)    # 3 paid sources = one city
    make_sub(-1, cities=())
    make_sub(-2, cities=())
    subscriptions.add_location(-1, "Utrecht", 10)            # 3 searches
    with pytest.raises(LimitError):
        subscriptions.add_location(-1, "Zeist", 10)
    subscriptions.add_location(-2, "Utrecht", 10)            # shared: adds nothing new


def test_rebuild_searches_and_seeding(make_sub):
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Utrecht", 7)
    keys = subscriptions.rebuild_searches()
    assert ("funda", "utrecht", 10.0) in keys and ("pararius", "utrecht", 10.0) in keys
    found = subscriptions.searches_for("funda")
    assert found == [Search("utrecht", 10.0, False)]
    subscriptions.mark_seeded("funda", found[0])
    assert subscriptions.searches_for("funda")[0].seeded is True
    subscriptions.rebuild_searches()                          # unchanged set keeps the flag
    assert subscriptions.searches_for("funda")[0].seeded is True
    subscriptions.remove_location(-1, "Utrecht")
    subscriptions.rebuild_searches()
    assert subscriptions.searches_for("funda") == []


def test_search_fetch_args():
    assert Search("capelle aan den ijssel", 10.0, False).fetch_args() == {
        "name": "capelle aan den ijssel 10km", "area": "capelle-aan-den-ijssel", "radius": "10km"}
    assert Search("zeist", 0.0, False).fetch_args() == {"name": "zeist", "area": "zeist"}


def test_source_specific_locations_do_not_create_paid_searches(make_sub):
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Nijkerk", 40, source="ikwilhuren", enforce=False)
    assert subscriptions.rebuild_searches() == set()
    sub = subscriptions.get(-1)
    assert sub.locations_for("funda") == []
    assert [l.city for l in sub.locations_for("ikwilhuren")] == ["Nijkerk"]


def test_wants_and_cities_for(make_sub):
    make_sub(-1, cities=(("Utrecht", None),))
    make_sub(-2, cities=(("Zeist", None),))
    subscriptions.set_source(-2, "holland2stay", False)
    subs = subscriptions.all_active()
    assert subscriptions.wants("holland2stay", subs)
    assert subscriptions.cities_for("holland2stay", subs) == {"utrecht"}
    subscriptions.set_source(-1, "holland2stay", False)
    assert not subscriptions.wants("holland2stay", subscriptions.all_active())
```
Also add this test to the same file:

```python
def test_move_chat_keeps_settings_and_reactivates(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),), price_min=500)
    subscriptions.deactivate(-1)
    subscriptions.move_chat(-1, -1001)
    assert subscriptions.get(-1) is None
    moved = subscriptions.get_active(-1001)
    assert moved.price_min == 500
    assert [l.city for l in moved.locations] == ["Utrecht"]
```

- [ ] **Step 2: Run to verify failure** — `venv/bin/python -m pytest tests/test_locations.py -q` → FAIL (`add_location` missing).

- [ ] **Step 3: Implement** — append to `subscriptions.py`:

```python
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
```

- [ ] **Step 4: Run all tests** — `venv/bin/python -m pytest -q` → PASS.

- [ ] **Step 5: Commit** — `git add h2snotifier && git commit -m "Add per-group locations, limits and shared search set" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 3: Matcher

**Files:**
- Create: `h2snotifier/matcher.py`
- Test: `h2snotifier/tests/test_matcher.py`

**Interfaces:**
- Consumes: `subscriptions.Subscription/Location`, `registry.STORE_TO_CONFIG`, `geo.locate_town/locate_listing/distance_km`.
- Produces: `parse_price(value) -> int|None`, `matches(listing: dict, sub: Subscription) -> bool`.

- [ ] **Step 1: Write the failing tests** — `h2snotifier/tests/test_matcher.py`:

```python
from types import SimpleNamespace

import pytest

import geo
import matcher
from subscriptions import Location, Subscription

POINTS = {"utrecht": (52.0907, 5.1214), "amersfoort": (52.1561, 5.3878)}  # ~19 km apart


@pytest.fixture(autouse=True)
def fake_geo(monkeypatch):
    monkeypatch.setattr(matcher, "geo", SimpleNamespace(
        locate_town=lambda name: POINTS.get((name or "").lower()),
        locate_listing=lambda postcode, city=None: POINTS.get((city or "").lower()),
        distance_km=geo.distance_km,
    ))


def sub(**fields):
    base = dict(chat_id=1, topic_id=None, title="", price_min=None, price_max=None,
                timezone="UTC", paused=False, active=True,
                locations=[Location("Utrecht")], sources={"funda", "ikwilhuren", "holland2stay"})
    base.update(fields)
    return Subscription(**base)


def listing(**fields):
    base = {"source": "funda", "city": "Utrecht", "postcode": "3511 AB", "price_excl": "1.250"}
    base.update(fields)
    return base


@pytest.mark.parametrize("raw,expected", [
    ("1.250", 1250), ("1,250", 1250), ("1.250,00", 1250), ("1550.00", 1550),
    ("€ 950", 950), (None, None), ("", None), ("on request", None),
])
def test_parse_price(raw, expected):
    assert matcher.parse_price(raw) == expected


def test_exact_city_matches_case_insensitively():
    assert matcher.matches(listing(city="UTRECHT"), sub())
    assert not matcher.matches(listing(city="Zeist"), sub())


def test_group_without_cities_gets_nothing():
    assert not matcher.matches(listing(), sub(locations=[]))


def test_paused_inactive_or_disabled_source_never_matches():
    assert not matcher.matches(listing(), sub(paused=True))
    assert not matcher.matches(listing(), sub(active=False))
    assert not matcher.matches(listing(), sub(sources={"ikwilhuren"}))


def test_price_bounds():
    low, high = sub(price_min=1000, price_max=1500), sub(price_max=1000)
    assert matcher.matches(listing(price_excl="1.250"), low)
    assert not matcher.matches(listing(price_excl="900"), low)
    assert not matcher.matches(listing(price_excl="1.250"), high)


def test_listing_without_price_passes_price_filter():
    assert matcher.matches(listing(price_excl=None, price_on_request=True), sub(price_min=500, price_max=900))


def test_radius_uses_distance_not_name():
    near = sub(locations=[Location("Utrecht", 25)])
    far = sub(locations=[Location("Utrecht", 10)])
    other = listing(city="Amersfoort")
    assert matcher.matches(other, near)
    assert not matcher.matches(other, far)


def test_holland2stay_matches_by_name_only():
    radius = sub(locations=[Location("Utrecht", 25)])
    assert not matcher.matches(listing(source="h2s", city="Amersfoort"), radius)
    assert matcher.matches(listing(source="h2s", city="Utrecht"), radius)


def test_source_specific_location_only_applies_to_its_source():
    s = sub(locations=[Location("Utrecht", None, "ikwilhuren")])
    assert matcher.matches(listing(source="ikwilhuren"), s)
    assert not matcher.matches(listing(source="funda"), s)


def test_unlocatable_listing_does_not_match_radius():
    s = sub(locations=[Location("Utrecht", 25)])
    assert not matcher.matches(listing(city="Nowhere"), s)
```

- [ ] **Step 2: Run to verify failure** → FAIL (`ModuleNotFoundError: matcher`).

- [ ] **Step 3: Implement** — `h2snotifier/matcher.py`:

```python
"""
Does one listing belong in one group's chat?

Pure apart from the geocoder cache: no Telegram, no fetching. Everything a
group can configure is decided here and nowhere else.
"""

import re

import geo
import registry


def parse_price(value):
    """
    Whole euros from whatever the sites print.

    Handles "1.250", "1,250", "1.250,00", "1550.00" and "€ 950". A separator
    followed by one or two digits at the end is cents (a thousands group always
    has three); every other separator is a thousands mark.
    """
    if value is None:
        return None
    text = re.sub(r"[^\d.,]", "", str(value))
    text = re.sub(r"[.,]\d{1,2}$", "", text)
    digits = re.sub(r"[.,]", "", text)
    return int(digits) if digits else None


def _price_ok(listing, sub):
    price = parse_price(listing.get("price_excl"))
    if price is None:
        price = parse_price(listing.get("price_incl"))
    if price is None:
        return True
    if sub.price_min is not None and price < sub.price_min:
        return False
    if sub.price_max is not None and price > sub.price_max:
        return False
    return True


def _within(listing, location):
    city = (listing.get("city") or "").strip().lower()
    if city and city == location.city.strip().lower():
        return True
    # Holland2Stay only tells us a city name, so a radius means nothing there.
    if not location.radius_km or listing.get("source") == "h2s":
        return False
    anchor = geo.locate_town(location.city)
    point = geo.locate_listing(listing.get("postcode"), listing.get("city"))
    if not anchor or not point:
        return False
    return geo.distance_km(point, anchor) <= location.radius_km


def matches(listing, sub):
    config_key = registry.STORE_TO_CONFIG.get(listing.get("source"))
    if not sub.active or sub.paused or config_key not in sub.sources:
        return False
    if not _price_ok(listing, sub):
        return False
    return any(_within(listing, loc) for loc in sub.locations_for(config_key))
```

- [ ] **Step 4: Run** — `venv/bin/python -m pytest tests/test_matcher.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add h2snotifier && git commit -m "Add per-group listing matcher" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 4: Rendering move and dispatch

**Files:**
- Create: `h2snotifier/render.py`, `h2snotifier/dispatch.py`, `h2snotifier/tests/test_dispatch.py`
- Modify: `h2snotifier/main.py` (remove the moved functions, import from `render`), `h2snotifier/telegram.py` (optional default chat)

**Interfaces:**
- Consumes: `matcher.matches`, `store.delivery_exists/record_delivery/pending_deliveries/update_delivery`, `subscriptions.deactivate/set_topic/move_chat`, a bot with `send_simple_msg(msg, button=None, chat_id=None, message_thread_id=None)` returning a `requests.Response`-like (`ok`, `status_code`, `text`, `json()`).
- Produces: `render.city_tag`, `render.listing_to_msg`, `render.listing_button`, `render.SOURCE_LABEL`; `dispatch.Pacer(seconds=3.0, clock=time.monotonic, sleep=time.sleep)`, `dispatch.fan_out(listing, subs, bot, pacer=None) -> int` (messages sent), `dispatch.retry_pending(subs, bot, pacer=None)`.

- [ ] **Step 1: Move the rendering code mechanically** (from `h2snotifier/`):

```bash
{ printf '"""Render a listing as a Telegram message and its link button."""\n\nimport re\nimport unicodedata\n\nimport registry\n\nSOURCE_LABEL = registry.LABELS\n\n\n'
  sed -n '/^def city_tag/,/^def _seed/p' main.py | sed '$d'; } > render.py
sed -i.bak '/^def city_tag/,/^def _seed/{/^def _seed/!d;}' main.py && rm main.py.bak
```
Then in `main.py`: keep `SOURCE_LABEL = registry.LABELS` (still used by `_seed`) and add `import render` after `import registry` (dispatch calls render; main no longer needs its functions). Verify: `venv/bin/python -c "import render; print(render.city_tag('Capelle aan den IJssel'))"` → `#CapelleAanDenIJssel`; `venv/bin/python -c "import main"` succeeds.

- [ ] **Step 2: Make `TelegramBot` chat-optional** — in `telegram.py` change the constructor to `def __init__(self, apikey, chat_id=None, message_thread_id=None):` (body unchanged; every send already takes an explicit `chat_id`).

- [ ] **Step 3: Write the failing tests** — `h2snotifier/tests/test_dispatch.py`:

```python
import json
from types import SimpleNamespace

import dispatch
import store
import subscriptions

NOW = dispatch.Pacer(0)


def reply(ok=True, status=200, text="", body=None):
    return SimpleNamespace(ok=ok, status_code=status, text=text, json=lambda: body or {})


class FakeBot:
    def __init__(self, scripted=None):
        self.sent = []
        self.scripted = scripted or {}      # chat_id -> list of replies, popped in order

    def send_simple_msg(self, msg, button=None, chat_id=None, message_thread_id=None, **kw):
        self.sent.append({"chat_id": chat_id, "thread": message_thread_id, "msg": msg})
        queue = self.scripted.get(chat_id)
        return queue.pop(0) if queue else reply()


def house(key="funda-1", city="Utrecht", **fields):
    base = {"source": "funda", "url_key": key, "url": f"https://x/{key}", "city": city,
            "postcode": "3511 AB", "address": "Street 1", "price_excl": "1.250"}
    base.update(fields)
    return base


def test_fan_out_reaches_only_matching_groups_in_their_topic(make_sub):
    make_sub(-1, cities=(("Utrecht", None),), topic_id=9)
    make_sub(-2, cities=(("Zeist", None),))
    make_sub(-3, cities=(("Utrecht", None),), price_max=1000)
    bot = FakeBot()
    sent = dispatch.fan_out(house(), subscriptions.all_active(), bot, NOW)
    assert sent == 1
    assert [(m["chat_id"], m["thread"]) for m in bot.sent] == [(-1, 9)]


def test_same_listing_is_not_sent_twice_to_a_group(make_sub):
    make_sub(-1)
    bot = FakeBot()
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert dispatch.fan_out(house(), subs, bot, NOW) == 0


def test_failed_send_is_retried_later(make_sub):
    make_sub(-1)
    bot = FakeBot({-1: [reply(ok=False, status=500, text="boom")]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 0
    assert len(store.pending_deliveries(3)) == 1
    dispatch.retry_pending(subs, bot, NOW)
    assert store.pending_deliveries(3) == []
    assert len(bot.sent) == 2


def test_gives_up_after_max_attempts(make_sub):
    make_sub(-1)
    bot = FakeBot({-1: [reply(ok=False, status=500, text="boom")] * 10})
    subs = subscriptions.all_active()
    dispatch.fan_out(house(), subs, bot, NOW)
    for _ in range(5):
        dispatch.retry_pending(subs, bot, NOW)
    assert len(bot.sent) == dispatch.MAX_ATTEMPTS


def test_kicked_bot_deactivates_group_and_others_still_get_it(make_sub):
    make_sub(-1)
    make_sub(-2)
    bot = FakeBot({-1: [reply(ok=False, status=403, text="Forbidden: bot was kicked from the group chat")]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert subscriptions.get_active(-1) is None
    assert subscriptions.get_active(-2) is not None
    dispatch.fan_out(house("funda-2"), subs, bot, NOW)         # -1 is skipped from now on
    assert [m["chat_id"] for m in bot.sent].count(-1) == 1


def test_deleted_topic_falls_back_to_general_and_says_so(make_sub):
    make_sub(-1, topic_id=9)
    bot = FakeBot({-1: [reply(ok=False, status=400, text="Bad Request: message thread not found")]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert subscriptions.get(-1).topic_id is None
    assert [m["thread"] for m in bot.sent] == [9, None, None]   # try, notice, resend


def test_supergroup_upgrade_moves_the_subscription(make_sub):
    make_sub(-1)
    body = {"parameters": {"migrate_to_chat_id": -1001}}
    bot = FakeBot({-1: [reply(ok=False, status=400, text="group chat was upgraded to a supergroup chat", body=body)]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert subscriptions.get(-1) is None and subscriptions.get_active(-1001) is not None
    assert bot.sent[-1]["chat_id"] == -1001


def test_paused_group_is_skipped(make_sub):
    make_sub(-1, paused=True)
    assert dispatch.fan_out(house(), subscriptions.all_active(), FakeBot(), NOW) == 0
```

- [ ] **Step 4: Run to verify failure** → FAIL (`ModuleNotFoundError: dispatch`).

- [ ] **Step 5: Implement** — `h2snotifier/dispatch.py`:

```python
"""
Send a listing to every group it matches.

One place owns the messy part of talking to many chats: pacing per chat (a
group allows about 20 messages a minute), noticing that a group is gone or has
moved, and remembering failed sends so a later cycle can retry them.
"""

import json
import logging
import time

import render
import store
import subscriptions
from matcher import matches

log = logging.getLogger("dispatch")

MAX_ATTEMPTS = 3
PACE_SECONDS = 3.0

# Telegram's wording when the bot can no longer post in a chat.
GONE = (
    "bot was kicked", "bot was blocked", "chat not found", "bot is not a member",
    "group chat was deleted", "not enough rights to send",
)
THREAD_GONE = ("message thread not found", "topic_deleted", "topic deleted")


class Pacer:
    """Spaces messages to the same chat; different chats never wait on each other."""

    def __init__(self, seconds=PACE_SECONDS, clock=time.monotonic, sleep=time.sleep):
        self.seconds, self.clock, self.sleep = seconds, clock, sleep
        self.last = {}

    def wait(self, chat_id):
        gap = self.seconds - (self.clock() - self.last.get(chat_id, float("-inf")))
        if gap > 0:
            self.sleep(gap)
        self.last[chat_id] = self.clock()


PACER = Pacer()


def _migrated_chat(response):
    try:
        return response.json().get("parameters", {}).get("migrate_to_chat_id")
    except Exception:
        return None


def _send(bot, sub, listing, pacer):
    """One send to one group. True when delivered."""
    pacer.wait(sub.chat_id)
    response = bot.send_simple_msg(
        render.listing_to_msg(listing),
        button=render.listing_button(listing),
        chat_id=sub.chat_id,
        message_thread_id=sub.topic_id,
    )
    if getattr(response, "ok", False):
        return True

    body = (getattr(response, "text", "") or "").lower()
    status = getattr(response, "status_code", None)

    new_id = _migrated_chat(response)
    if new_id:
        log.warning("group %s became supergroup %s", sub.chat_id, new_id)
        subscriptions.move_chat(sub.chat_id, new_id)
        sub.chat_id = new_id
        return _send(bot, sub, listing, pacer)

    if sub.topic_id and any(phrase in body for phrase in THREAD_GONE):
        log.warning("group %s: topic %s is gone, falling back to General", sub.chat_id, sub.topic_id)
        subscriptions.set_topic(sub.chat_id, None)
        sub.topic_id = None
        bot.send_simple_msg(
            "⚠️ The topic alerts were going to no longer exists, so I moved them to the "
            "main chat. Send /settopic inside a topic to pick another one.",
            chat_id=sub.chat_id,
        )
        return _send(bot, sub, listing, pacer)

    if status == 403 or any(phrase in body for phrase in GONE):
        log.warning("group %s is unreachable, deactivating: %s", sub.chat_id, body[:120])
        subscriptions.deactivate(sub.chat_id)
        sub.active = False
    return False


def fan_out(listing, subs, bot, pacer=None):
    """Send `listing` to every matching group not already sent it. Returns how many."""
    pacer = pacer or PACER
    sent = 0
    for sub in subs:
        if not matches(listing, sub):
            continue
        if store.delivery_exists(sub.chat_id, listing["source"], listing["url_key"]):
            continue
        ok = _send(bot, sub, listing, pacer)
        store.record_delivery(
            sub.chat_id, listing["source"], listing["url_key"], ok,
            None if ok else json.dumps(listing),
        )
        sent += bool(ok)
    return sent


def retry_pending(subs, bot, pacer=None):
    """Give failed sends another go; abandon those whose group is gone."""
    pacer = pacer or PACER
    by_chat = {sub.chat_id: sub for sub in subs}
    for chat_id, source, url_key, attempts, payload in store.pending_deliveries(MAX_ATTEMPTS):
        sub = by_chat.get(chat_id)
        if sub is None or not sub.active:
            store.update_delivery(chat_id, source, url_key, False, MAX_ATTEMPTS)
            continue
        if sub.paused:
            continue
        ok = _send(bot, sub, json.loads(payload), pacer)
        store.update_delivery(chat_id, source, url_key, ok, attempts + 1)
```

- [ ] **Step 6: Run** — `venv/bin/python -m pytest -q` → PASS. (`test_gives_up_after_max_attempts`: first send + 2 retries = 3 = `MAX_ATTEMPTS`.)
- [ ] **Step 7: Commit** — `git add h2snotifier && git commit -m "Add per-group dispatch; move rendering to render.py" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 5: Migration from config.json

**Files:**
- Create: `h2snotifier/migration.py`, `h2snotifier/tests/test_migration.py`

**Interfaces:**
- Consumes: `subscriptions.*`, `store.get_setting/known_keys`, `registry`.
- Produces: `migration.migrate_from_config(config) -> bool` (True when it created subscription #1).

Behaviour notes (also put in the module docstring): one `Location` per old per-source entry (source-specific, so Funda still fetches only its two searches); price = widest range across enabled paid-source searches (min `0` → none); legacy `settings` (`paused`, `source:*`, `timezone`) are carried over once; searches for sources that already have history are marked seeded.

- [ ] **Step 1: Write the failing test** — `h2snotifier/tests/test_migration.py`:

```python
import migration
import store
import subscriptions

CONFIG = {
    "telegram": {"chat_id": -1001234567890, "topic_id": 184, "admin_ids": [1], "timezone": "Europe/Amsterdam"},
    "holland2stay": {"enabled": True, "cities": ["Utrecht", "Zeist"]},
    "funda": {"enabled": True, "searches": [
        {"name": "A", "area": "amersfoort", "radius": "10km", "price": "1000-2000"},
        {"name": "H", "area": "hilversum", "radius": "5km", "price": "1000-2000"}]},
    "huurwoningen": {"enabled": False, "searches": []},
    "pararius": {"enabled": False, "searches": [{"area": "zwolle", "radius": "20km", "price": "0-2000"}]},
    "ikwilhuren": {"enabled": True, "areas": [{"place": "Nijkerk", "radius_km": 40}], "cities": ["Nijmegen"]},
}


def test_migrates_the_owner_group_once():
    assert migration.migrate_from_config(CONFIG) is True
    sub = subscriptions.get_active(-1001234567890)
    assert sub.topic_id == 184 and sub.timezone == "Europe/Amsterdam"
    assert sub.sources == {"holland2stay", "funda", "ikwilhuren"}          # disabled sources off
    assert (sub.price_min, sub.price_max) == (1000, 2000)                  # enabled funda only
    keys = {(l.city, l.radius_km, l.source) for l in sub.locations}
    assert ("Utrecht", None, "holland2stay") in keys
    assert ("Amersfoort", 10.0, "funda") in keys
    assert ("Nijkerk", 40.0, "ikwilhuren") in keys
    assert ("Nijmegen", None, "ikwilhuren") in keys
    assert migration.migrate_from_config(CONFIG) is False                  # idempotent


def test_funda_still_fetches_only_its_own_two_searches():
    migration.migrate_from_config(CONFIG)
    assert sorted(s.city for s in subscriptions.searches_for("funda")) == ["amersfoort", "hilversum"]
    assert subscriptions.searches_for("ikwilhuren") == []


def test_carries_over_legacy_settings():
    store.set_setting("paused", "1")
    store.set_setting("source:funda", "0")
    store.set_setting("timezone", "Asia/Tehran")
    migration.migrate_from_config(CONFIG)
    sub = subscriptions.get(-1001234567890)
    assert sub.paused and "funda" not in sub.sources and sub.timezone == "Asia/Tehran"


def test_existing_history_means_searches_start_seeded():
    store.record("funda-1", city="Amersfoort", notified=True, source="funda")
    migration.migrate_from_config(CONFIG)
    assert all(s.seeded for s in subscriptions.searches_for("funda"))


def test_no_chat_id_means_nothing_to_migrate():
    assert migration.migrate_from_config({"telegram": {}}) is False
```

- [ ] **Step 2: Run to verify failure** → FAIL (`ModuleNotFoundError: migration`).

- [ ] **Step 3: Implement** — `h2snotifier/migration.py`:

```python
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
    subscriptions.create(int(chat_id), telegram.get("topic_id"), "Owner group", timezone)
    chat_id = int(chat_id)

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
```

- [ ] **Step 4: Run** — `venv/bin/python -m pytest tests/test_migration.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add h2snotifier && git commit -m "Migrate legacy config.json into the first subscription" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 6: Collect/dispatch refactor of the runners and scheduler

**Files:**
- Modify: `h2snotifier/main.py` (imports, `_notify` removed, `run_h2s`, `run_search_source`, `run_ikwilhuren`, `run_cycle`, `Scheduler`, `main`)
- Test: `h2snotifier/tests/test_runners.py`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: `run_h2s(config, subs, bot, debug)`, `run_search_source(module, key, config, subs, bot, debug)`, `run_ikwilhuren(config, subs, bot, debug)` — each returns the same result dicts as today; `run_cycle(config, bot, debug, force=False)`; `Scheduler(config, bot, debug, interval, replies=None)` with `request_cycle(reply_to=None, force=False) -> bool` and `cycle(force=False)`.

- [ ] **Step 1: Write the failing tests** — `h2snotifier/tests/test_runners.py`:

```python
from types import SimpleNamespace

import main
import store
import subscriptions
from tests.test_dispatch import FakeBot, house   # reuse the fake bot and listing builder
import dispatch


def fake_module(per_city):
    """A search source: per_city maps area slug -> {id: listing}."""
    return SimpleNamespace(NAME="funda", fetch_search=lambda search: dict(per_city[search["area"]]))


def run(module, subs, bot):
    dispatch.PACER = dispatch.Pacer(0)
    return main.run_search_source(module, "funda", {}, subs, bot, None)


def test_first_fetch_of_a_search_is_silent_then_new_listings_go_out(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),))
    subscriptions.rebuild_searches()
    results = {"utrecht": {"1": house("funda-1")}}
    bot = FakeBot()
    run(fake_module(results), subscriptions.all_active(), bot)
    assert bot.sent == []                                   # seeded silently
    results["utrecht"]["2"] = house("funda-2")
    run(fake_module(results), subscriptions.all_active(), bot)
    assert [m["chat_id"] for m in bot.sent] == [-1]


def test_new_city_on_an_existing_source_does_not_flood(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),))
    subscriptions.rebuild_searches()
    results = {"utrecht": {"1": house("funda-1")}, "zeist": {
        "7": house("funda-7", city="Zeist"), "8": house("funda-8", city="Zeist")}}
    bot = FakeBot()
    run(fake_module(results), subscriptions.all_active(), bot)    # seeds utrecht
    subscriptions.add_location(-1, "Zeist", 10)
    subscriptions.rebuild_searches()
    run(fake_module(results), subscriptions.all_active(), bot)    # zeist is unseeded
    assert bot.sent == []
    results["zeist"]["9"] = house("funda-9", city="Zeist")
    run(fake_module(results), subscriptions.all_active(), bot)
    assert len(bot.sent) == 1


def test_each_unique_search_is_fetched_once_for_many_groups(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),))
    make_sub(-2, cities=(("Utrecht", 10),))
    subscriptions.rebuild_searches()
    calls = []
    module = SimpleNamespace(NAME="funda", fetch_search=lambda s: calls.append(s["area"]) or {})
    run(module, subscriptions.all_active(), FakeBot())
    assert calls == ["utrecht"]


def test_ikwilhuren_fans_out_by_each_groups_filters(make_sub, monkeypatch):
    make_sub(-1, cities=(("Utrecht", None),))
    make_sub(-2, cities=(("Utrecht", None),), price_max=1000)
    store.record("old", notified=True, source="ikwilhuren")          # not a first run
    fresh = house("ikw-1", source="ikwilhuren")
    monkeypatch.setattr(main.ikwilhuren, "fetch_catalogue", lambda: {"old": {}, "ikw-1": fresh})
    bot = FakeBot()
    dispatch.PACER = dispatch.Pacer(0)
    result = main.run_ikwilhuren({}, subscriptions.all_active(), bot, None)
    assert [m["chat_id"] for m in bot.sent] == [-1]
    assert result["new"] == 1 and result["sent"] == 1


def test_run_cycle_skips_sources_nobody_wants(make_sub, monkeypatch):
    make_sub(-1, cities=(("Utrecht", None),))
    subscriptions.set_source(-1, "funda", False)
    config = {"funda": {"enabled": True}}
    results = main.run_cycle(config, FakeBot(), None)
    assert results["funda"] == {"skipped": "off"}


def test_h2s_looks_up_only_cities_some_group_watches(make_sub, monkeypatch):
    make_sub(-1, cities=(("Utrecht", None),))
    store.record("old-1", city="Utrecht", notified=True, source="h2s")
    monkeypatch.setattr(main.h2s, "fetch_listing_keys", lambda: {"old-1", "utrecht-a-1", "zeist-b-2"})
    monkeypatch.setattr(main.h2s, "street_prefix", lambda key: key.rsplit("-", 1)[0])
    fetched = []

    def fetch_listing(key):
        fetched.append(key)
        return house(key, source="h2s", city="Utrecht")

    monkeypatch.setattr(main.h2s, "fetch_listing", fetch_listing)
    store.learn_street("zeist-b", "Zeist")                            # known street, unwatched city
    bot = FakeBot()
    dispatch.PACER = dispatch.Pacer(0)
    main.run_h2s({}, subscriptions.all_active(), bot, None)
    assert fetched == ["utrecht-a-1"] and len(bot.sent) == 1
```
(`tests/` needs an empty `tests/__init__.py` for the `from tests.test_dispatch import` line — create it, and give `FakeBot`/`house` a shared home by moving them into `tests/helpers.py` if you prefer; keep the import consistent with whichever you choose.)

- [ ] **Step 2: Run to verify failure** → FAIL.

- [ ] **Step 3: Implement.** In `main.py`:

Add imports: `import dispatch`, `import migration`, `import subscriptions` (keep `import control`, `geo` is now unused — remove `import geo`; remove `import re` and `import unicodedata` if the moved code was their only user).

Delete `_notify`. Replace the four runner/cycle functions with:

```python
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
    if fresh:
        keys = {k for listings in fresh.values() for k in listings}
        _seed(name, keys, debug)
        for search in fresh:
            subscriptions.mark_seeded(key, search)

    note = f"{failed} search(es) failed" if failed else None
    return {"seen": len(set(found) | {k for l in fresh.values() for k in l}),
            "new": len(new_keys), "sent": sent, "note": note}


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
```
(`_seed` stays as-is. Note its message says "enabled"; fine for new searches too.)

Replace `run_cycle`:

```python
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
            log.warning("%s: source unavailable, skipping this cycle (%s)", name, exc)
            results[name] = {"skipped": "site unavailable"}
        except Exception as exc:  # one bad source must not stop the other
            log.exception("%s: cycle failed", name)
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            results[name] = {"error": type(exc).__name__}

    if failures and debug:
        debug.send_simple_msg("Notifier errors:\n" + "\n".join(failures))
    return results
```

Update `Scheduler`: constructor param `notifier` → `bot` (`self.bot`, `self.replies = replies or control.Replies(bot)`), add `self.force = False`; replace `cycle`/`request_cycle`:

```python
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
```
(The global pause check is gone: pause is per group now and handled by the matcher.)

Replace `main()` body from the Telegram setup down:

```python
    telegram_config = config.get("telegram", {})
    bot = TelegramBot(apikey)

    debug_chat = os.environ.get("DEBUGGING_CHAT_ID")
    debug = TelegramBot(apikey, chat_id=debug_chat) if debug_chat else None

    store.init()
    migration.migrate_from_config(config)
    interval = int(os.environ.get("RUN_INTERVAL", 3600))
    replies = control.Replies(bot)
    scheduler = Scheduler(config, bot, debug, interval, replies)

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
```
(Remove the now-unused `telegram_config` line if flake complains. Keep the existing RUN_ONCE comment about 409.)

- [ ] **Step 4: Run** — `venv/bin/python -m pytest -q` → PASS (Controller tests come in Task 9; `control.Controller` is only constructed in `main()`, which tests don't call).
- [ ] **Step 5: Commit** — `git add h2snotifier && git commit -m "Refactor runners to fetch once and fan out per group" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 7: Telegram client additions

**Files:**
- Modify: `h2snotifier/telegram.py`
- Test: `h2snotifier/tests/test_telegram.py`

**Interfaces:**
- Produces: `TelegramBot.get_chat_member(chat_id, user_id) -> str|None` (status such as `"creator"`, `"administrator"`, `"member"`); `get_updates` additionally requests `my_chat_member`.

- [ ] **Step 1: Write the failing test** — `h2snotifier/tests/test_telegram.py`:

```python
import json
from types import SimpleNamespace

import telegram


def fake_post(result, ok=True):
    def post(url, data=None, timeout=None):
        post.last = (url, data)
        return SimpleNamespace(ok=ok, status_code=200 if ok else 400, text="", json=lambda: result)
    return post


def test_get_chat_member_returns_status(monkeypatch):
    monkeypatch.setattr(telegram.requests, "post", fake_post({"result": {"status": "administrator"}}))
    bot = telegram.TelegramBot("T")
    assert bot.get_chat_member(-1, 5) == "administrator"


def test_get_chat_member_none_on_failure(monkeypatch):
    monkeypatch.setattr(telegram.requests, "post", fake_post({}, ok=False))
    assert telegram.TelegramBot("T").get_chat_member(-1, 5) is None


def test_get_updates_asks_for_membership_changes(monkeypatch):
    post = fake_post({"result": []})
    monkeypatch.setattr(telegram.requests, "post", post)
    telegram.TelegramBot("T").get_updates(timeout=0)
    assert "my_chat_member" in json.loads(post.last[1]["allowed_updates"])
```

- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** — in `telegram.py`, change `"allowed_updates": json.dumps(["message", "callback_query"])` to `json.dumps(["message", "callback_query", "my_chat_member"])` and add after `answer_callback`:

```python
    def get_chat_member(self, chat_id, user_id):
        """The user's role in a chat ('creator', 'administrator', 'member', ...) or None."""
        response = self._post(
            "getChatMember", {"chat_id": chat_id, "user_id": user_id}, retries=0
        )
        if response is None or not response.ok:
            return None
        return response.json().get("result", {}).get("status")
```
- [ ] **Step 4: Run** — `venv/bin/python -m pytest tests/test_telegram.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add h2snotifier && git commit -m "Telegram: membership updates and getChatMember" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 8: Group settings commands (text handlers)

**Files:**
- Create: `h2snotifier/commands.py`, `h2snotifier/tests/test_commands.py`

**Interfaces:**
- Consumes: `subscriptions.*`, `geo.locate_town`, `registry`.
- Produces: `commands.city(chat_id, argument) -> str`, `commands.price(chat_id, argument) -> str`, `commands.filters_text(sub) -> str`, constants `HELP_GROUP`, `HELP_OWNER`, `SETUP_DONE`, `SETUP_USAGE`. None of them touch Telegram.

- [ ] **Step 1: Write the failing tests** — `h2snotifier/tests/test_commands.py`:

```python
import pytest

import commands
import subscriptions


@pytest.fixture(autouse=True)
def known_towns(monkeypatch):
    towns = {"utrecht", "zeist", "hilversum", "capelle aan den ijssel", "amersfoort", "nijmegen"}
    monkeypatch.setattr(commands.geo, "locate_town",
                        lambda name: (52.0, 5.0) if name.lower() in towns else None)


def test_city_add_with_radius(make_sub):
    make_sub(-1, cities=())
    assert "Utrecht" in commands.city(-1, "add Utrecht 10")
    assert [(l.city, l.radius_km) for l in subscriptions.get(-1).locations] == [("Utrecht", 10.0)]


def test_city_add_multi_word_name_without_radius(make_sub):
    make_sub(-1, cities=())
    commands.city(-1, "add Capelle aan den IJssel")
    assert subscriptions.get(-1).locations[0].city == "Capelle aan den IJssel"
    assert subscriptions.get(-1).locations[0].radius_km is None


def test_city_add_rejects_unknown_town(make_sub):
    make_sub(-1, cities=())
    assert "could not find" in commands.city(-1, "add Atlantis 10")
    assert subscriptions.get(-1).locations == []


def test_city_add_reports_limit(monkeypatch, make_sub):
    monkeypatch.setattr(subscriptions, "MAX_CITIES", 1)
    make_sub(-1, cities=())
    commands.city(-1, "add Utrecht")
    assert "at most 1" in commands.city(-1, "add Zeist")


def test_city_remove_and_list(make_sub):
    make_sub(-1, cities=(("Utrecht", None),))
    assert "Utrecht" in commands.city(-1, "")
    assert "Removed" in commands.city(-1, "remove utrecht")
    assert "not on your list" in commands.city(-1, "remove utrecht")


def test_price_set_and_clear(make_sub):
    make_sub(-1)
    commands.price(-1, "800 1800")
    assert (subscriptions.get(-1).price_min, subscriptions.get(-1).price_max) == (800, 1800)
    commands.price(-1, "- 1500")
    assert (subscriptions.get(-1).price_min, subscriptions.get(-1).price_max) == (None, 1500)
    commands.price(-1, "clear")
    assert subscriptions.get(-1).price_max is None


@pytest.mark.parametrize("bad", ["", "800", "abc 100", "1800 800", "-5 100"])
def test_price_rejects_bad_input(make_sub, bad):
    make_sub(-1)
    assert commands.price(-1, bad).startswith(("❓", "Usage"))
    assert subscriptions.get(-1).price_min is None


def test_filters_text_lists_everything(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),), price_min=800, price_max=1800)
    subscriptions.set_source(-1, "funda", False)
    text = commands.filters_text(subscriptions.get(-1))
    assert "Utrecht" in text and "10 km" in text and "€800" in text and "€1800" in text
    assert "Pararius" in text and "Funda" not in text.split("Sources")[1]


def test_filters_text_for_empty_group_says_nothing_will_arrive(make_sub):
    make_sub(-1, cities=())
    assert "no alerts" in commands.filters_text(subscriptions.get(-1)).lower()
```

- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** — `h2snotifier/commands.py`:

```python
"""
What a group's admins can type to change their filters.

Every function takes the chat id and the text after the command and returns
the reply. Nothing here talks to Telegram, so it is easy to test, and the
Controller stays a router.
"""

import geo
import registry
import subscriptions
from subscriptions import LimitError

SETUP_USAGE = "Send /setup <code> with the invite code the bot owner gave you."

SETUP_DONE = (
    "✅ This group is set up. Alerts will arrive {where}.\n\n"
    "Next: tell me where you are looking.\n"
    "/city add Utrecht 10 - a city and, optionally, a radius in km\n"
    "/price 800 1800 - minimum and maximum rent (use - for no limit)\n"
    "/panel - switch sources on or off\n"
    "/help - everything else\n\n"
    "Until you add a city, nothing will be sent."
)

HELP_GROUP = (
    "🎛 Rental notifier\n\n"
    "/city - list your cities; /city add Utrecht 10; /city remove Utrecht\n"
    "/price 800 1800 - rent range (- means no limit, /price clear removes it)\n"
    "/filters - show all current settings\n"
    "/panel - buttons for sources and pause\n"
    "/status - what is on\n"
    "/last - when each source was last checked\n"
    "/timezone - show or set the clock, e.g. /timezone Europe/Amsterdam\n"
    "/settopic - send alerts to the topic you type this in\n"
    "/pause, /resume - stop or restart alerts for this group\n"
    "/check - run a check now\n\n"
    "Holland2Stay matches the exact city name; the other sources also use the radius."
)

HELP_OWNER = (
    "🛠 Owner commands (private chat)\n\n"
    "/invite - make a one-time code for a friend's group\n"
    "/groups - list active groups\n"
    "/revoke <chat id> - switch a group off\n\n"
    "A friend adds the bot to their group and, as a group admin, sends /setup <code>."
)

CITY_USAGE = "Usage: /city add <name> [km] · /city remove <name> · /city"


def parse_radius(token):
    try:
        value = float(token.lower().removesuffix("km"))
    except ValueError:
        return None
    return value if 0 < value <= 100 else None


def city(chat_id, argument):
    parts = (argument or "").split()
    if not parts:
        return _cities_line(subscriptions.get(chat_id))
    action, rest = parts[0].lower(), parts[1:]

    if action == "add":
        radius = None
        if len(rest) > 1 and parse_radius(rest[-1]) is not None:
            radius = parse_radius(rest[-1])
            rest = rest[:-1]
        name = " ".join(rest).strip()
        if not name:
            return CITY_USAGE
        if not geo.locate_town(name):
            return (f"❓ I could not find a Dutch town called {name!r}. "
                    "Check the spelling (or the lookup service may be down - try again).")
        try:
            subscriptions.add_location(chat_id, name, radius)
        except LimitError as exc:
            return f"⛔ {exc}"
        reach = f" within {radius:g} km" if radius else ""
        return f"✅ Watching {name}{reach}."

    if action in ("remove", "rm", "del"):
        name = " ".join(rest).strip()
        if not name:
            return CITY_USAGE
        if subscriptions.remove_location(chat_id, name):
            return f"✅ Removed {name}."
        return f"{name} is not on your list."

    return CITY_USAGE


def _amount(token):
    if token in ("-", "any"):
        return None
    value = int(token.replace("€", "").replace(".", "").replace(",", ""))
    if value < 0:
        raise ValueError(token)
    return value


PRICE_USAGE = "❓ Usage: /price <min> <max>, for example /price 800 1800 (use - for no limit, /price clear to remove)."


def price(chat_id, argument):
    parts = (argument or "").split()
    if parts and parts[0].lower() in ("clear", "off", "none"):
        subscriptions.set_price(chat_id, None, None)
        return "✅ Price filter removed."
    if len(parts) != 2:
        return PRICE_USAGE
    try:
        low, high = (_amount(part) for part in parts)
    except ValueError:
        return PRICE_USAGE
    if low is not None and high is not None and low > high:
        return "❓ The minimum is higher than the maximum."
    subscriptions.set_price(chat_id, low, high)
    return f"✅ Rent range: {_price_line(low, high)}."


def _price_line(low, high):
    if low is None and high is None:
        return "any price"
    if low is None:
        return f"up to €{high}"
    if high is None:
        return f"from €{low}"
    return f"€{low} - €{high}"


def _place(loc):
    text = loc.city
    if loc.radius_km:
        text += f" ({loc.radius_km:g} km)"
    if loc.source:
        text += f" [{registry.label(loc.source)} only]"
    return text


def _cities_line(sub):
    if sub is None or not sub.locations:
        return "No cities yet. Add one with /city add Utrecht 10"
    return "📍 " + ", ".join(_place(loc) for loc in sub.locations)


def filters_text(sub):
    sources = [registry.label(key) for key in registry.CONFIG_KEYS if key in sub.sources]
    lines = [
        "🔎 Filters for this group",
        "",
        _cities_line(sub),
        f"💶 {_price_line(sub.price_min, sub.price_max)}",
        f"📡 Sources: {', '.join(sources) if sources else 'none'}",
    ]
    if sub.paused:
        lines.append("⏸ Alerts are paused")
    if not sub.locations:
        lines.append("\nNo alerts will arrive until you add a city.")
    return "\n".join(lines)
```
Note: `test_price_rejects_bad_input` expects replies starting with `❓` or `Usage` — `PRICE_USAGE` starts with `❓` ✓; `"1800 800"` → "❓ The minimum..." ✓; `"-5 100"` → `_amount("-5")` parses `-5` → raises ValueError → usage ✓ (the `-` special-case only matches the bare token).

- [ ] **Step 4: Run** — `venv/bin/python -m pytest tests/test_commands.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add h2snotifier && git commit -m "Add per-group settings command handlers" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 9: Per-group Controller (setup, invites, routing, panels)

**Files:**
- Modify: `h2snotifier/control.py` (rewrite the module-level helpers and `Controller`; `Replies`, `_describe`, `_message_id`, `_ago`, `suggest_timezones` stay)
- Test: `h2snotifier/tests/test_control.py`

**Interfaces:**
- Consumes: `commands`, `subscriptions`, `store`, `registry`, `bot.get_chat_member/send_simple_msg/delete_message/edit_text/answer_callback/set_commands/get_updates`, `run_now(reply_to=None, force=False) -> bool`.
- Produces: `Controller(bot, config, run_now, replies=None, clock=time.monotonic)` with `handle(update)` and `listen_forever(stop=None)`; helpers `is_owner(user_id, config)`, `panel_text(sub, config)`, `panel_keyboard(sub, config)`, `status_text(sub, config)`, `last_checks_text(sub, config)`, `timezone_text(sub, argument=None)`; constants `COMMANDS`, `DENIED`.

- [ ] **Step 1: Write the failing tests** — `h2snotifier/tests/test_control.py`:

```python
from types import SimpleNamespace

import pytest

import commands
import control
import subscriptions

OWNER = 1
CONFIG = {"telegram": {"admin_ids": [OWNER]}, "funda": {"enabled": True}, "ikwilhuren": {"enabled": True}}


class Bot:
    def __init__(self, roles=None):
        self.roles, self.sent, self.edits, self.answers, self.deleted = roles or {}, [], [], [], []

    def send_simple_msg(self, text, chat_id=None, message_thread_id=None, keyboard=None, **kw):
        self.sent.append({"chat_id": chat_id, "thread": message_thread_id, "text": text, "kb": keyboard})
        n = len(self.sent)
        return SimpleNamespace(ok=True, json=lambda: {"result": {"message_id": n}})

    def get_chat_member(self, chat_id, user_id):
        return self.roles.get((chat_id, user_id))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def edit_text(self, chat_id, message_id, text, keyboard=None):
        self.edits.append((chat_id, message_id, text))

    def answer_callback(self, callback_id, text=None, alert=False):
        self.answers.append((text, alert))

    def set_commands(self, commands_):
        pass


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, reply_to=None, force=False):
        self.calls.append(force)
        return True


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(commands.geo, "locate_town", lambda name: (52.0, 5.0))

    def build(roles=None):
        bot, runner = Bot(roles), Runner()
        clock = SimpleNamespace(now=0.0)
        ctl = control.Controller(bot, CONFIG, runner, clock=lambda: clock.now)
        ctl.clock_state = clock
        return ctl, bot, runner

    return build


def group_msg(text, chat_id=-100, user=5, thread=None, topic=False, **extra):
    msg = {"text": text, "chat": {"id": chat_id, "type": "supergroup", "title": "Friends"},
           "from": {"id": user}, "message_id": 1}
    if thread is not None:
        msg["message_thread_id"] = thread
    if topic:
        msg["is_topic_message"] = True
    msg.update(extra)
    return {"update_id": 1, "message": msg}


def private_msg(text, user):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": user, "type": "private"}, "from": {"id": user}}}


def test_owner_creates_invite_and_group_admin_redeems_it(make):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    ctl.handle(private_msg("/invite", OWNER))
    code = bot.sent[-1]["text"].split("/setup ")[1].split()[0]
    ctl.handle(group_msg(f"/setup {code}", thread=42, topic=True))
    sub = subscriptions.get_active(-100)
    assert sub is not None and sub.topic_id == 42 and sub.title == "Friends"
    assert "42" in bot.sent[-1]["text"] or "this topic" in bot.sent[-1]["text"]


def test_setup_in_a_plain_group_reply_does_not_pick_a_topic(make):
    ctl, bot, _ = make({(-100, 5): "creator"})
    code = subscriptions.create_invite()
    # Non-forum groups put message_thread_id on replies, but is_topic_message is absent.
    ctl.handle(group_msg(f"/setup {code}", thread=999, topic=False))
    assert subscriptions.get_active(-100).topic_id is None


def test_setup_needs_a_group_admin(make):
    ctl, bot, _ = make({(-100, 5): "member"})
    code = subscriptions.create_invite()
    ctl.handle(group_msg(f"/setup {code}"))
    assert subscriptions.get_active(-100) is None
    assert bot.sent[-1]["text"] == control.DENIED


def test_used_or_bad_invite_is_refused(make):
    ctl, bot, _ = make({(-100, 5): "administrator", (-200, 5): "administrator"})
    code = subscriptions.create_invite()
    ctl.handle(group_msg(f"/setup {code}", chat_id=-100))
    ctl.handle(group_msg(f"/setup {code}", chat_id=-200))
    assert subscriptions.get_active(-200) is None
    assert "not valid" in bot.sent[-1]["text"]


def test_commands_in_unregistered_groups_are_ignored(make):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    ctl.handle(group_msg("/city add Utrecht"))
    assert bot.sent == []


def test_non_admin_cannot_change_filters(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "member"})
    make_sub(-100, cities=())
    ctl.handle(group_msg("/city add Utrecht"))
    assert subscriptions.get(-100).locations == []
    assert bot.sent[-1]["text"] == control.DENIED


def test_admin_changes_filters_and_owner_is_always_admin(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100, cities=())
    ctl.handle(group_msg("/city add Utrecht 10"))
    ctl.handle(group_msg("/price 800 1800", user=OWNER))
    sub = subscriptions.get(-100)
    assert [(l.city, l.radius_km) for l in sub.locations] == [("Utrecht", 10.0)]
    assert (sub.price_min, sub.price_max) == (800, 1800)


def test_replies_go_to_the_topic_only_when_it_is_a_topic(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    ctl.handle(group_msg("/filters", thread=7, topic=True))
    ctl.handle(group_msg("/filters", thread=555, topic=False))
    assert [m["thread"] for m in bot.sent] == [7, None]


def test_settopic_moves_alerts(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    ctl.handle(group_msg("/settopic", thread=7, topic=True))
    assert subscriptions.get(-100).topic_id == 7
    ctl.handle(group_msg("/settopic"))
    assert subscriptions.get(-100).topic_id is None


def test_stranger_in_private_chat_gets_the_invite_only_notice(make):
    ctl, bot, _ = make()
    ctl.handle(private_msg("/invite", 99))
    assert bot.sent[-1]["text"] == control.INVITE_ONLY
    assert subscriptions.has_any() is False


def test_owner_can_list_and_revoke_groups(make, make_sub):
    ctl, bot, _ = make()
    make_sub(-100)
    ctl.handle(private_msg("/groups", OWNER))
    assert "-100" in bot.sent[-1]["text"]
    ctl.handle(private_msg("/revoke -100", OWNER))
    assert subscriptions.get_active(-100) is None


def test_bot_removed_from_group_deactivates_it(make, make_sub):
    ctl, bot, _ = make()
    make_sub(-100)
    ctl.handle({"update_id": 1, "my_chat_member": {
        "chat": {"id": -100, "type": "supergroup"}, "new_chat_member": {"status": "kicked"}}})
    assert subscriptions.get_active(-100) is None


def test_supergroup_upgrade_message_moves_the_subscription(make, make_sub):
    ctl, bot, _ = make()
    make_sub(-100)
    ctl.handle({"update_id": 1, "message": {
        "chat": {"id": -100, "type": "group"}, "migrate_to_chat_id": -1001, "message_id": 2}})
    assert subscriptions.get_active(-1001) is not None and subscriptions.get(-100) is None


def test_check_cooldown_for_non_owners_and_force_for_owner(make, make_sub):
    ctl, bot, runner = make({(-100, 5): "administrator"})
    make_sub(-100)
    ctl.handle(group_msg("/check"))
    ctl.handle(group_msg("/check"))                      # inside the cooldown
    assert runner.calls == [False]
    assert "Try again" in bot.sent[-1]["text"]
    ctl.handle(group_msg("/check", user=OWNER))
    ctl.handle(group_msg("/check", user=OWNER))          # owner is never throttled
    assert runner.calls == [False, True, True]
    ctl.clock_state.now += control.CHECK_COOLDOWN_MINUTES * 60 + 1
    ctl.handle(group_msg("/check"))
    assert runner.calls[-1] is False and len(runner.calls) == 4


def test_panel_buttons_toggle_pause_and_sources_for_that_group_only(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    make_sub(-200)

    def tap(data, chat_id=-100):
        ctl.handle({"update_id": 1, "callback_query": {
            "id": "q", "data": data, "from": {"id": 5},
            "message": {"chat": {"id": chat_id}, "message_id": 10}}})

    tap("toggle:funda")
    tap("pause")
    assert "funda" not in subscriptions.get(-100).sources and subscriptions.get(-100).paused
    assert "funda" in subscriptions.get(-200).sources and not subscriptions.get(-200).paused
    assert bot.edits[-1][0] == -100


def test_button_from_a_non_admin_is_refused(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "member"})
    make_sub(-100)
    ctl.handle({"update_id": 1, "callback_query": {
        "id": "q", "data": "pause", "from": {"id": 5}, "message": {"chat": {"id": -100}, "message_id": 10}}})
    assert not subscriptions.get(-100).paused
    assert bot.answers[-1] == (control.DENIED, True)


def test_timezone_is_per_group(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    make_sub(-200)
    ctl.handle(group_msg("/timezone Asia/Tehran"))
    assert subscriptions.get(-100).timezone == "Asia/Tehran"
    assert subscriptions.get(-200).timezone == "Europe/Amsterdam"
```

- [ ] **Step 2: Run to verify failure** → FAIL (`control.Controller` signature / `INVITE_ONLY` missing).

- [ ] **Step 3: Implement `control.py`.** Keep from the current file: imports (add `import os`, `import commands`, `import subscriptions`), `log`, `Replies`, `_describe`, `_message_id`, `_ago`, `suggest_timezones`. Delete: `PAUSED_KEY`, `SOURCE_KEY`, `TIMEZONE_KEY`, `is_admin`, `is_paused`, `set_paused`, `source_enabled`, `set_source`, `timezone_name`, `user_zone`, `set_timezone`, the old `COMMANDS`/`HELP`, `panel_*`, `status_text`, `last_checks_text`, `timezone_text`, and the old `Controller`. Replace with:

```python
DENIED = "⛔ Only group admins can change this."
OWNER_ONLY = "⛔ Only the bot owner can do that."
INVITE_ONLY = (
    "🔒 This bot is invite-only. Ask the owner for a code, add me to your group, "
    "then send /setup <code> there as a group admin."
)
CHECK_COOLDOWN_MINUTES = int(os.environ.get("CHECK_COOLDOWN_MINUTES", "10"))
ADMIN_CACHE_SECONDS = 60

COMMANDS = (
    ("panel", "Open the control panel"),
    ("city", "List, add or remove cities"),
    ("price", "Set the rent range"),
    ("filters", "Show this group's filters"),
    ("status", "What the notifier is doing"),
    ("last", "When each source was last checked"),
    ("timezone", "Show or set the clock used for times"),
    ("settopic", "Send alerts to this topic"),
    ("pause", "Stop alerts for this group"),
    ("resume", "Start alerts again"),
    ("check", "Run a check right now"),
    ("help", "List the commands"),
)


def admin_ids(config):
    ids = config.get("telegram", {}).get("admin_ids", [])
    return {int(value) for value in ids}


def is_owner(user_id, config):
    return user_id in admin_ids(config)


def configured_sources(config):
    """Only sources the owner has switched on globally get buttons."""
    return [key for key in registry.CONFIG_KEYS if config.get(key, {}).get("enabled", False)]


def source_on(sub, key):
    return key in sub.sources


def user_zone(sub):
    """The group's clock, falling back to UTC rather than failing a command."""
    try:
        return sub.timezone, ZoneInfo(sub.timezone)
    except Exception:
        log.warning("unknown timezone %r, showing UTC", sub.timezone)
        return "UTC", utc_timezone.utc


def panel_text(sub, config):
    state = "⏸ Alerts are PAUSED" if sub.paused else "▶️ Alerts are running"
    on = [registry.label(k) for k in configured_sources(config) if source_on(sub, k)]
    return (
        f"🎛 Notifier control\n\n{state}\n"
        f"Listening to: {', '.join(on) if on else 'nothing'}\n\n"
        "Tap a source to switch it on or off. /filters shows your cities and price."
    )


def panel_keyboard(sub, config):
    rows = []
    for key in configured_sources(config):
        mark = "✅" if source_on(sub, key) else "🚫"
        rows.append([{"text": f"{mark} {registry.label(key)}", "callback_data": f"toggle:{key}"}])
    power = (
        {"text": "▶️ Resume alerts", "callback_data": "resume"}
        if sub.paused
        else {"text": "⏸ Pause alerts", "callback_data": "pause"}
    )
    rows.append([power])
    rows.append([
        {"text": "⚡ Check now", "callback_data": "check"},
        {"text": "🕒 Last checks", "callback_data": "last"},
    ])
    rows.append([{"text": "🔄 Refresh", "callback_data": "refresh"}])
    return rows


def status_text(sub, config):
    counts = store.counts_by_source()
    lines = ["⏸ Alerts are PAUSED" if sub.paused else "▶️ Alerts are running", ""]
    for source in registry.SOURCES:
        key = source["config"]
        if not config.get(key, {}).get("enabled", False):
            lines.append(f"➖ {source['label']} - not available")
            continue
        mark = "✅" if source_on(sub, key) else "🚫"
        elapsed = store.minutes_since_run(key)
        when = "never checked" if elapsed is None else f"checked {elapsed:.0f} min ago"
        lines.append(f"{mark} {source['label']} - {counts.get(source['store'], 0)} tracked, {when}")
    return "\n".join(lines)


def last_checks_text(sub, config):
    """When each source last ran, on the group's clock."""
    name, zone = user_zone(sub)
    now = datetime.now(utc_timezone.utc)
    lines = [f"🕒 Last checked  ({name})", ""]
    newest = None
    for source in registry.SOURCES:
        key = source["config"]
        if not config.get(key, {}).get("enabled", False):
            lines.append(f"➖ {source['label']} - not available")
            continue
        mark = "✅" if source_on(sub, key) else "🚫"
        moment = store.last_run(key)
        if moment is None:
            lines.append(f"{mark} {source['label']} - never checked")
            continue
        newest = moment if newest is None else max(newest, moment)
        elapsed = (now - moment).total_seconds() / 60
        stamp = moment.astimezone(zone).strftime("%a %d %b, %H:%M")
        line = f"{mark} {source['label']} - {stamp}  ({_ago(elapsed)})"
        wait = config.get(key, {}).get("min_interval_minutes")
        if wait and elapsed < wait:
            due = moment.timestamp() + wait * 60
            line += f"\n     next due {datetime.fromtimestamp(due, zone).strftime('%H:%M')}"
        lines.append(line)
    if newest is not None:
        stamp = newest.astimezone(zone).strftime("%a %d %b, %H:%M")
        lines.append(f"\nLast activity: {stamp}")
    return "\n".join(lines)


def timezone_text(sub, argument=None):
    """Show the group's clock, or change it when given a name."""
    if not argument:
        name, zone = user_zone(sub)
        here = datetime.now(zone).strftime("%a %d %b, %H:%M")
        return (
            f"🕒 Times are shown in {name}\nRight now that is {here}.\n\n"
            "Change it with a zone name, for example:\n"
            "/timezone Europe/Amsterdam\n/timezone Asia/Tehran"
        )
    name = argument.strip()
    try:
        ZoneInfo(name)
    except Exception:
        hints = suggest_timezones(name)
        tail = "\n\nDid you mean:\n" + "\n".join(hints) if hints else ""
        return f"❓ I do not know the zone {name!r}.{tail}"
    subscriptions.set_timezone(sub.chat_id, name)
    here = datetime.now(ZoneInfo(name)).strftime("%a %d %b, %H:%M")
    return f"🕒 Times now shown in {name}.\nRight now that is {here}."
```

Then the Controller (keep `_discard_backlog` and `listen_forever` exactly as they are in the current file; replace `__init__`, `handle`, and everything after):

```python
class Controller:
    """
    Long-polls Telegram and routes every command to the group it came from.

    Anyone in a registered group may read; only that group's admins (or the bot
    owner) may change anything. Unregistered groups are ignored entirely, so a
    stranger who adds the bot gets silence, not a menu.
    """

    def __init__(self, bot, config, run_now, replies=None, clock=time.monotonic):
        self.bot = bot
        self.config = config
        self.run_now = run_now
        self.replies = replies or Replies(bot)
        self.clock = clock
        self.offset = None
        self.admin_cache = {}
        self.last_check = {}

    # -- plumbing (_discard_backlog and listen_forever unchanged) ---------

    def handle(self, update):
        log.info("update %s", _describe(update))
        if "my_chat_member" in update:
            return self._handle_membership(update["my_chat_member"])
        if "message" in update:
            return self._handle_message(update["message"])
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])

    # -- who may do what --------------------------------------------------

    def is_admin(self, chat_id, user_id, message=None):
        if is_owner(user_id, self.config):
            return True
        # An admin posting anonymously arrives as the group itself.
        if message and (message.get("sender_chat") or {}).get("id") == chat_id:
            return True
        key, now = (chat_id, user_id), self.clock()
        cached = self.admin_cache.get(key)
        if cached and now - cached[0] < ADMIN_CACHE_SECONDS:
            return cached[1]
        result = self.bot.get_chat_member(chat_id, user_id) in ("creator", "administrator")
        self.admin_cache[key] = (now, result)
        return result

    def _check_cooldown(self, chat_id, user_id):
        """Minutes left before this group may /check again; 0 means go ahead."""
        if is_owner(user_id, self.config):
            return 0
        now, last = self.clock(), self.last_check.get(chat_id)
        window = CHECK_COOLDOWN_MINUTES * 60
        if last is not None and now - last < window:
            return int((window - (now - last)) // 60) + 1
        self.last_check[chat_id] = now
        return 0

    # -- membership and migration ----------------------------------------

    def _handle_membership(self, change):
        chat = change.get("chat", {})
        status = (change.get("new_chat_member") or {}).get("status")
        if chat.get("type") in ("group", "supergroup") and status in ("left", "kicked"):
            log.info("removed from chat %s, deactivating", chat.get("id"))
            subscriptions.deactivate(chat["id"])

    # -- messages ---------------------------------------------------------

    def _handle_message(self, message):
        chat = message["chat"]
        chat_id = chat["id"]

        if message.get("migrate_to_chat_id"):
            log.info("group %s became supergroup %s", chat_id, message["migrate_to_chat_id"])
            return subscriptions.move_chat(chat_id, message["migrate_to_chat_id"])

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        # Group commands often arrive as /pause@our_h2stay_bot.
        command = text.split()[0].lstrip("/").split("@")[0].lower()
        argument = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
        user_id = message.get("from", {}).get("id")

        if chat.get("type") == "private":
            return self._handle_private(chat_id, user_id, command, argument)

        # message_thread_id also appears on plain replies in non-forum groups,
        # where it must not be used. Only a real forum topic counts.
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None

        if command == "setup":
            return self._setup(message, chat, user_id, thread, argument)

        sub = subscriptions.get_active(chat_id)
        if sub is None:
            log.info("ignoring /%s in unregistered chat %s", command, chat_id)
            return None

        slot = self.replies.open(chat_id, thread)
        if not self.is_admin(chat_id, user_id, message):
            log.warning("denied /%s from %s in %s", command, user_id, chat_id)
            return self.replies.send(slot, DENIED)
        return self._command(sub, slot, command, argument, thread, user_id)

    def _handle_private(self, chat_id, user_id, command, argument):
        slot = self.replies.open(chat_id, None)
        if not is_owner(user_id, self.config):
            return self.replies.send(slot, INVITE_ONLY)
        if command == "invite":
            code = subscriptions.create_invite()
            return self.replies.send(
                slot,
                f"🎟 Invite code: {code}\n\nIn your friend's group (they must be an admin): "
                f"add this bot, then send\n/setup {code}\nThe code works once.",
            )
        if command == "groups":
            subs = subscriptions.all_active()
            lines = [f"{s.chat_id}  {s.title or '-'}  ({len(s.locations)} places"
                     f"{', paused' if s.paused else ''})" for s in subs]
            return self.replies.send(slot, "\n".join(lines) or "No active groups.")
        if command == "revoke":
            try:
                target = int(argument)
            except ValueError:
                return self.replies.send(slot, "Usage: /revoke <chat id>")
            subscriptions.deactivate(target)
            return self.replies.send(slot, f"Switched off {target}.")
        return self.replies.send(slot, commands.HELP_OWNER)

    def _setup(self, message, chat, user_id, thread, argument):
        chat_id = chat["id"]
        slot = self.replies.open(chat_id, thread)
        if not self.is_admin(chat_id, user_id, message):
            return self.replies.send(slot, DENIED)
        if subscriptions.get_active(chat_id):
            return self.replies.send(slot, "✅ This group is already set up. Try /help.")
        code = argument.strip()
        if not code:
            return self.replies.send(slot, commands.SETUP_USAGE)
        sub = subscriptions.activate(code, chat_id, thread, chat.get("title") or "")
        if sub is None:
            return self.replies.send(slot, "❌ That invite code is not valid or was already used.")
        where = "in this topic" if thread else "in this chat"
        return self.replies.send(slot, commands.SETUP_DONE.format(where=where))

    def _command(self, sub, slot, command, argument, thread, user_id):
        chat_id = sub.chat_id
        send = lambda text, keyboard=None: self.replies.send(slot, text, keyboard)
        fresh = lambda: subscriptions.get(chat_id)

        if command in ("start", "help"):
            return send(commands.HELP_GROUP)
        if command in ("panel", "sources", "control"):
            return send(panel_text(sub, self.config), panel_keyboard(sub, self.config))
        if command == "status":
            return send(status_text(sub, self.config))
        if command in ("last", "checks", "lastcheck"):
            return send(last_checks_text(sub, self.config))
        if command in ("timezone", "tz", "time"):
            return send(timezone_text(sub, argument or None))
        if command == "city":
            return send(commands.city(chat_id, argument))
        if command == "price":
            return send(commands.price(chat_id, argument))
        if command == "filters":
            return send(commands.filters_text(sub))
        if command == "pause":
            subscriptions.set_paused(chat_id, True)
            return send("⏸ Alerts paused for this group.")
        if command in ("resume", "start_alerts"):
            subscriptions.set_paused(chat_id, False)
            return send("▶️ Alerts resumed.")
        if command == "settopic":
            subscriptions.set_topic(chat_id, thread)
            return send("✅ Alerts will arrive in this topic." if thread
                        else "✅ Alerts will arrive in the main chat.")
        if command in ("check", "run"):
            wait = self._check_cooldown(chat_id, user_id)
            if wait:
                return send(f"⏳ Try again in about {wait} min.")
            send("⚡ Checking now…")
            if not self.run_now(reply_to=slot, force=is_owner(user_id, self.config)):
                return send("⏳ A check is already running.")
            return None
        return send(commands.HELP_GROUP)

    # -- buttons ----------------------------------------------------------

    def _handle_callback(self, query):
        data = query.get("data") or ""
        user_id = query.get("from", {}).get("id")
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None

        sub = subscriptions.get_active(chat_id) if chat_id else None
        if sub is None:
            return self.bot.answer_callback(query["id"], "This group is not set up.", alert=True)
        if not self.is_admin(chat_id, user_id):
            log.warning("denied button %r from %s", data, user_id)
            return self.bot.answer_callback(query["id"], DENIED, alert=True)

        note = "Updated"
        if data.startswith("toggle:"):
            key = data.split(":", 1)[1]
            if key in configured_sources(self.config):
                now_on = key not in sub.sources
                subscriptions.set_source(chat_id, key, now_on)
                note = f"{registry.label(key)} {'on' if now_on else 'off'}"
        elif data == "pause":
            subscriptions.set_paused(chat_id, True)
            note = "Alerts paused"
        elif data == "resume":
            subscriptions.set_paused(chat_id, False)
            note = "Alerts resumed"
        elif data == "check":
            wait = self._check_cooldown(chat_id, user_id)
            if wait:
                note = f"Try again in about {wait} min"
            else:
                slot = self.replies.open(chat_id, thread)
                forced = is_owner(user_id, self.config)
                note = "Checking now…" if self.run_now(reply_to=slot, force=forced) else "Already running"
        elif data == "last":
            slot = self.replies.open(chat_id, thread)
            self.replies.send(slot, last_checks_text(sub, self.config))
            note = "Last checks"

        self.bot.answer_callback(query["id"], note)
        if message_id:
            fresh = subscriptions.get(chat_id)
            self.bot.edit_text(
                chat_id, message_id, panel_text(fresh, self.config), panel_keyboard(fresh, self.config)
            )
```
Notes for the implementer: `Replies.send` already accepts `(slot, text, keyboard=None)`. `import store` stays at the top of `control.py` (still used by status/last). Remove the now-unused `import threading`? No — `Replies` uses it; leave imports that are still used.

- [ ] **Step 4: Run** — `venv/bin/python -m pytest -q` → PASS.
- [ ] **Step 5: Commit** — `git add h2snotifier && git commit -m "Route Telegram commands per group; add setup and invite flow" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

### Task 10: Docker, config, docs, end-to-end check

**Files:**
- Modify: `h2snotifier/Dockerfile`, `docker-compose.yml`, `README.md`, `config.example.json` (comment-free; keep content), `.env.example` (no change needed)

- [ ] **Step 1: Dockerfile** — replace the `COPY` line with:

```dockerfile
COPY fetcher.py h2s.py funda.py huurwoningen.py ikwilhuren.py pararius.py \
     geo.py registry.py control.py store.py telegram.py main.py \
     subscriptions.py matcher.py render.py dispatch.py migration.py commands.py ./
```
Verify none are missing: `ls h2snotifier/*.py` should list exactly the files copied (tests excluded).

- [ ] **Step 2: docker-compose.yml** — add under `environment:` after `RUN_INTERVAL`:

```yaml
      # Friends' groups: how many cities one group may watch, how many distinct
      # paid searches the whole bot may run (each costs Firecrawl credits), and
      # how often a non-owner group may press /check.
      MAX_CITIES_PER_GROUP: 5
      MAX_DISTINCT_SEARCHES: 30
      CHECK_COOLDOWN_MINUTES: 10
```
and change the `config.json` volume comment to: `# Global source settings + the owner ids. On first start the old per-chat keys are copied into the database once; after that groups are managed from Telegram.` Remove `:ro` is NOT needed; leave `:ro`.

- [ ] **Step 3: README** — add a section "Sharing the bot with friends" covering: owner `telegram.admin_ids` = the owner; `/invite` in a private chat → code; friend adds the bot to their group, an admin sends `/setup <code>` (in a topic to use that topic); commands (`/city add Utrecht 10`, `/price 800 1800`, `/panel`, `/filters`, `/pause`, `/settopic`); privacy-mode note (commands work with the default privacy mode, no need to make the bot an admin unless the group restricts bots posting); limits and env vars; that Funda/Pararius/Huurwoningen are fetched once per unique city+radius and shared; Holland2Stay matches the exact city name only; that `config.json`'s per-chat keys are read once on first start (migration) and the price filter becomes the group's single widest range; backups = copy `./data/listings.db`.

- [ ] **Step 4: Full test run and image build**

Run: `cd h2snotifier && venv/bin/python -m pytest -q` → all PASS.
Run: `docker build -t h2s-multi-test ./h2snotifier && docker run --rm h2s-multi-test python -c "import main, control, dispatch, matcher, migration, commands, subscriptions, render; print('imports ok')"`
Expected: `imports ok`.

- [ ] **Step 5: Manual end-to-end check** (needs a throwaway bot token and two test groups; run with `RUN_ONCE` unset)

1. Start with a copy of the real `./data` and `config.json`; confirm the log shows `migrated config.json into subscription for chat ...` and that the owner group still receives alerts / `/panel` works.
2. In a private chat as owner: `/invite` → code. In a *second* test group (forum with a topic): add the bot, as admin send `/setup <code>` inside a topic → confirmation arrives in that topic.
3. `/city add Utrecht 10`, `/price 800 1800`, `/filters`, `/panel` toggles, `/pause`/`/resume`, `/settopic` in General.
4. From a non-admin account in that group: `/city add Zeist` → refused.
5. `/check` as a non-admin twice → second says to try again later.
6. Remove the bot from the test group → log shows deactivation; `/groups` no longer lists it.
7. Convert the test group's topics on/off (supergroup upgrade) and trigger an alert → subscription survives (check `/groups`).

- [ ] **Step 6: Commit** — `git add -A && git commit -m "Docker, compose and README for multi-group support" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`

---

## Self-review notes

- **Spec coverage:** data model (T1-2), invite onboarding + admin check + topic rule + group removal + topic-deleted fallback (T4, T9), commands (T8-9), shared fetch set / radius rounding / seeding / caps (T2, T6), matcher (T3), migration (T5), dispatch pacing/retry/dedupe (T4), tests (all tasks), docs/Docker (T10).
- **Spec refinements made during planning (tell the owner):** (a) `sub_locations.source` column so the migrated owner group keeps per-source city lists and Funda credit use does not multiply; (b) price on migration = widest range across *enabled* paid-source searches, so it now also applies to H2S/ikwilhuren (previously unfiltered) — adjustable with `/price`; (c) non-owner `/check` never forces paid sources (`force` plumbed through `Scheduler.request_cycle`); (d) supergroup-upgrade handling (service message + `migrate_to_chat_id` fallback) added because enabling topics changes the chat id; (e) paused groups keep being fetched so resuming does not flood.
- **Known limits:** the `/check` report shows global per-source counts, not per-group; radius step tables for Pararius/Huurwoningen are best-effort (verify against the sites).
