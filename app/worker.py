import logging
import threading
import time
import uuid

from .config import get_settings
from .database import Database
from .pipeline import BookPipeline


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("book-tts.worker")


def run_forever() -> None:
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    pipeline = BookPipeline(database, settings)
    logger.info("worker started")
    while True:
        token = uuid.uuid4().hex
        book = database.claim_book(token, settings.worker_lease_seconds)
        if book is not None:
            book_id = book["id"]
            logger.info(
                "processing local parse book=%s status=%s",
                book_id,
                book["status"],
            )
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=_renew_lease,
                args=(database, book_id, token, settings.worker_lease_seconds, heartbeat_stop),
                name=f"lease-book-{book_id[:8]}",
                daemon=True,
            )
            heartbeat.start()
            try:
                pipeline.run(book_id)
            except Exception:
                logger.exception("local parse failed book=%s", book_id)
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=2)
                database.release_book(book_id, token)
            continue

        execution = database.claim_execution(token, settings.worker_lease_seconds)
        if execution is None:
            time.sleep(settings.poll_interval_seconds)
            continue
        execution_id = execution["id"]
        logger.info(
            "processing execution=%s book=%s range=%s-%s",
            execution_id,
            execution["book_id"],
            execution["resolved_start_sequence"] + 1,
            execution["resolved_end_sequence"] + 1,
        )
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=_renew_execution_lease,
            args=(database, execution_id, token, settings.worker_lease_seconds, heartbeat_stop),
            name=f"lease-execution-{execution_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            pipeline.run_execution(execution_id)
        except Exception:
            logger.exception("execution failed execution=%s", execution_id)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            database.release_execution(execution_id, token)


def _renew_lease(database, book_id: str, token: str, lease_seconds: int, stop: threading.Event) -> None:
    interval = max(1, lease_seconds // 3)
    while not stop.wait(interval):
        if not database.renew_book(book_id, token, lease_seconds):
            logger.error("book lease lost book=%s", book_id)
            return


def _renew_execution_lease(
    database, execution_id: str, token: str, lease_seconds: int, stop: threading.Event
) -> None:
    interval = max(1, lease_seconds // 3)
    while not stop.wait(interval):
        if not database.renew_execution(execution_id, token, lease_seconds):
            logger.error("execution lease lost execution=%s", execution_id)
            return


if __name__ == "__main__":
    run_forever()
