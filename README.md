# Rental Notifier — Holland2Stay + Funda + Huurwoningen + ikwilhuren.nu

Watches Dutch rental listings and posts new ones to a Telegram group — optionally
into a specific **forum topic**, so the alerts stay out of your normal chat.

Four sources, polled independently:

| Source | What it watches | Filter | Cost |
| --- | --- | --- | --- |
| **Holland2Stay** | every residence in their sitemap | by city | free, or 1 credit if Cloudflare blocks |
| **Funda** | any saved search you paste in | anything Funda's UI can filter: area + radius, price, type, rooms, energy label… | 1 credit per poll |
| **Huurwoningen** | any saved search you paste in | area + radius, price, rooms, interior, pets, garden… | 1 credit per poll |
| **ikwilhuren.nu** | their whole national catalogue | by city, or by radius on the map | free |

Enable any combination.

```
🏠 Botter 38
📍 3863 ED #Nijkerk  ·  Funda
💶 €1.600
📐 133 m²  ·  4 rooms  ·  Huis  ·  energy A
[ 🔗 View on Funda ]
```

Each message carries an inline button that opens the listing, and the town is
sent as a hashtag — tap `#Nijkerk` to pull up every listing ever posted there.
Names are squashed into a single tag (`Capelle aan den IJssel` → `#CapelleAanDenIJssel`,
`Huis ter Heide (UT)` → `#HuisTerHeide`) because Telegram tags cannot contain
spaces or punctuation.

## Why this fork exists

The original [JafarAkhondali/Holland2StayNotifier](https://github.com/JafarAkhondali/Holland2StayNotifier)
no longer works, and cannot be fixed by configuration:

- `api.holland2stay.com/graphql` — the endpoint the whole app was built on — now
  answers `Forbidden / graphql-authorization` to **every** query, even `{__typename}`.
  The public Magento API was closed.
- The site was rebuilt as a Next.js app behind **Cloudflare Turnstile**.
  `cloudscraper` (the fix both maintained forks adopted) gets a challenge page,
  and so does a Googlebot user agent.
- Upstream's last commit is June 2024; its issue #12 is literally
  "graphql returns 403", unanswered.

| | Original | This fork |
| --- | --- | --- |
| Data source | Magento GraphQL API | sitemap + page scraping |
| Cloudflare | blocked | handled |
| Sources | Holland2Stay | Holland2Stay **+ Funda + Huurwoningen + ikwilhuren.nu** |
| Telegram topics | ✗ | ✓ |
| Scheduling | host cron | built into the container |
| Deploy | manual venv | `docker compose up -d` |
| Image | — | published to GHCR by CI |

### How it works

**Holland2Stay** — `/sitemap.xml` is fetched first over plain HTTP (free); if
Cloudflare blocks it, the fetch falls back to [Firecrawl](https://firecrawl.dev)
(1 credit). The set of listing keys is diffed against SQLite. Only genuinely new
keys trigger a detail-page fetch, and each street's city is cached the first time
it is seen — so a new listing in an already-known building is filtered by city
for free, and listings outside your cities are never fetched at all.

**Funda** and **Huurwoningen** — both block plain HTTP outright, so their search
pages always go through Firecrawl (1 credit each). A single search page carries
full listing data for 15–25 listings, so there is no per-listing fetch. Both are
sorted newest-first, which makes pagination unnecessary.

**ikwilhuren.nu** has no bot protection at all, so it never costs a credit. Its
search form is a POST, but the server ignores the filter fields — so the notifier
does the opposite of what the form intends: it asks for the whole catalogue in
one request (~330 listings) and filters by city locally. No detail fetches
either; one cycle is one HTTP request.

Paid sources can be throttled independently with `min_interval_minutes`, so you
can poll the free sources hourly while only spending Firecrawl credits on Funda
and Huurwoningen every few hours.

**The first run of each source is silent.** Everything currently listed is
recorded as already-seen so you are not flooded with a hundred messages. Enabling
a source later only seeds that source. After that seed, **every** new listing is
sent — there is no cap that quietly drops matches.

## Quick start

You need Docker, a Telegram bot token, and a [Firecrawl API key](https://firecrawl.dev)
(the free tier covers hourly polling).

```bash
mkdir rental-notifier && cd rental-notifier
curl -O https://raw.githubusercontent.com/amirzenoozi/Holland2StayNotifier/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/amirzenoozi/Holland2StayNotifier/main/.env.example
curl -o config.json https://raw.githubusercontent.com/amirzenoozi/Holland2StayNotifier/main/config.example.json

# fill in both files, then:
docker compose up -d
docker compose logs -f
```

Those three files are all the host needs — the image comes from GHCR.

### `.env`

```ini
TELEGRAM_API_KEY=123456:ABC-your-bot-token
FIRECRAWL_API_KEY=fc-your-key
DEBUGGING_CHAT_ID=-1001234567890   # optional: where errors are reported
```

### `config.json`

```json
{
  "telegram": {
    "chat_id": -1001234567890,
    "topic_id": 184,
    "admin_ids": [61462805],
    "timezone": "Europe/Amsterdam"
  },

  "holland2stay": {
    "enabled": true,
    "cities": ["Utrecht", "Nijmegen", "Amersfoort"],
    "max_lookups_per_cycle": 25
  },

  "funda": {
    "enabled": true,
    "min_interval_minutes": 120,
    "searches": [
      { "name": "Amersfoort 10km", "area": "amersfoort", "radius": "10km", "type": "huur", "price": "1000-2000" }
    ]
  },

  "huurwoningen": {
    "enabled": true,
    "min_interval_minutes": 120,
    "searches": [
      { "name": "Amersfoort 10km", "area": "amersfoort", "radius": "10km", "price": "1000-2000" }
    ]
  },

  "ikwilhuren": {
    "enabled": true,
    "areas": [
      { "place": "Nijkerk",    "radius_km": 40 },
      { "place": "Utrecht",    "radius_km": 20 },
      { "place": "Amersfoort", "radius_km": 20 },
      { "place": "Zwolle",     "radius_km": 20 }
    ],
    "cities": ["Nijmegen"]
  }
}
```

Omit `topic_id` to post in the group's main thread.

**Holland2Stay cities** (exact spelling): Amersfoort, Amsterdam, Arnhem,
Capelle aan den IJssel, Delft, Den Bosch, Den Haag, Diemen, Dordrecht, Eindhoven,
Groningen, Haarlem, Helmond, Leiden, Maarssen, Maastricht, Nieuwegein, Nijmegen,
Rijswijk, Rotterdam, Sittard, Tilburg, Utrecht, Velp, Zeist, Zoetermeer.

**Funda searches** take either structured fields (`area`, `radius`, `price`,
`type`, `object_type`, `rooms`, `bedrooms`, `floor_area`) or — better — a raw
`url`. Tune the filters on funda.nl until the results look right, copy the
address bar, and paste it:

```json
{ "name": "Nijkerk area", "url": "https://www.funda.nl/zoeken/huur?selected_area=%5B%22nijkerk,15km%22%5D&price=%221000-2000%22&sort=%22date_down%22" }
```

That gives you every filter Funda has without this project needing to model any
of them. Add `"sort=%22date_down%22"` so the newest listings stay on page one.

**Huurwoningen searches** work the same way: structured fields (`area`, `radius`,
`price`, plus any extra query parameters under `params`) or a raw `url` copied
from huurwoningen.nl:

```json
{ "name": "Nijkerk area", "url": "https://www.huurwoningen.nl/in/nijkerk/?price=1000-2000&radius=10" }
```

Its default sort is already newest-first, so no sort parameter is needed.

**ikwilhuren.nu** takes places rather than searches, because the site returns
its entire national catalogue in one request and the filtering happens here.
There are two ways to say what you want, and a listing only has to satisfy one
of them:

- `areas` draws circles on the map: `{ "place": "Nijkerk", "radius_km": 40 }`
  notifies about anything within 40 km of Nijkerk, including villages you would
  never have thought to name.
- `cities` names towns exactly, spelled as the site spells them
  (`Amersfoort`, `Den Haag`); matching is case-insensitive.

Leave both out to be notified about every listing in the country.

Radius matching works by putting each listing on the map from the four-digit
part of its postcode, using [PDOK](https://www.pdok.nl/), the Dutch
government's geocoder — free, no key, no account. Every answer is cached in
the database, so each postcode area and each anchor town is looked up once and
never again.

### Finding your chat and topic IDs

Open the target topic in Telegram Web and copy a message link — it looks like
`https://t.me/c/1900208566/184/185`. Then `chat_id` is `-100` + the first number
(`-1001900208566`) and `topic_id` is the second (`184`). **Add the bot to the
group as an admin**, or it cannot post.

## Controlling the bot from Telegram

The bot listens for commands and button taps, so you can steer it without
touching the server.

| Command | What it does |
| --- | --- |
| `/panel` | the control panel — buttons to pause and to pick sources |
| `/status` | what is on, how much is tracked, when each source last ran |
| `/pause` | stop all alerts |
| `/resume` | start them again |
| `/check` | run a check right now instead of waiting for the hour |
| `/last` | when each source was last checked, and when it is next due |
| `/timezone` | show the clock times are printed in, or set it: `/timezone Europe/Amsterdam` |
| `/help` | list the commands |

The panel draws one button per source, marked ✅ when it is on and 🚫 when it is
off, plus pause/resume, check-now and last-checks buttons. Tapping redraws the
panel in place rather than posting a new message.

A command that speaks twice replaces its own message rather than posting a
second one — `/check` says "checking now" and then turns that same message
into the report. The replacing stops there: the next command starts a new
message and leaves the last one alone. House alerts are never replaced.

```
🎛 Notifier control

▶️ Alerts are running
Listening to: Holland2Stay, Huurwoningen, ikwilhuren.nu

[✅ Holland2Stay]
[🚫 Funda]
[✅ Huurwoningen]
[✅ ikwilhuren.nu]
[⏸ Pause alerts]
[⚡ Check now]  [🕒 Last checks]
[🔄 Refresh]
```

`/last` answers on your clock, not the server's:

```
🕒 Last checked  (Europe/Amsterdam)

✅ Holland2Stay — Thu 03 Sep, 15:45  (25 min ago)
     next due 16:45
✅ Funda — Thu 03 Sep, 13:00  (3h 10m ago)
✅ ikwilhuren.nu — Thu 03 Sep, 16:10  (just now)
```

Telegram does not tell a bot where you are — an update carries your id, name
and language, but no timezone — so the clock is a setting rather than
something the bot can work out. It defaults to `Europe/Amsterdam`; set another
with `/timezone Asia/Tehran`, and it is remembered in the database. A name it
does not recognise comes back with suggestions.

These switches live in the database, not in `config.json`, so they survive
restarts and image updates. `config.json` decides which sources *exist*; the
panel decides which are listening right now. A source turned off in the file
cannot be turned on from Telegram — it may have no search terms to run.

Only the Telegram user ids in `telegram.admin_ids` can change anything.
Everyone else — typing a command or tapping a button on a panel someone left in
the chat — gets `⛔ You do not have access to this feature, call AmirKhan!`. To
find your id, message [@RawDataBot](https://t.me/RawDataBot) and read
`message.from.id`.

## Configuration reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUN_INTERVAL` | `600` | seconds between polls — the fastest any source can run |
| `CONFIG_PATH` | `/app/config.json` | config location in the container |
| `DB_PATH` | `/data/listings.db` | SQLite state, bind-mounted to `./data/listings.db` next to the compose file |
| `RUN_ONCE` | unset | set to `1` to run a single cycle and exit, instead of looping and listening |

| Config key | Default | Meaning |
| --- | --- | --- |
| `telegram.admin_ids` | `[]` | user ids allowed to use the commands and buttons; empty means nobody |
| `telegram.timezone` | `Europe/Amsterdam` | clock used for times in `/last`; `/timezone` overrides it and wins |
| `holland2stay.cities` / `ikwilhuren.cities` | — | city names to notify about (case-insensitive) |
| `ikwilhuren.areas` | — | circles on the map: `{"place": "Nijkerk", "radius_km": 40}`. A listing matches if it falls in any circle **or** the city list |
| `holland2stay.max_lookups_per_cycle` | `15` | cap on detail-page fetches per cycle; listings past the cap are not dropped, just deferred to the next cycle |
| `<source>.min_interval_minutes` | unset | skip this source unless that many minutes have passed since its last run — how you spend fewer credits on the paid sources without slowing the free one |

**On the interval and cost.** `RUN_INTERVAL` sets how fast the loop turns, and
that is the fastest any source can run; `min_interval_minutes` only holds a
source *back*. So run the loop at the speed the free sources deserve and pin the
paid ones.

ikwilhuren.nu costs nothing — one plain HTTP request to their own server, no
Firecrawl, and the whole catalogue comes back in it. The Holland2Stay sitemap is
free too whenever plain HTTP gets through, but falls back to Firecrawl (1 credit)
when Cloudflare shuts the door, so it is worth pinning as well. Funda and
Huurwoningen cost 1 credit per search page every time.

The shipped defaults — a 10-minute loop, 60 minutes on Holland2Stay, 120 on
Funda and Huurwoningen — check the free sources six times an hour while keeping
the paid ones near 360 credits/month each, inside the 1,000/month free plan.
Detail-page fetches are unaffected by the interval: each new listing is fetched
exactly once however often you look.

## Operating

```bash
docker compose up -d        # start
docker compose down         # stop
docker compose logs -f      # watch
docker compose pull && docker compose up -d   # update to the latest image
docker compose restart      # apply config.json changes
```

### Where the data lives

Everything the bot remembers sits in one SQLite file beside the compose file:

```
rental-notifier/
├── .env                 bot token, Firecrawl key
├── config.json          what to watch, who may command it
├── docker-compose.yml
└── data/
    └── listings.db      ← every listing seen, per source
```

| Table | What it holds |
| --- | --- |
| `listings` | every listing key ever seen, with its source and city |
| `streets` | learned street → city map, so Holland2Stay skips other cities for free |
| `places` | geocoded coordinates, cached forever, misses included |
| `runs` | when each source last ran |
| `settings` | pause state, per-source switches, your timezone |

Back it up or read it with plain `sqlite3` — no Docker needed:

```bash
sqlite3 data/listings.db "select source, count(*) from listings group by source"
cp data/listings.db ~/listings-backup.db
```

Deleting the file makes the next run re-seed silently — it will not re-notify
you about everything, but it forgets anything posted while it was down. The
switches live there too, so pause and per-source state survive `down`/`up` and
image updates.

Logs are not written to a file; they go to stdout and Docker keeps them
(`docker compose logs -f`), capped at 10 MB × 3.

To force a check by hand, use `/check` in Telegram, or:

```bash
docker exec -e RUN_ONCE=1 h2snotifier python main.py
```

`RUN_ONCE=1` matters: without it the command starts a second Telegram poller
alongside the container's, and Telegram rejects whichever one loses with a 409.

## Building locally

Uncomment the `build:` line in `docker-compose.yml`, comment out `image:`, then
`docker compose up -d --build`.

## Layout

```
docker-compose.yml        # what you run
.env.example
config.example.json
h2snotifier/
  main.py                 # scrape loop + cycle over enabled sources
  control.py              # Telegram commands and the control panel
  registry.py             # the list of sources, in one place
  fetcher.py              # shared plain-HTTP + Firecrawl fetch layer
  h2s.py                  # Holland2Stay: sitemap + detail parsing
  funda.py                # Funda: search page parsing
  huurwoningen.py         # Huurwoningen: search page parsing
  ikwilhuren.py           # ikwilhuren.nu: full catalogue over plain HTTP
  store.py                # SQLite: seen listings, street→city cache, run times
  telegram.py             # send, edit, long-poll; topics and inline buttons
.github/workflows/docker-publish.yml   # builds and pushes to GHCR
```

## Caveats

- Holland2Stay listings report the **exclusive** price only; the inclusive price
  and occupancy sit behind a UI expander that is not in the page markup.
- Funda and Huurwoningen both prohibit automated access in their terms. Polling
  one search page every couple of hours is modest, but this is your call to make.
- Huurwoningen shows `Prijs op aanvraag` on some listings; those are reported as
  "Price on request" rather than skipped.
- Holland2Stay is rebranding to **Codomo**; the URLs this depends on may move.
- Scraped markup is not an API. When a site redesigns, parsing breaks — errors
  are reported to `DEBUGGING_CHAT_ID` so you find out quickly.

## License

MIT, as the original.
