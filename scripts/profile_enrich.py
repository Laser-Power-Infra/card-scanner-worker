"""Batch runner for profile enrichment.

Selects contacts from Postgres and either publishes tasks to the
CS-profile:collection queue (default) or runs the handlers inline.
"""
import argparse
import json
import logging
import os

import pika

from app.profile import budget, config, db, handlers
from app.shared import db as shared_db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("profile_enrich")

def _conn():
    return shared_db.connect(config.DATABASE_URL)


def _select(pass_name, limit, only_missing, ids=None, company=None):
    where = ['coalesce(trim(c."company"), \'\') <> \'\'']
    params = []
    if pass_name == "address":
        if only_missing:
            where.append("(e.address_verified_at IS NULL)")
            where.append("(coalesce(e.address_attempts, 0) < %s)")
            params.append(config.ADDRESS_MAX_ATTEMPTS)
    else:
        where.append("coalesce(e.status::text, \'\') <> 'DONE'")
    if ids:
        where.append("c.id = ANY(%s)")
        params.append(list(ids))
    if company:
        where.append("c.company ILIKE %s")
        params.append("%" + company + "%")
    sql = (
        'SELECT c.id, c."fullName", c."company", c."companyLocation", '
        'c.website, c.emails '
        'FROM "Contact" c '
        'LEFT JOIN enrichment e ON e.contact_id = c.id '
        'WHERE ' + " AND ".join(where) + ' '
        'ORDER BY c."createdAt" DESC NULLS LAST '
        'LIMIT %s'
    )
    params.append(limit)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            if rows and isinstance(rows[0], dict):
                return [dict(row) for row in rows]
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def _snapshot(row):
    return {
        "fullName": row.get("fullName"),
        "company": row.get("company"),
        "jobTitle": None,
        "emails": row.get("emails") or [],
        "website": row.get("website"),
        "linkedin": None,
        "otherSocials": [],
        "mobileNumbers": [],
        "telephoneNumbers": [],
        "address": None,
        "companyLocation": row.get("companyLocation"),
    }


def _payload(pass_name, row):
    tag = "address" if pass_name == "address" else "research"
    return {"tag": tag, "contactId": row["id"], "contact": _snapshot(row)}


def _publish(payloads):
    params = pika.URLParameters(config.RABBITMQ_URL)
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    ch.queue_declare(queue=config.QUEUE_PROFILE_COLLECTION, durable=True)
    for p in payloads:
        ch.basic_publish(exchange="", routing_key=config.QUEUE_PROFILE_COLLECTION,
                         body=json.dumps(p),
                         properties=pika.BasicProperties(delivery_mode=2))
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="pass_name", choices=("address", "socials", "all"),
                    default="address")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--inline", action="store_true")
    ap.add_argument("--ids", help="comma-separated contact IDs to run")
    ap.add_argument("--company", help="filter by company name fragment")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the temporary address page/geocode cache")
    args = ap.parse_args()

    if args.no_cache:
        os.environ["ADDRESS_CACHE"] = "0"

    ids = [item.strip() for item in (args.ids or "").split(",") if item.strip()]
    if ids and args.limit < len(ids):
        args.limit = len(ids)

    if args.pass_name == "address" and not db.address_schema_ok():
        print("address schema missing; apply the Prisma migration first")
        raise SystemExit(2)

    passes = ["address", "socials"] if args.pass_name == "all" else [args.pass_name]

    for p in passes:
        # An explicit id list means "rerun exactly these" — do not silently
        # skip contacts that already hit the attempt cap.
        only_missing = args.only_missing or (p == "address" and not ids)
        rows = _select(p, args.limit, only_missing, ids=ids,
                       company=args.company)
        print("pass=%s selected=%d" % (p, len(rows)))
        if args.dry_run:
            for r in rows:
                print("  %s | %s | %s" % (r["id"], r.get("company"), r.get("companyLocation")))
            if p == "address":
                print("  geocode budget remaining: %d" % budget.remaining())
            continue

        payloads = [_payload(p, r) for r in rows]
        if args.inline:
            for pl in payloads:
                print(pl["contactId"], handlers.process_payload(pl))
        else:
            _publish(payloads)
            print("published %d jobs to %s" % (len(payloads),
                  config.QUEUE_PROFILE_COLLECTION))


if __name__ == "__main__":
    main()
