"""Publish one durable match job for every company missing current extraction."""
import argparse
import json
import logging
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pika  # noqa: E402

from app.subsidiary import config  # noqa: E402
from app.subsidiary.db import get_contact_hints, has_extraction, pending_companies  # noqa: E402
from app.subsidiary.logic import extract_version  # noqa: E402
from app.subsidiary.handlers import EXTRACT_PROMPT_VERSION  # noqa: E402


def publish(payloads):
    params = pika.URLParameters(config.RABBITMQ_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=config.QUEUE, durable=True)
    for payload in payloads:
        channel.basic_publish(exchange="", routing_key=config.QUEUE,
                              body=json.dumps(payload),
                              properties=pika.BasicProperties(delivery_mode=2))
    channel.close()
    connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    version = extract_version(config.LLM_MODEL, EXTRACT_PROMPT_VERSION)
    unprocessed = pending_companies(version, rebuild=args.rebuild)
    companies = list(dict.fromkeys(unprocessed))
    logging.basicConfig(level=logging.INFO)
    logging.info("Found %d companies to enqueue", len(companies))
    payloads = []
    for company in companies:
        # Already-extracted companies only need a tag recompute; fresh ones
        # must search and extract. Determined per company, not from the list.
        aggregate_only = has_extraction(company)
        hint = get_contact_hints(company)
        domain_hints = [domain.lower() for domain in hint.get("domains", [])]
        payloads.append({"taskId": str(uuid4()), "tag": "match", "company": company,
                         "aggregate_only": aggregate_only,
                         "hint": {"state": hint.get("state", ""),
                                  "domains": list(dict.fromkeys(domain_hints))},
                         "timestamp": int(time.time() * 1000)})
    if args.dry_run:
        for payload in payloads[:20]:
            print(json.dumps(payload, ensure_ascii=False))
        print("dry-run: %d jobs" % len(payloads))
        return
    publish(payloads)
    print("Published %d jobs to %s" % (len(payloads), config.QUEUE))


if __name__ == "__main__":
    main()
