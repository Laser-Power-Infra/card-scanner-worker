"""Postgres persistence for enrichment results.

Workers write their slice of a contact's enrichment straight into the shared
`enrichment` table (same database the Next.js app uses via Prisma). One
connection per thread is kept open and reused.
"""
import logging
import time

import psycopg

from ..shared import db as shared_db
from . import config

log = logging.getLogger("card-desk.db")

# The enrichment table is owned by Prisma (see the Next.js schema.prisma and
# the add_enrichment migration). We only write into it, never create/alter it.
# EnrichmentStatus is a Postgres enum and is case-sensitive, so map our
# lowercase statuses onto the enum values it accepts.
_STATUS_ENUM = {
    "pending": "PENDING",
    "running": "RUNNING",
    "done": "DONE",
    "partial": "PARTIAL",
    "failed": "FAILED",
}

# Columns a worker may write through merge_enrichment().
_WRITABLE = {
    "linkedin_url", "linkedin_photo_url", "avatar_url",
    "facebook_url", "twitter_url", "instagram_url", "other_profiles",
    "company_details", "company_core_business", "official_site",
    "sources", "location", "career_background", "summary",
    "address_line1", "address_line2", "city", "state",
    "postal_code", "country",
}

def get_conn():
    """A psycopg connection owned by the calling thread."""
    return shared_db.get_conn(config.DATABASE_URL)


def _run(sql, params=None):
    """Execute a statement, reconnecting once if the server dropped the idle
    connection (common after long AI calls)."""
    try:
        with get_conn().cursor() as cur:
            cur.execute(sql, params)
    except psycopg.OperationalError:
        log.warning("DB connection dropped; reconnecting and retrying")
        shared_db.reset_conn()
        with get_conn().cursor() as cur:
            cur.execute(sql, params)


def _fetchone(sql, params=None):
    try:
        with get_conn().cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    except psycopg.OperationalError:
        log.warning("DB connection dropped; reconnecting and retrying")
        shared_db.reset_conn()
        with get_conn().cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def get_contact(contact_id):
    """Read the Prisma-managed contact row so a task that only carries a
    contactId can still be enriched. Returns None if the table/row is gone."""
    try:
        return _fetchone(
            'SELECT "id", "fullName", "company", "jobTitle", "emails", '
            '"website", "linkedin", "companyLocation" FROM "Contact" '
            'WHERE "id" = %s', (contact_id,))
    except Exception:
        log.warning("get_contact(%s) failed (contact table missing?)",
                    contact_id, exc_info=True)
        return None


def get_enrichment(contact_id):
    return _fetchone("SELECT * FROM enrichment WHERE contact_id = %s",
                     (contact_id,))


def upsert_enrichment(contact_id):
    # Prisma's updated_at column is NOT NULL with no default, so a fresh row
    # must set it explicitly.
    _run("INSERT INTO enrichment (contact_id, updated_at) "
         "VALUES (%s, now()) "
         "ON CONFLICT (contact_id) DO NOTHING", (contact_id,))


def reset_enrichment(contact_id, attempts, expected):
    """Fresh research run: clear previous scrape fields, restart counters."""
    upsert_enrichment(contact_id)
    _run(
        "UPDATE enrichment SET status = %s, attempts = %s, "
        "completed = 0, failed = 0, expected = %s, "
        "linkedin_url = NULL, linkedin_photo_url = NULL, avatar_url = NULL, "
        "facebook_url = NULL, twitter_url = NULL, instagram_url = NULL, "
        "other_profiles = NULL, company_details = NULL, "
        "company_core_business = NULL, official_site = NULL, "
        "sources = NULL, updated_at = now() WHERE contact_id = %s",
        (_STATUS_ENUM["running"], attempts, expected, contact_id))


def merge_enrichment(contact_id, fields):
    """Field-scoped upsert: only the given, non-empty columns are written, so
    parallel workers never clobber each other's slice."""
    sets, vals = [], []
    for key, val in fields.items():
        if key not in _WRITABLE:
            continue
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        sets.append('"%s" = %%s' % key)
        vals.append(val)
    if not sets:
        return
    upsert_enrichment(contact_id)
    vals.append(contact_id)
    _run("UPDATE enrichment SET %s, updated_at = now() "
         "WHERE contact_id = %%s" % ", ".join(sets), vals)


def increment_completed(contact_id):
    upsert_enrichment(contact_id)
    _run("UPDATE enrichment SET completed = completed + 1, "
         "updated_at = now() WHERE contact_id = %s", (contact_id,))


def increment_failed(contact_id):
    upsert_enrichment(contact_id)
    _run("UPDATE enrichment SET failed = failed + 1, "
         "updated_at = now() WHERE contact_id = %s", (contact_id,))


def finalize(contact_id):
    """Flip status once every expected subtask has reported (done or failed)."""
    row = get_enrichment(contact_id)
    if not row:
        return
    if row["completed"] + row["failed"] < row["expected"]:
        return
    if row["failed"] == 0:
        status = "DONE"
    elif row["completed"] > 0:
        status = "PARTIAL"
    else:
        status = "FAILED"
    _run("UPDATE enrichment SET status = %s, enriched_at = now(), "
         "updated_at = now() WHERE contact_id = %s",
         (_STATUS_ENUM[status.lower()], contact_id))
    log.info("Enrichment finalized for contact %s -> %s", contact_id, status)


def wait_for_urls(contact_id, timeout, interval):
    """Photo worker helper: wait until the LinkedIn/Twitter URLs (written by
    the parallel linkedin/social workers) land, or give up after timeout."""
    deadline = time.time() + timeout
    linkedin = twitter = ""
    while True:
        row = get_enrichment(contact_id)
        if row:
            linkedin = row.get("linkedin_url") or ""
            twitter = row.get("twitter_url") or ""
        if (linkedin or twitter) or time.time() >= deadline:
            return linkedin, twitter
        time.sleep(interval)


def append_profile_line(contact_id, label, url):
    """Add or replace a 'Label: url' line in other_profiles without clobbering
    lines written by other workers/platforms."""
    if not url:
        return
    display = "WhatsApp" if label.lower() == "whatsapp" else label.capitalize()
    row = get_enrichment(contact_id)
    lines = []
    if row and row.get("other_profiles"):
        lines = [l.strip() for l in row["other_profiles"].split("\n") if l.strip()]
    lines = [l for l in lines
             if not l.lower().startswith(label.lower() + ":")]
    lines.append("%s: %s" % (display, url))
    merge_enrichment(contact_id, {"other_profiles": "\n".join(lines)[:900]})


def set_status(contact_id, status):
    """Set the enrichment status using the Prisma enum value (uppercase)."""
    upsert_enrichment(contact_id)
    value = _STATUS_ENUM.get((status or "pending").lower(), "PENDING")
    _run("UPDATE enrichment SET status = %s, updated_at = now() "
         "WHERE contact_id = %s", (value, contact_id))


def set_contact_job_title(contact_id, title):
    """Backfill the Prisma-managed Contact.jobTitle (ID cards print no title)."""
    if not title:
        return
    _run('UPDATE "Contact" SET "jobTitle" = %s WHERE "id" = %s',
         (str(title)[:120], contact_id))


def address_schema_ok():
    """True if the enrichment address columns exist."""
    try:
        row = _fetchone(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'enrichment' AND column_name = 'address_verified_at'")
        return bool(row)
    except Exception:
        return False


def location_cache_schema_ok():
    """True if LocationCache has the extended columns."""
    try:
        row = _fetchone(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'LocationCache' AND column_name = 'normalizedQuery'")
        return bool(row)
    except Exception:
        return False


def update_address(contact_id, fields, verified_at=None):
    """Write verified address fields. Does not touch status or attempts."""
    merge_enrichment(contact_id, fields or {})
    if verified_at is not None:
        _run("UPDATE enrichment SET address_verified_at = %s, updated_at = now() "
             "WHERE contact_id = %s", (verified_at, contact_id))


def mark_address_failed(contact_id):
    """Increment the per-contact address attempt counter."""
    upsert_enrichment(contact_id)
    _run("UPDATE enrichment SET address_attempts = COALESCE(address_attempts, 0) + 1, "
         "updated_at = now() WHERE contact_id = %s", (contact_id,))


def set_company_location_if_empty(contact_id, value):
    if not value:
        return
    _run('UPDATE "Contact" SET "companyLocation" = %s '
         'WHERE "id" = %s AND (coalesce(trim("companyLocation"), \'\') = \'\')',
         (str(value)[:200], contact_id))


def get_location_cache(normalized_query):
    if not normalized_query:
        return None
    try:
        return _fetchone(
            'SELECT "placeId", latitude, longitude, "formattedAddress", '
            'country, state, "expiresAt", resolved '
            'FROM "LocationCache" WHERE "normalizedQuery" = %s', (normalized_query,))
    except Exception:
        return None


def upsert_location_cache(normalized_query, query, latitude, longitude, place_id,
                         formatted_address, country, state, expires_at):
    import uuid
    _run(
        'INSERT INTO "LocationCache" '
        '(id, query, "normalizedQuery", latitude, longitude, "placeId", '
        '"formattedAddress", country, state, resolved, "expiresAt", "createdAt", "updatedAt") '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, now(), now()) '
        'ON CONFLICT ("normalizedQuery") DO UPDATE SET '
        'query = EXCLUDED.query, latitude = EXCLUDED.latitude, '
        'longitude = EXCLUDED.longitude, "placeId" = EXCLUDED."placeId", '
        '"formattedAddress" = EXCLUDED."formattedAddress", country = EXCLUDED.country, '
        'state = EXCLUDED.state, resolved = true, "expiresAt" = EXCLUDED."expiresAt", '
        '"updatedAt" = now()',
        (str(uuid.uuid4()), query, normalized_query, latitude, longitude,
         place_id, formatted_address, country, state, expires_at))
