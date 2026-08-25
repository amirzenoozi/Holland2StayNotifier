# Rental Notifier — Holland2Stay + Funda + Huurwoningen

Watches Dutch rental listings and posts new ones to a Telegram group — optionally
into a specific **forum topic**, so the alerts stay out of your normal chat.

Three sources, polled independently:

| Source | What it watches | Filter |
| --- | --- | --- |
| **Holland2Stay** | every residence in their sitemap | by city |
| **Funda** | any saved search you paste in | anything Funda's UI can filter: area + radius, price, type, rooms, energy label… |
| **Huurwoningen** | any saved search you paste in | area + radius, price, rooms, interior, pets, garden… |

Enable any combination.

```
🏠 Botter 38
📍 3863 ED Nijkerk  ·  Funda
💶 €1.600
📐 133 m²  ·  4 rooms  ·  Huis  ·  energy A
[ 🔗 View on Funda ]
```

Each message carries an inline button that opens the listing.

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
| Sources | Holland2Stay | Holland2Stay **+ Funda + Huurwoningen** |
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

Paid sources can be throttled independently with `min_interval_minutes`, so you
can run the container hourly for Holland2Stay while only spending Firecrawl
credits on the other two every few hours.

**The first run of each source is silent.** Everything currently listed is
recorded as already-seen so you are not flooded with a hundred messages. Enabling
a second source later only seeds that source.

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
  "telegram": { "chat_id": -1001234567890, "topic_id": 184 },
  "max_new_per_cycle": 25,

  "holland2stay": {
    "enabled": true,
    "cities": ["Utrecht", "Nijmegen", "Amersfoort"],
    "max_lookups_per_cycle": 15
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

### Finding your chat and topic IDs

Open the target topic in Telegram Web and copy a message link — it looks like
`https://t.me/c/1900208566/184/185`. Then `chat_id` is `-100` + the first number
(`-1001900208566`) and `topic_id` is the second (`184`). **Add the bot to the
group as an admin**, or it cannot post.

## Configuration reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUN_INTERVAL` | `3600` | seconds between polls |
| `CONFIG_PATH` | `/app/config.json` | config location in the container |
| `DB_PATH` | `/data/listings.db` | SQLite state (persisted in a volume) |

| Config key | Default | Meaning |
| --- | --- | --- |
| `max_new_per_cycle` | `25` | more new listings than this in one cycle are absorbed silently (guards against a site-wide relist spamming the group) |
| `holland2stay.max_lookups_per_cycle` | `15` | cap on detail-page fetches per cycle |
| `<source>.min_interval_minutes` | unset | skip this source unless that many minutes have passed since its last run — how you spend fewer credits on the paid sources without slowing the free one |

**On the interval and cost.** Each poll costs 1 Firecrawl credit per search page,
so hourly ≈ 720 credits/month per source against a 1,000/month free plan. With
two paid sources, hourly polling does not fit — set `min_interval_minutes: 120`
on each (≈ 360/month each) and leave `RUN_INTERVAL` at an hour so Holland2Stay,
which usually succeeds over free plain HTTP, keeps checking every cycle.

## Operating

```bash
docker compose up -d        # start
docker compose down         # stop
docker compose logs -f      # watch
docker compose pull && docker compose up -d   # update to the latest image
docker compose restart      # apply config.json changes
```

State lives in the `h2s_data` volume. Deleting it makes the next run re-seed
silently — it will not re-notify you about everything.

## Building locally

Uncomment the `build:` line in `docker-compose.yml`, comment out `image:`, then
`docker compose up -d --build`.

## Layout

```
docker-compose.yml        # what you run
.env.example
config.example.json
h2snotifier/
  main.py                 # orchestrates a cycle over enabled sources
  fetcher.py              # shared plain-HTTP + Firecrawl fetch layer
  h2s.py                  # Holland2Stay: sitemap + detail parsing
  funda.py                # Funda: search page parsing
  huurwoningen.py         # Huurwoningen: search page parsing
  store.py                # SQLite: seen listings, street→city cache, run times
  telegram.py             # sendMessage with topic support
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
