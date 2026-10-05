# Multi-group subscriptions — design

## Goal

Let other people (friends) add the bot to their own Telegram group and receive rental alerts in a topic of their choice (or the General thread when the group has no topics), with their own filters: cities + radius, price range, and sources on/off. One Docker container, one bot token, SQLite storage, on the owner's server. Existing behaviour for the owner's current group must not change.

## Decisions (agreed)

- **Architecture:** one shared service; each group is a subscription row; scrape once, fan out per group.
- **Paid sources (Funda, Pararius, Huurwoningen):** shared pool. Each unique `(source, city, radius)` is fetched once and shared by every group that matches.
- **Access:** invite codes. The owner (`telegram.admin_ids` in config) generates a one-time code; a group admin activates the group with `/setup <code>`. After that, any admin of that group edits its filters.
- **Filters in v1:** cities + radius, price min/max, sources on/off. (Size/rooms are out of scope.)

## Out of scope (YAGNI)

Min m² / rooms filters, per-group source schedules, web UI, multiple owners with separate quotas, payments.

## Data model (additive SQLite migration in `store.init`)

| Table | Columns |
|---|---|
| `subscriptions` | `chat_id` PK, `topic_id` NULL, `title`, `price_min`, `price_max`, `timezone` DEFAULT 'Europe/Amsterdam', `paused` DEFAULT 0, `active` DEFAULT 1, `created_at` |
| `sub_locations` | `chat_id`, `city`, `radius_km` NULL; PK (`chat_id`, `city`) |
| `sub_sources` | `chat_id`, `source` (config key), `enabled`; PK (`chat_id`, `source`) |
| `invites` | `code` PK, `created_at`, `used_by_chat` NULL, `used_at` NULL |
| `searches` | `source`, `city`, `radius_km`, `seeded` DEFAULT 0; PK (`source`, `city`, `radius_km`) — derived, rebuilt from subscriptions |
| `deliveries` | `chat_id`, `source`, `url_key`, `ok`, `sent_at`; PK (`chat_id`, `source`, `url_key`) |

Existing `listings`, `streets`, `places`, `runs` tables are unchanged. `listings` stays the global "seen" catalogue; `settings` keeps only global values. The global `paused` and `source:*` keys are replaced by `subscriptions.paused` and `sub_sources`. Note (from `registry.py`): `listings.source` uses the *store* key, `runs`/`sub_sources` use the *config* key — keep that split.

## Components

- **`subscriptions.py` (new):** CRUD for subscriptions/locations/sources, invite create/redeem, `rebuild_searches()`, limits (max cities per group, global cap on distinct paid searches via env `MAX_DISTINCT_SEARCHES`).
- **`matcher.py` (new):** pure `matches(listing, sub)` — city name match or `geo.area_match` for radius, price range (`price_excl`/`price_incl`, a listing with no price (price on request) always passes the price filter), source enabled, not paused. No I/O besides the existing `geo` cache.
- **`main.py`:** `run_*` split into **collect** (fetch, diff against `listings`, return new listings with their source) and **dispatch** (for each new listing, find matching active subscriptions, send, record in `deliveries`). Scheduler/lock/`min_interval_minutes` behaviour is kept.
- **`control.py`:** `Controller` routes every update by `chat_id` to its subscription; replaces global `is_admin`/`is_paused`/`source_enabled` with per-group equivalents. Owner commands (`/invite`, `/groups`, `/revoke`) only work in a private chat with an owner id.
- **`telegram.py`:** one `TelegramBot` (token) used for all chats; `get_updates` adds `my_chat_member` to `allowed_updates`; add `get_chat_member`. Sends take an explicit `chat_id`/`message_thread_id` (already supported).
- **`config.json`:** shrinks to global settings (owner `admin_ids`, per-source `enabled` and `min_interval_minutes`, `max_lookups_per_cycle`). `docker-compose.yml` is unchanged.

## Onboarding and access flow

1. Owner: `/invite` (private chat) → bot returns a one-time code.
2. Friend adds the bot to their group, a group admin sends `/setup <code>`.
3. Bot verifies admin status via `getChatMember` (status `administrator`/`creator`), redeems the code, creates the subscription with all sources on, no cities yet, and replies with next steps (`/city add …`).
4. **Topic:** `topic_id` = `message_thread_id` of the `/setup` message only when `message.is_topic_message` is true (in non-forum groups a plain reply also carries `message_thread_id` and must be ignored). Sent from General or a non-forum group → `topic_id` NULL. `/settopic` run inside another topic moves alerts there.
5. Commands from groups without an active subscription are ignored (except `/setup`). If the bot is removed/kicked (`my_chat_member` → `left`/`kicked`) or a send returns 403 / "chat not found", the subscription is set `active=0`. If a send fails because the topic was deleted, fall back to General and tell the group once.

## Commands (per group, admin-only)

`/panel` (buttons: sources, pause/resume, check, last checks) · `/city add <name> [km]` · `/city remove <name>` · `/price <min> <max>` (either may be `-` for no limit) · `/filters` (summary) · `/status` · `/last` · `/timezone` · `/pause` · `/resume` · `/check` · `/settopic` · `/help`.

City names are validated with `geo.locate_town` (PDOK) before saving; unknown names are rejected with a hint. Radius is optional: no radius means exact city-name match. Guardrails: max cities per group (default 5), global distinct-search cap, `/check` for non-owners cooldown (default 10 min) and never bypasses `min_interval_minutes` for paid sources.

## Scraping and fan-out

- **Search set:** `searches` is the union of `(source, city, radius)` across active subscriptions (paused ones included, so their seeding stays current) with that source enabled. A search with no subscribers is dropped and no longer fetched.
- **Funda / Pararius / Huurwoningen:** each search is fetched price-agnostic (city + radius only). The requested radius is rounded **up** to the nearest value the site supports; the exact distance is then enforced locally with `geo` (listings carry a postcode). Price is filtered per group. The first fetch of a new search (`seeded=0`) records its listings silently and sets `seeded=1`, so adding a city never floods the group. Per-search fetch failure is logged and skipped, as today.
- **ikwilhuren.nu:** one nationwide fetch as today; each new listing is matched per group via `matcher`.
- **Holland2Stay:** city targets for the detail-lookup budget become the union of all groups' cities; each fetched listing is matched per group by city name.
- **Dispatch:** a listing is sent to every matching group not already in `deliveries` for it; the row is written with the send result and failed sends (`ok=0`) are retried next cycle up to a small attempt limit. Sends are paced per chat (existing 3 s sleep becomes per-chat, and the existing 429 back-off in `telegram.py` stays).
- **Dedupe/ordering:** a listing found by two overlapping searches is sent once per group (guarded by `deliveries`).

## Migration

On first start, if `subscriptions` is empty and `config.json` has `telegram.chat_id`, create subscription #1 from it: `chat_id`, `topic_id`, `timezone`, one `sub_locations` row per `holland2stay.cities` entry, per funda/pararius/huurwoningen `searches` entry (area + radius, price range converted to `price_min/max`), and per ikwilhuren `areas`/`cities`; per-source `enabled` flags copied. Old `config.json` keys remain readable and are ignored afterwards (documented in the README). Existing `settings` pause/source toggles are carried over once. Because `listings` is the global catalogue, the owner's group sees no re-alerts and no gap. `.env`/Docker Compose need no change.

## Error handling

- Source failures keep today's isolation (`SiteUnavailable` quiet skip, other exceptions reported to the debug chat).
- Invalid user input (bad city, bad price, limit hit) → one clear reply, no state change.
- Callback buttons re-check admin status for the tapping user in that chat.
- All DB writes for a command are single transactions.

## Testing (pytest, new)

- `matcher`: city match, radius via mocked geo, price bounds, paused/disabled source, price-on-request.
- `subscriptions`: invite single-use, limits, `rebuild_searches`, topic extraction rule (`is_topic_message`).
- Migration from the repo's `config.example.json` to subscription #1.
- Collect/dispatch with fake sources and a fake Telegram client: fan-out to two groups with different filters, silent seeding for a new search, duplicate suppression, deactivation on 403.
- No network calls in tests; Firecrawl and PDOK are stubbed.

## Rollout order (feeds the implementation plan)

1. Schema + `subscriptions.py` + migration + tests.
2. `matcher.py` + tests.
3. Refactor `main.py` to collect/dispatch behind the single migrated subscription (behaviour parity for the owner).
4. Controller routing, `/setup`, `/invite`, filter commands, panel.
5. Search-set sharing, radius rounding + local trim, per-search seeding, caps.
6. README/config docs, compose notes.
