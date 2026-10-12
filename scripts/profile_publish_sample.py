"""Test producer / runner: publishes or runs profile-collection tasks.

Two modes:

  python -m scripts.publish_sample                # default: run workers directly
  python -m scripts.publish_sample --publish ...  # publish to RabbitMQ

Default run mode uses the SAMPLE_TASKS array below as input:
  - array NON-EMPTY: for each entry, run its real worker directly (no
    RabbitMQ), print the worker output and a diff of the fields it wrote.
  - array EMPTY: take one task from the CS-profile:collection queue, print it,
    then requeue it (peek — the real receiver still gets it).

NOTE: the enrichment row is keyed by contact_id and shared across runs. The
name/company in SAMPLE_TASKS is only search input — use a distinct, real
contact_id per test person, and merge-only means a run that finds nothing
keeps the previously found values.

--publish mode mirrors the Next.js publisher:
  python -m scripts.publish_sample --publish --all
  python -m scripts.publish_sample --publish --sample N
  python -m scripts.publish_sample --publish <tag> <contactId> [options]
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pika  # noqa: E402

from app.profile import ai as ai_mod  # noqa: E402
from app.profile import config  # noqa: E402
from app.profile import db as app_db  # noqa: E402
from app.profile import handlers  # noqa: E402

TAGS = ("linkedin", "whatsapp", "instagram", "facebook", "twitter", "research")

# ── Edit this array to run/publish sample tasks. When it is EMPTY, the default
# run mode peeks one task from the RabbitMQ queue instead. ────────────────
# test-satya-1 is a real Contact row (created in the DB) so the FK holds.
SAMPLE_TASKS = [
    {"tag": "research",
     "contact_id": "test-satya-1",
     "contact": {
         "fullName": "Utsab Kundu",
         "jobTitle": None,
         "company": "Struxo",
         "mobileNumbers": [],
         "telephoneNumbers": [],
         "emails": [],
         "website": None,
         "address": None,
         "companyLocation": None,
         "linkedin": None,
         "otherSocials": [],
         "rawNotes": None,
     }},
    {"tag": "linkedin",
     "contact_id": "test-satya-1",
     "contact": {
         "fullName": "Satya Nadella",
         "jobTitle": None,
         "company": "Microsoft",
         "mobileNumbers": [],
         "telephoneNumbers": [],
         "emails": [],
         "website": None,
         "address": None,
         "companyLocation": None,
         "linkedin": None,
         "otherSocials": [],
         "rawNotes": None,
     }},
    {"tag": "facebook",
     "contact_id": "test-satya-1",
     "contact": {
         "fullName": "Satya Nadella",
         "jobTitle": None,
         "company": "Microsoft",
         "mobileNumbers": [],
         "telephoneNumbers": [],
         "emails": [],
         "website": None,
         "address": None,
         "companyLocation": None,
         "linkedin": None,
         "otherSocials": [],
         "rawNotes": None,
     }},
    # {"tag": "twitter",
    #  "contact_id": "cmsis5zgm00010kuvrbq3vnoy",
    #  "contact": {
    #      "fullName": "Jonh Henry",
    #      "jobTitle": None,
    #      "company": "Acme",
    #      "mobileNumbers": [],
    #      "telephoneNumbers": [],
    #      "emails": [],
    #      "website": None,
    #      "address": None,
    #      "companyLocation": None,
    #      "linkedin": None,
    #      "otherSocials": [],
    #      "rawNotes": None,
    #  }},
    # {"tag": "instagram",
    #  "contact_id": "cmsis5zgm00010kuvrbq3vnoy",
    #  "contact": {
    #      "fullName": "Jonh Henry",
    #      "jobTitle": None,
    #      "company": "Acme",
    #      "mobileNumbers": [],
    #      "telephoneNumbers": [],
    #      "emails": [],
    #      "website": None,
    #      "address": None,
    #      "companyLocation": None,
    #      "linkedin": None,
    #      "otherSocials": [],
    #      "rawNotes": None,
    #  }},
]


# ── Helpers ────────────────────────────────────────────────────────────────
def payload_for(tag, contact_id, contact):
    return {
        "tag": tag,
        "taskId": str(uuid4()),
        "contactId": contact_id or "",
        "contact": contact,
        "timestamp": int(time.time() * 1000),
    }


def publish(payload):
    params = pika.URLParameters(config.RABBITMQ_URL)
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    ch.queue_declare(queue=config.QUEUE_PROFILE_COLLECTION, durable=True)
    sent = ch.basic_publish(
        exchange="",
        routing_key=config.QUEUE_PROFILE_COLLECTION,
        body=json.dumps(payload),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    ch.close()
    conn.close()
    return sent


def _row_snapshot(row):
    if not row:
        return {}
    return {k: v for k, v in row.items()
            if k not in ("created_at", "updated_at", "enriched_at")}


def _print_diff(before, after):
    """Print only the fields this run actually changed. The enrichment row is
    keyed by contact_id and shared across runs, so untouched columns are
    history from earlier runs, not this one."""
    before = _row_snapshot(before)
    after = _row_snapshot(after)
    changed = {}
    for k in after:
        if before.get(k) != after.get(k) and after[k] not in (None, "", 0):
            changed[k] = after[k]
    if not changed:
        print("  no fields written this run - row unchanged (previous values kept)")
        return
    print("  fields written this run:")
    for k, v in changed.items():
        print("    %s = %s" % (k, v))
    print("  (other columns are shared history from earlier runs)")


def run_task(task):
    tag = task["tag"]
    contact_id = task["contact_id"]
    contact = task["contact"]
    worker = handlers.TAG_WORKERS.get(tag)

    print("=" * 64)
    print("=== tag=%s contact=%s -> worker=%s ==="
          % (tag, contact_id or "-", worker or "?"))

    # Same processing the RabbitMQ receiver uses, so run mode behaves exactly
    # like a task arriving on the queue.
    payload = payload_for(tag, contact_id, contact)
    before = app_db.get_enrichment(contact_id) if contact_id else None
    t0 = time.monotonic()
    outcome, value = handlers.process_payload(payload)
    elapsed = time.monotonic() - t0
    after = app_db.get_enrichment(contact_id) if contact_id else None

    if outcome == "skipped":
        print("  skipped: %s" % value)
        return
    if outcome == "error":
        print("  error: %s" % value)
        return

    print("  output:")
    print(json.dumps(value, indent=2))
    print("  took %.2fs" % elapsed)
    if contact_id:
        _print_diff(before, after)


def run_ai(name, company):
    """Standalone OpenAI web_search research - no DB, no queue. Prints the
    full details the 'research' worker would store."""
    print("=" * 64)
    print("=== AI research: %s @ %s ===" % (name or "-", company or "-"))
    t0 = time.monotonic()
    data, api_ok = ai_mod.enrich_person(name, "", company)
    print("  api_ok: %s" % api_ok)
    print("  took %.2fs" % (time.monotonic() - t0))
    print("  output:")
    print(json.dumps(data, indent=2))


def peek_queue():
    """Empty-array fallback: take one queued task, print it, requeue it."""
    print("SAMPLE_TASKS is empty - peeking one task from the queue...")
    params = pika.URLParameters(config.RABBITMQ_URL)
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    ch.queue_declare(queue=config.QUEUE_PROFILE_COLLECTION, durable=True)
    method, props, body = ch.basic_get(config.QUEUE_PROFILE_COLLECTION,
                                       auto_ack=False)
    if not method:
        print("queue empty")
        ch.close()
        conn.close()
        return

    try:
        payload = json.loads(body.decode("utf-8"))
        print("task (contactId=%s, tag=%s):" % (payload.get("contactId"), payload.get("tag")))
        print(json.dumps(payload, indent=2))
    except Exception:
        print("undecodable body: %r" % body[:500])

    # Peek only — keep the task for the real receiver.
    ch.basic_nack(method.delivery_tag, requeue=True)
    ch.close()
    conn.close()
    print("(requeued - still waiting for the receiver)")


# ── CLI ────────────────────────────────────────────────────────────────────
def main():
    # Mirror the queue receiver's console logging (the "Assigning tag ->
    # worker" lines come from handlers.dispatch via logging).
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", choices=TAGS)
    ap.add_argument("contact_id", nargs="?", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--company", default="")
    ap.add_argument("--mobile", default="9876543210")
    ap.add_argument("--no-contact-id", action="store_true")
    ap.add_argument("--ai", action="store_true",
                    help="run the OpenAI web_search research directly (no DB/queue)")
    ap.add_argument("--publish", action="store_true",
                    help="publish to RabbitMQ instead of running workers")
    ap.add_argument("--sample", type=int, default=None,
                    help="SAMPLE_TASKS index (0-based)")
    ap.add_argument("--all", action="store_true",
                    help="publish/run every SAMPLE_TASKS entry")
    args = ap.parse_args()

    # ── Standalone AI research (no DB, no queue) ───────────────────────────
    if args.ai:
        run_ai(args.name, args.company)
        return

    # ── PUBLISH mode ───────────────────────────────────────────────────────
    if args.publish:
        to_publish = []
        if args.tag:
            contact = {
                "fullName": args.name,
                "jobTitle": None,
                "company": args.company,
                "mobileNumbers": [args.mobile],
                "telephoneNumbers": [],
                "emails": [],
                "website": None,
                "address": None,
                "companyLocation": None,
                "linkedin": None,
                "otherSocials": [],
                "rawNotes": None,
            }
            to_publish = [payload_for(args.tag,
                                      "" if args.no_contact_id else args.contact_id,
                                      contact)]
        elif args.sample is not None:
            if not 0 <= args.sample < len(SAMPLE_TASKS):
                print("--sample out of range (0..%d)" % (len(SAMPLE_TASKS) - 1))
                return
            t = SAMPLE_TASKS[args.sample]
            to_publish = [payload_for(t["tag"], t["contact_id"], t["contact"])]
        else:
            to_publish = [payload_for(t["tag"], t["contact_id"], t["contact"])
                          for t in SAMPLE_TASKS]

        for payload in to_publish:
            publish(payload)
            label = payload["contactId"] or "(no contactId)"
            print("published %s task -> %s (contact=%s)"
                  % (payload["tag"], config.QUEUE_PROFILE_COLLECTION, label))
        return

    # ── RUN mode (default) ────────────────────────────────────────────────
    if args.sample is not None:
        if not 0 <= args.sample < len(SAMPLE_TASKS):
            print("--sample out of range (0..%d)" % (len(SAMPLE_TASKS) - 1))
            return
        run_task(SAMPLE_TASKS[args.sample])
    elif SAMPLE_TASKS:
        for task in SAMPLE_TASKS:
            run_task(task)
    else:
        peek_queue()


if __name__ == "__main__":
    main()
