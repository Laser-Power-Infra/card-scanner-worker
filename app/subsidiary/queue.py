"""Durable RabbitMQ receiver; pauses and requeues jobs when search is exhausted."""
import json
import logging
import time

import pika

from . import config, handlers, search

log = logging.getLogger("subsidiary.queue")
RECONNECT_SLEEP = 5


def _connect():
    params = pika.URLParameters(config.RABBITMQ_URL)
    params.heartbeat = 60
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=config.QUEUE, durable=True)
    channel.basic_qos(prefetch_count=1)
    return connection, channel


def _on_message(channel, method, properties, body, pause_state=None):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.exception("Invalid message; acknowledging and dropping")
        channel.basic_ack(method.delivery_tag)
        return

    outcome, value = handlers.process_payload(payload)
    if outcome == "paused":
        channel.basic_nack(method.delivery_tag, requeue=True)
        log.warning("Search exhausted; requeued company and pausing consumer")
        if pause_state is not None:
            pause_state["requested"] = True
        channel.stop_consuming()
        return
    if outcome == "error":
        log.error("Company matching failed; dropping poison message: %s", value)
        channel.basic_nack(method.delivery_tag, requeue=False)
        return
    if outcome == "skipped":
        log.warning("Skipping malformed task: %s", value)
    else:
        log.info("Matched company %r: %s", value.get("company"), value.get("matches"))
    channel.basic_ack(method.delivery_tag)


def _consume_loop():
    while True:
        connection = None
        try:
            connection, channel = _connect()
            log.info("Connected to %s", config.QUEUE)
            pause_state = {"requested": False}
            def callback(ch, method, properties, body):
                _on_message(ch, method, properties, body, pause_state)

            channel.basic_consume(config.QUEUE, callback)
            channel.start_consuming()
            # stop_consuming returns here for the paused path.
            if pause_state["requested"]:
                log.warning("PAUSED: waiting for search cooldowns before resume")
                while True:
                    time.sleep(config.PAUSE_PROBE_INTERVAL)
                    if search.provider_available():
                        search.reset_rotation()
                        log.info("Search provider recovered; resuming queue")
                        break
        except KeyboardInterrupt:
            log.info("Worker shutdown requested")
            return
        except Exception:
            log.exception("Receiver error; reconnecting in %ss", RECONNECT_SLEEP)
            time.sleep(RECONNECT_SLEEP)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def run_receiver():
    _consume_loop()
