"""Convert the pre-2026-09-25 production database into the current schema.

uv run python -m scripts.convert_legacy_db OLD.db NEW.db
python -m scripts.convert_legacy_db /data/ranker.db   # in place, used by docker/entrypoint.sh

Only the most recently used ranking is kept, with its list and recovery code.
Its items, criteria and settings carry over; scores and match counts start from
zero because the old database holds no vote history to refit from. In place, a
current-schema DB is left alone and the old file is kept as
ranker.db.legacy-<timestamp>.
"""

import argparse
import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from ranker import database
from ranker.schemas import SettingsModel
from ranker.store import create_store


def _settings(raw: str) -> dict:
    known = SettingsModel.model_fields
    stored = {k: v for k, v in json.loads(raw).items() if k in known}
    try:
        return SettingsModel(**stored).model_dump(mode="python")
    except ValidationError:
        return SettingsModel().model_dump(mode="python")


def build_backup(db: sqlite3.Connection, session_id: str) -> dict:
    settings = db.execute(
        "SELECT settings FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()[0]
    criteria = [
        {"key": r[0], "label": r[1], "color": r[2], "weight": r[3]}
        for r in db.execute(
            "SELECT key, label, color, weight FROM criteria "
            "WHERE session_id = ? ORDER BY sort_order",
            (session_id,),
        )
    ]
    keys = [c["key"] for c in criteria]
    items = [
        {
            "id": item_id,
            "name": name,
            "mu": dict.fromkeys(keys, 0.0),
            "sigma_sq": dict.fromkeys(keys, 1.0),
            "matches_played": 0,
            "criterion_matches": dict.fromkeys(keys, 0),
        }
        for item_id, name in db.execute(
            "SELECT id, name FROM items WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
    ]
    return {
        "schema_version": 4,
        "settings": _settings(settings),
        "criteria": criteria,
        "items": items,
        "next_item_id": max((i["id"] for i in items), default=0) + 1,
        "observations": {},
        "exposures": {},
        "history": [],
    }


async def convert(old_path: Path, new_path: Path) -> None:
    if new_path.exists():
        raise SystemExit(f"{new_path}가 이미 있습니다. 덮어쓰지 않습니다.")
    with closing(sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)) as old:
        latest = old.execute(
            "SELECT id, created_at, last_accessed FROM sessions "
            "ORDER BY last_accessed DESC, created_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise SystemExit("변환할 랭킹이 없습니다.")
        session_id, created_at, last_accessed = latest
        backup = build_backup(old, session_id)
        board = old.execute(
            "SELECT b.library_id, b.name, l.created_at FROM boards b "
            "JOIN libraries l ON l.id = b.library_id WHERE b.session_id = ?",
            (session_id,),
        ).fetchone()
        total = old.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    database.DB_PATH = new_path
    await database.init_db()
    try:
        store = await create_store(session_id)
        await store.import_json(json.dumps(backup, ensure_ascii=False))
        async with database.transaction() as db:
            await db.execute(
                "UPDATE sessions SET created_at = ?, last_accessed = ? WHERE id = ?",
                (created_at, last_accessed, session_id),
            )
            if board is not None:
                library_id, name, library_created_at = board
                await db.execute(
                    "INSERT INTO libraries (id, created_at) VALUES (?, ?)",
                    (library_id, library_created_at),
                )
                await db.execute(
                    "INSERT INTO boards (session_id, library_id, name) VALUES (?, ?, ?)",
                    (session_id, library_id, name),
                )
    finally:
        await database.close_db()
    print(
        f"랭킹 {session_id}{f' ({board[1]})' if board else ''}: "
        f"항목 {len(backup['items'])}, 기준 {len(backup['criteria'])}. "
        f"나머지 랭킹 {total - 1}개는 옮기지 않았습니다."
    )


def is_legacy(path: Path) -> bool:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
    return bool(columns) and "next_item_id" not in columns


def convert_in_place(path: Path) -> None:
    if not path.exists() or not is_legacy(path):
        return
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    new_path = path.with_name(path.name + ".new")
    new_path.unlink(missing_ok=True)
    asyncio.run(convert(path, new_path))
    legacy = path.with_name(f"{path.name}.legacy-{int(time.time())}")
    path.rename(legacy)
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    new_path.rename(path)
    print(f"기존 DB는 {legacy}에 남겼습니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path, nargs="?")
    args = parser.parse_args()
    if args.new is None:
        convert_in_place(args.old)
    else:
        asyncio.run(convert(args.old, args.new))


if __name__ == "__main__":
    main()
