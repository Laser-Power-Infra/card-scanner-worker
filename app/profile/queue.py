"""RabbitMQ receiver for the profile-collection queue.

Consumes 'CS-profile:collection' — the queue the Next.js publisher sends every
tagged task to. Each message carries a 'tag' in its payload; the receiver
reads it and hands the task to the right worker via handlers.dispatch()
(workers are assigned based on tags).

No retry/DLX: a message is acked on success (or when it can't be persisted or
assigned) and dropped with an error logged if a handler throws. The queue is
durable and messages are persistent, so nothing is lost while the receiver is
down; a reconnect loop keeps the consumer alive across connection drops.
"""
import json
import logging
import time

import pika

from . import config, handlers

log = logging.getLogger("card-desk.queue")

QUEUE = config.QUEUE_PROFILE_COLLECTION
RECONNECT_SLEEP = 5


def _connect():
    params = pika.URLParameters(config.RABBITMQ_URL)
    params.heartbeat = 60
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    # Same declaration as the Next.js publisher's assertQueue(queue, {durable:true}).
    ch.queue_declare(queue=QUEUE, durable=True)
    ch.basic_qos(prefetch_count=1)
    return conn, ch


def _on_message(ch, method, properties, body):
    delivery_tag = method.delivery_tag
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        log.exception("Undecodable message, dropping")
        ch.basic_ack(delivery_tag)
        return

    tag = payload.get("tag")
    contact_id = payload.get("contactId")

    t0 = time.monotonic()
    outcome, value = handlers.process_payload(payload)
    elapsed = time.monotonic() - t0

    if outcome == "skipped":
        log.warning("Dropping %s task (task %s): %s",
                    tag, payload.get("taskId"), value)
        ch.basic_ack(delivery_tag)
        return
    if outcome == "error":
        log.warning("Dropping %s task %s (contact %s): %s",
                    tag, payload.get("taskId"), contact_id, value)
        try:
            ch.basic_nack(delivery_tag, requeue=False)
        except Exception:
            log.exception("basic_nack failed")
        return

    result = value if isinstance(value, dict) else {}
    log.info("<<< result: %s in %.2fs", result.get("site"), elapsed)
    log.info(">>> DB saved: %s", _summarize(result))
    ch.basic_ack(delivery_tag)


def _summarize(result):
    """One readable line of what the worker returned/wrote. Works for every
    worker type: URL fields printed in full, long text as <field>(<chars>)."""
    parts = []
    for k, v in (result or {}).items():
        if k in ("ok", "site", "reason"):
            continue
        if k == "api_ok":
            parts.append("api_ok=%s" % v)
        elif isinstance(v, str) and v.startswith("http"):
            parts.append("%s=%s" % (k, v))
        elif isinstance(v, str) and v:
            parts.append("%s(%dc)" % (k, len(v)))
        elif v:
            parts.append("%s=%s" % (k, v))
    return ", ".join(parts) or "(no fields)"


def _consume_loop():
    while True:
        conn = None
        try:
            conn, ch = _connect()
            log.info("Receiver connected to queue %s", QUEUE)
            ch.basic_consume(QUEUE, _on_message)
            ch.start_consuming()
        except Exception:
            log.exception("Receiver error; reconnecting in %ss", RECONNECT_SLEEP)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        time.sleep(RECONNECT_SLEEP)


def run_receiver():
    _consume_loop()
