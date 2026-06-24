import time

from app.config import get_settings
from app.db.connection import get_pool


def main() -> None:
    settings = get_settings()
    get_pool()
    interval = settings.worker_loop_interval_ms / 1000.0
    while True:
        # Matching logic to be implemented by the worker owner.
        time.sleep(interval)


if __name__ == "__main__":
    main()
