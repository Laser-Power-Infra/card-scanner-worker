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

## Tests

```powershell
python -m pytest
```

The suite needs no database, broker or network: each feature fakes its
boundaries. `tests/test_independence.py` enforces the merge guarantee that
neither feature imports the other.
