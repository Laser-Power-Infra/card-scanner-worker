"""Run the subsidiary matching RabbitMQ consumer: python -m app.main."""
import logging

from . import queue

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main():
    queue.run_receiver()


if __name__ == "__main__":
    main()
