"""Publish or directly run a sample company match job."""
import argparse
import json
import logging
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pika  # noqa: E402

from app.subsidiary import config  # noqa: E402
from app.subsidiary import handlers  # noqa: E402
from app.subsidiary.db import header  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("company")
    parser.add_argument("--state", default="")
    parser.add_argument("--domain", action="append", default=[])
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    payload = {"taskId": str(uuid4()), "tag": "match", "company": args.company,
               "hint": {"state": args.state, "domains": args.domain}}
    if args.publish:
        connection = pika.BlockingConnection(pika.URLParameters(config.RABBITMQ_URL))
        channel = connection.channel()
        channel.queue_declare(queue=config.QUEUE, durable=True)
        channel.basic_publish("", config.QUEUE, json.dumps(payload),
                              pika.BasicProperties(delivery_mode=2))
        connection.close()
        print("Published sample job")
        return
    before = header(args.company)
    outcome, result = handlers.process_payload(payload, dry_run=args.dry_run)
    print(outcome)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    after = header(args.company)
    if before != after:
        print("CompanyKeywordScore header updated")


if __name__ == "__main__":
    main()
