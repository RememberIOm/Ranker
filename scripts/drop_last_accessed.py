"""Drop sessions.last_accessed and its index (2026-11-06).

python -m scripts.drop_last_accessed /data/ranker.db   # used by docker/entrypoint.sh

Rankings no longer expire, so nothing reads the last access time. A database
already without the column is left alone.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def convert(path: Path) -> bool:
    if not path.exists():
        return False
    with closing(sqlite3.connect(path, autocommit=False)) as db, db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if "last_accessed" not in columns:
            return False
        db.execute("DROP INDEX IF EXISTS idx_sessions_last_accessed")
        db.execute("ALTER TABLE sessions DROP COLUMN last_accessed")
    return True


if __name__ == "__main__":
    dropped = convert(Path(sys.argv[1]))
    print(f"drop_last_accessed: {'dropped' if dropped else 'nothing to do'}")
