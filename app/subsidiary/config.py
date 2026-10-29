"""Environment-backed configuration for the subsidiary-matching worker."""
import os

from ..shared.config import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
QUEUE = os.environ.get("QUEUE_SUBSIDIARY_MATCHING", "CS-subsidiary:matching")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-6-luna")
# Reasoning models reject `temperature`; leave empty to omit the field entirely.
LLM_TEMPERATURE = os.environ.get("LLM_TEMPERATURE", "0")
SEARCH_PRIORITY = os.environ.get("SEARCH_PRIORITY", "tavily,exa,firecrawl,serpapi")
SEARCH_MAX_RESULTS = int(os.environ.get("SEARCH_MAX_RESULTS", "5"))
SEARCH_SOFT_COOLDOWN = int(os.environ.get("SEARCH_SOFT_COOLDOWN", "60"))
SEARCH_CREDIT_COOLDOWN = int(os.environ.get("SEARCH_CREDIT_COOLDOWN", "21600"))
SEARCH_SCRAPE_TIMEOUT = int(os.environ.get("SEARCH_SCRAPE_TIMEOUT", "8"))
MATCH_TOP_N = int(os.environ.get("MATCH_TOP_N", "10"))
MATCH_MAX_PHRASES = int(os.environ.get("MATCH_MAX_PHRASES", "30"))
MATCH_THRESHOLD_DEFAULT = float(os.environ.get("MATCH_THRESHOLD_DEFAULT", "0.5"))
PAUSE_PROBE_INTERVAL = int(os.environ.get("PAUSE_PROBE_INTERVAL", "60"))
DIRECT_SITE_FAILURE_COOLDOWN = int(os.environ.get("DIRECT_SITE_FAILURE_COOLDOWN", "3600"))
MATCH_WEIGHT_OWN_BUSINESS = float(os.environ.get("MATCH_WEIGHT_OWN_BUSINESS", "1.0"))
MATCH_WEIGHT_TECHNICAL = float(os.environ.get("MATCH_WEIGHT_TECHNICAL", "0.6"))
MATCH_WEIGHT_DEMAND_SIDE = float(os.environ.get("MATCH_WEIGHT_DEMAND_SIDE", "0.3"))
MATCH_PENALTY_WEIGHT = float(os.environ.get("MATCH_PENALTY_WEIGHT", "0.3"))
# Comma-separated extra consume-only domains treated as non-corporate.
GENERIC_EMAIL_DOMAINS = os.environ.get("GENERIC_EMAIL_DOMAINS", "")
