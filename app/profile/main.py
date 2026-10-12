"""Card-desk worker entrypoint.

    python -m app.main              # run the profile-collection receiver

The receiver consumes the CS-profile:collection queue and assigns each task
to the right worker based on its tag.
"""
import logging

from . import queue

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("card-desk.main")


def main():
    log.info("Starting card-desk receiver on queue %s", queue.QUEUE)
    queue.run_receiver()


if __name__ == "__main__":
    main()
