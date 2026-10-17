"""Initialize the QuotTracker schema without creating or modifying records."""

import db


def run() -> None:
    db.init_db()
    print("QuotTracker database schema is ready. No records were created.")


if __name__ == "__main__":
    run()
