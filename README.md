# Holland2Stay Notifier

Watches [Holland2Stay](https://www.holland2stay.com) for new rental listings and posts them to a Telegram chat — or to a specific **topic** inside a Telegram forum group.

```
🏠 Berg en Dalseweg 79A110
📍 Nijmegen
💶 €1,658.00 excl.
📐 44.5 m2 · Loft
📅 Available per September 22, 2026
https://www.holland2stay.com/residences/berg-en-dalseweg-79a110.html
```

---

## Why this fork exists

The original project scraped `api.holland2stay.com/graphql`. That endpoint is **closed** — every query now returns `Forbidden / graphql-authorization` — and the whole site sits behind Cloudflare Turnstile, which also defeats the `cloudscraper` workaround other forks adopted. Upstream has been unmaintained since June 2024.

This fork replaces the data layer entirely:

| | Original | This fork |
|---|---|---|
| Data source | Magento GraphQL API | `sitemap.xml` + listing detail pages |
| New-listing signal | API `available_to_book` filter | diff of listing URL keys against SQLite |
| Blocking | broken by Cloudflare | direct fetch first, [Firecrawl](https://firecrawl.dev) as fallback |
| City filter | numeric city IDs | learned street → city map |
| Scheduling | external cron | self-contained loop in the container |
| Telegram target | chat only | chat **or forum topic** |

### How it works

1. Fetch `sitemap.xml` (~200 live listings). Plain HTTP is tried first and is free; if Cloudflare blocks it, Firecrawl is used at a cost of 1 credit.
2. Diff the listing keys against SQLite. **The very first run records everything silently** so you don't get 200 messages at once.
3. For each genuinely new key, look up the street prefix (`berg-en-dalseweg-79a110` → `berg-en-dalseweg`) in a learned cache. Known non-target city ⇒ skipped for free.
4. Otherwise fetch that one detail page as markdown (1 credit), parse it, and remember the street's city forever.
5. Post to Telegram if the city is one you're watching.

Steady-state cost is roughly **one credit per cycle plus one per new listing in your cities** — comfortably inside Firecrawl's free 1,000 credits/month at hourly checks.

---

## Quick start

You need a server with Docker, a Telegram bot, and a [Firecrawl API key](https://firecrawl.dev) (free tier is enough).

```bash
mkdir h2snotifier && cd h2snotifier
curl -O https://raw.githubusercontent.com/amirzenoozi/Holland2StayNotifier/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/amirzenoozi/Holland2StayNotifier/main/.env.example
curl -o config.json https://raw.githubusercontent.com/amirzenoozi/Holland2StayNotifier/main/config.example.json

# fill in both files, then:
chmod 600 .env config.json
docker compose up -d
docker compose logs -f
```

Stop it with `docker compose down`. State lives in a named volume, so stopping and starting never re-sends old listings.

### `.env`

```ini
TELEGRAM_API_KEY=123456789:AAF...     # from @BotFather
DEBUGGING_CHAT_ID=-1001234567890      # where errors get reported
FIRECRAWL_API_KEY=fc-...              # from firecrawl.dev
```

### `config.json`

```json
{
  "cities": ["Utrecht", "Nijmegen", "Zeist", "Nieuwegein", "Maarssen", "Amersfoort"],
  "telegram": { "chat_id": -1001234567890, "topic_id": 184 },
  "max_new_per_cycle": 25,
  "max_lookups_per_cycle": 15
}
```

| Key | Meaning |
|---|---|
| `cities` | City names exactly as Holland2Stay writes them. Omit or leave empty to get every city. |
| `telegram.chat_id` | Group ID, including the `-100` prefix. |
| `telegram.topic_id` | Forum topic ID. Omit for a normal group. |
| `max_new_per_cycle` | If more new listings than this appear at once (a site change, or a wiped database), absorb them silently instead of flooding the chat. |
| `max_lookups_per_cycle` | Hard ceiling on paid detail fetches per cycle. |

**Cities currently served:** Amersfoort, Amsterdam, Arnhem, Capelle aan den IJssel, Delft, Den Bosch, Den Haag, Diemen, Dordrecht, Eindhoven, Groningen, Haarlem, Helmond, Leiden, Maarssen, Maastricht, Nieuwegein, Nijmegen, Rijswijk, Rotterdam, Sittard, Tilburg, Utrecht, Velp, Zeist, Zoetermeer.

### Finding your chat and topic IDs

Open the target topic in Telegram Web and copy the message link — `https://t.me/c/1900208566/184/185` means:

- `chat_id` = `-1001900208566` (prefix `-100` to the first number)
- `topic_id` = `184` (the second number)

Add the bot to the group and **make it an admin**, otherwise it cannot post.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `RUN_INTERVAL` | `3600` | Seconds between checks. Lower it only if you have Firecrawl credits to spare. |
| `CONFIG_PATH` | `/app/config.json` | Config file location inside the container. |
| `DB_PATH` | `/data/listings.db` | SQLite state file. |

## Operating

```bash
docker compose logs -f                      # watch
docker compose restart                      # apply config.json changes
docker compose pull && docker compose up -d # upgrade to the latest image
docker exec h2snotifier python main.py      # force a check right now
docker compose down -v                      # wipe state (next run re-seeds silently)
```

## Building locally

Uncomment the `build:` line in `docker-compose.yml`, then `docker compose up -d --build`. Images for `linux/amd64` and `linux/arm64` are otherwise published to `ghcr.io/amirzenoozi/holland2staynotifier:latest` by GitHub Actions on every push to `main`.

## Layout

```
docker-compose.yml          # what you deploy
.env.example                # secrets template
config.example.json         # cities + Telegram target template
h2snotifier/
├── main.py                 # one cycle: diff, enrich, notify
├── source.py               # sitemap + detail fetching and parsing
├── store.py                # SQLite: seen listings, learned streets
├── telegram.py             # topic-aware sendMessage with 429 handling
├── entrypoint.sh           # run / sleep loop, replaces cron
└── Dockerfile
```

## Caveats

- Detail-page markdown omits the inclusive price and occupancy limit, so messages show the **exclusive** price.
- Holland2Stay is rebranding to **Codomo**. When URLs change the sitemap parse will fail — you'll get an error in your debug chat rather than silence.
- Scraping is inherently fragile. Treat this as a best-effort alert, not a guarantee.

## License

See [LICENSE](LICENSE). Original project by [JafarAkhondali](https://github.com/JafarAkhondali/Holland2StayNotifier).
