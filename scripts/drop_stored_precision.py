"""Remove the stored precision matrices from the posterior cache (2026-10-21).

python -m scripts.drop_stored_precision /data/ranker.db   # used by docker/entrypoint.sh

The app now rebuilds each precision from the observations and the saved
location. Rows already in the new form are left alone.
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def convert(path: Path) -> int:
    if not path.exists():
        return 0
    changed = 0
    with closing(sqlite3.connect(path)) as db, db:
        for session_id, raw in db.execute(
            "SELECT session_id, state FROM ranking_models"
        ).fetchall():
            state = json.loads(raw)
            for posterior in state["posteriors"].values():
                for key in ("rows", "cols", "values"):
                    posterior.pop(key, None)
            converted = json.dumps(state, ensure_ascii=False, allow_nan=False)
            if converted != raw:
                db.execute(
                    "UPDATE ranking_models SET state = ? WHERE session_id = ?",
                    (converted, session_id),
                )
                changed += 1
    return changed


if __name__ == "__main__":
    print(f"drop_stored_precision: {convert(Path(sys.argv[1]))} rankings converted")
