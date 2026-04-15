# database.py
# SQLite 데이터베이스 초기화, 커넥션 관리, JSON 마이그레이션을 담당합니다.

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import ValidationError

logger = logging.getLogger("ranker.database")

DB_PATH: Path = Path(os.getenv("DATABASE_PATH", "./data/ranker.db"))

_connection: aiosqlite.Connection | None = None

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    settings TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS criteria (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT 'gray',
    weight REAL NOT NULL DEFAULT 1.0,
    battles INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, key)
);

CREATE TABLE IF NOT EXISTS items (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id INTEGER NOT NULL,
    name TEXT NOT NULL,
    matches_played INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, id)
);

CREATE TABLE IF NOT EXISTS item_ratings (
    session_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    criterion_key TEXT NOT NULL,
    mu REAL NOT NULL DEFAULT 0.0,
    sigma_sq REAL NOT NULL,
    criterion_matches INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, item_id, criterion_key),
    FOREIGN KEY (session_id, item_id) REFERENCES items(session_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS active_rounds (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    item1_id INTEGER NOT NULL,
    item2_id INTEGER NOT NULL,
    item3_id INTEGER,
    issued_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_session ON items(session_id);
CREATE INDEX IF NOT EXISTS idx_item_ratings_session_item ON item_ratings(session_id, item_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last_accessed ON sessions(last_accessed);
"""


async def init_db() -> None:
    """DB 커넥션을 열고 스키마를 초기화합니다."""
    global _connection
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _connection = await aiosqlite.connect(DB_PATH)
    _connection.row_factory = aiosqlite.Row
    await _connection.executescript(_SCHEMA_SQL)
    await _connection.commit()


def get_db() -> aiosqlite.Connection:
    """싱글턴 커넥션을 반환합니다. init_db() 호출 전이면 RuntimeError."""
    if _connection is None:
        raise RuntimeError("DB가 초기화되지 않았습니다. init_db()를 먼저 호출하세요.")
    return _connection


async def close_db() -> None:
    """커넥션을 닫습니다."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


async def migrate_json_sessions(session_dir: Path) -> int:
    """기존 JSON 세션 파일을 SQLite로 마이그레이션합니다.

    성공한 파일은 session_dir/migrated/로 이동하며, 실패한 파일은 원본 위치에 보존됩니다.
    """
    from store import _normalize_loaded_data, InvalidSessionDataError
    from schemas import SessionDataModel

    json_files = list(session_dir.glob("*.json"))
    if not json_files:
        return 0

    db = get_db()
    migrated_dir = session_dir / "migrated"
    migrated_dir.mkdir(exist_ok=True)
    migrated_count = 0

    for json_path in json_files:
        session_id = json_path.stem
        try:
            raw = json_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            normalized = _normalize_loaded_data(parsed)
            validated = SessionDataModel.model_validate(normalized)
            data = validated.model_dump(mode="python")
        except (
            json.JSONDecodeError,
            ValidationError,
            InvalidSessionDataError,
            OSError,
        ) as exc:
            logger.warning("migration_skip — session_id=%s: %s", session_id, exc)
            continue

        now = time.time()
        try:
            file_mtime = json_path.stat().st_mtime
        except OSError:
            file_mtime = now

        try:
            await _insert_session_data(db, session_id, data, created_at=file_mtime, last_accessed=now)
            shutil.move(str(json_path), str(migrated_dir / json_path.name))
            migrated_count += 1
            logger.info("migrated_session — session_id=%s", session_id)
        except Exception:
            logger.exception("migration_failed — session_id=%s", session_id)

    return migrated_count


async def _insert_session_data(
    db: aiosqlite.Connection,
    session_id: str,
    data: dict[str, Any],
    *,
    created_at: float,
    last_accessed: float,
) -> None:
    """세션 데이터를 DB에 삽입합니다 (단일 트랜잭션)."""
    settings_json = json.dumps(data["settings"], ensure_ascii=False)

    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "INSERT OR REPLACE INTO sessions (id, settings, created_at, last_accessed) VALUES (?, ?, ?, ?)",
            (session_id, settings_json, created_at, last_accessed),
        )

        await db.execute("DELETE FROM criteria WHERE session_id = ?", (session_id,))
        await db.executemany(
            "INSERT INTO criteria (session_id, key, label, color, weight, battles, draws, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (session_id, c["key"], c["label"], c["color"], c["weight"], c.get("battles", 0), c.get("draws", 0), i)
                for i, c in enumerate(data["criteria"])
            ],
        )

        await db.execute("DELETE FROM items WHERE session_id = ?", (session_id,))
        await db.executemany(
            "INSERT INTO items (session_id, id, name, matches_played) VALUES (?, ?, ?, ?)",
            [(session_id, item["id"], item["name"], item["matches_played"]) for item in data["items"]],
        )

        ratings_rows: list[tuple[str, int, str, float, float, int]] = []
        for item in data["items"]:
            for key in item["mu"]:
                ratings_rows.append((
                    session_id,
                    item["id"],
                    key,
                    item["mu"][key],
                    item["sigma_sq"][key],
                    item.get("criterion_matches", {}).get(key, 0),
                ))
        if ratings_rows:
            await db.executemany(
                "INSERT INTO item_ratings (session_id, item_id, criterion_key, mu, sigma_sq, criterion_matches) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ratings_rows,
            )

        await db.execute("DELETE FROM active_rounds WHERE session_id = ?", (session_id,))
        ar = data.get("active_round")
        if ar:
            await db.execute(
                "INSERT INTO active_rounds (session_id, token, item1_id, item2_id, item3_id, issued_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, ar["token"], ar["item1_id"], ar["item2_id"], ar.get("item3_id"), ar["issued_at"]),
            )

        await db.commit()
    except Exception:
        await db.rollback()
        raise
