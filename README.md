# card-scanner-workers

Python RabbitMQ workers that enrich the Card Scanner PostgreSQL schema
(the same Neon database the Next.js app owns via Prisma).

Two independent features live in one repository:

| Feature | Package | Queue | Entry point |
|---|---|---|---|
| Profile collection (research / linkedin / whatsapp / instagram / facebook / twitter) | `app/profile` | `CS-profile:collection` | `python -m app.profile.main` |
| Subsidiary matching (company → subsidiary tags) | `app/subsidiary` | `CS-subsidiary:matching` | `python -m app.subsidiary.main` |

They share the database, the broker and the OpenAI key, but they run as
separate processes, consume separate queues, and never import each other.
Only `app/shared` (the `.env` loader and the Postgres connect helper) is
common.

## Layout

```text
app/
  shared/          # .env loader + Neon-safe psycopg connection helper
  profile/         # feature 1
    config.py queue.py handlers.py common.py ai.py db.py main.py
    workers/{combined,research,linkedin,social,whatsapp}.py
  subsidiary/      # feature 2
    config.py queue.py handlers.py logic.py search.py ai.py db.py main.py
data/              # subsidiary reference keywords + gold set
scripts/           # per-feature CLIs (see below)
tests/
  profile/         # profile unit tests
  subsidiary/      # subsidiary unit tests
  test_independence.py  # asserts the two features never import each other
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `DATABASE_URL`, `RABBITMQ_URL`, `OPENAI_API_KEY`, plus the variables
for whichever feature(s) you run. Both features read the repo-root `.env`.

Apply the Prisma migrations from the `card-scanner` repository before running,
then seed the subsidiary reference data once:

```powershell
python -m scripts.subsidiary_seed_reference
```

## Run

Profile-collection receiver:

```powershell
python -m app.profile.main
```

Subsidiary-matching receiver:

```powershell
python -m app.subsidiary.main
```

Run each as its own process/service. Either can be restarted without
affecting the other.

## Scripts

Profile:

```powershell
python -m scripts.profile_publish_sample --ai --name "Satya Nadella" --company Microsoft
python -m scripts.profile_publish_sample --publish research <contactId>
python -m scripts.profile_publish_sample --sample 0
```

Subsidiary:

```powershell
python -m scripts.subsidiary_sweep --dry-run
python -m scripts.subsidiary_sweep
python -m scripts.subsidiary_publish_sample "Example Cable Works" --dry-run
python -m scripts.subsidiary_publish_sample "Example Cable Works" --publish
python -m scripts.subsidiary_eval_thresholds
```

## Search providers

The subsidiary matcher discovers providers from `SEARCH_*_API_KEY` variables
and tries them in `SEARCH_PRIORITY` order, round-robin across keys with
per-key cooldowns. Extras: `SEARCH_<PROVIDER>_API_KEY_2`, `_3`, ... add more
keys for the same provider.

| Provider | Variable | Endpoint | Returns page body |
|---|---|---|---|
| Tavily | `SEARCH_TAVILY_API_KEY` | `api.tavily.com/search` | yes |
| Exa | `SEARCH_EXA_API_KEY` | `api.exa.ai/search` | yes (text) |
| Firecrawl | `SEARCH_FIRECRAWL_API_KEY` | `api.firecrawl.dev/v2/search` | yes (markdown, scraped in the same call) |
| SerpAPI | `SEARCH_SERPAPI_API_KEY` | `serpapi.com/search.json` | no; pages fetched directly |

Firecrawl costs one search plus one scrape per result, so `SEARCH_MAX_RESULTS`
directly controls spend and latency.

## Address pass

The address worker extracts company addresses from the company site and search
results, then geocodes them. Useful rerun commands:

```powershell
# Rerun exactly the same contacts while comparing before/after behaviour
python -m scripts.profile_enrich --pass address --inline --ids "id1,id2,id3"

# Bypass the temporary cache for a clean live run
python -m scripts.profile_enrich --pass address --inline --limit 20 --no-cache

# Review approximate locations by pin precision
python -m scripts.address_review --precision city
```

PIN state/district/city comes from `data/india_pins.json`, generated from the
GeoNames India postal-code export (CC BY 4.0). Regenerate with:

```powershell
python -m scripts.build_pin_prefixes
```

The distance gate anchors on the PIN centre, not the district. Each PIN is
resolved once through Geoapify's postcode search and cached in
`data/pin_areas.json` (gitignored, disposable). GeoNames coordinates remain the
labelled fallback when Geoapify has no answer. Delete that file to force a
fresh lookup for every PIN.

### Temporary address cache

`app/profile/address_cache.py` is a deliberate, disposable cache. It freezes
cleaned pages, search URLs, deterministic failures and full geocode results
under `data/address_cache/` for a few days so reruns can be compared on the
same inputs.

- `ADDRESS_CACHE=0` or `--no-cache` disables it.
- Page TTL is 72h; failure TTL is 12h.
- No raw HTML, no database table, no cross-machine guarantee.
- Page text may contain emails/phones; the directory is gitignored.
- **Removal:** after the address overhaul is signed off, delete
  `app/profile/address_cache.py`, its call sites (grep `TEMPORARY CACHE`), the
  `ADDRESS_CACHE_*` keys, its tests, this section, and `data/address_cache/`.

## Tests

```powershell
python -m pytest
```

The suite needs no database, broker or network: each feature fakes its
boundaries. `tests/test_independence.py` enforces the merge guarantee that
neither feature imports the other.
