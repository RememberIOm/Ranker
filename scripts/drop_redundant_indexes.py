"""Drop redundant prefix indexes already covered by composite PRIMARY KEYs.

python -m scripts.drop_redundant_indexes /data/ranker.db   # used by docker/entrypoint.sh

Both items(session_id) and item_ratings(session_id, item_id) are leading
prefixes of their tables' PRIMARY KEYs. A database already without them is
left alone.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

_REDUNDANT_INDEXES = ("idx_items_session", "idx_item_ratings_session_item")


def convert(path: Path) -> list[str]:
    if not path.exists():
        return []
    with closing(sqlite3.connect(path, autocommit=False)) as db, db:
        existing = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        to_drop = [name for name in _REDUNDANT_INDEXES if name in existing]
        for name in to_drop:
            db.execute(f"DROP INDEX IF EXISTS {name}")
    return to_drop


if __name__ == "__main__":
    dropped = convert(Path(sys.argv[1]))
    summary = ", ".join(dropped) if dropped else "nothing to do"
    print(f"drop_redundant_indexes: {summary}")
