import time

from app.db.connection import get_pool


def main() -> None:
    get_pool()
    while True:
        # Delivery + retry/backoff to be implemented by the callback owner.
        time.sleep(1.0)


if __name__ == "__main__":
    main()
