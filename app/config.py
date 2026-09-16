"""Runtime configuration for the card-desk workers.

Values come from environment variables; a simple .env file in the project
root (card-scanner_workers/.env) is loaded if present so local runs work
without exporting anything.
"""
import os
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    path = _BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# Shared Postgres (Neon) — the same database Prisma uses for the app.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Broker — the same RabbitMQ the Next.js publisher pushes profile tasks to.
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

# Profile-collection queue (must match lib/queue/config.ts on the Next.js side:
# QUEUES.PROFILE_COLLECTION = "CS-profile:collection").
QUEUE_PROFILE_COLLECTION = os.environ.get(
    "QUEUE_PROFILE_COLLECTION", "CS-profile:collection")

# OpenAI — used by the workers (Responses API + built-in web_search) to find
# real profile URLs, bios and company details. Same key as the Next.js app.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ENRICH_MODEL = os.environ.get("ENRICH_MODEL", "gpt-5.4-mini")
ENRICH_EFFORT = os.environ.get("ENRICH_EFFORT", "low")
SEARCH_CONTEXT = os.environ.get("SEARCH_CONTEXT", "medium")

# Worker behaviour
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
WORKER_TIMEOUT_MIN = int(os.environ.get("WORKER_TIMEOUT_MIN", "10"))

# Photo worker: how long to wait for LinkedIn/Twitter URLs to land in the
# enrichment row (they are written by the other workers in parallel) before
# giving up and resolving from whatever is already there.
PHOTO_POLL_TIMEOUT = float(os.environ.get("PHOTO_POLL_TIMEOUT", "30"))
PHOTO_POLL_INTERVAL = float(os.environ.get("PHOTO_POLL_INTERVAL", "5"))

# Outbound web scraping
SCRAPE_TIMEOUT = int(os.environ.get("SCRAPE_TIMEOUT", "8"))
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "CardDesk enrichment worker")
