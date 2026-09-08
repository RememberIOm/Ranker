# database.py
# SQLite 데이터베이스 초기화, 커넥션 관리를 담당합니다.

import asyncio
import json
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite


DB_PATH: Path = Path(os.getenv("DATABASE_PATH", "./data/ranker.db"))

_connection: aiosqlite.Connection | None = None

# 단일 커넥션을 모든 코루틴이 공유하므로, 멀티 스테이트먼트 트랜잭션 중간에
# 다른 코루틴의 execute/commit이 끼어들면 트랜잭션이 뒤섞입니다.
# 모든 DB 쓰기는 이 락을 잡고 수행해야 합니다.
db_write_lock = asyncio.Lock()

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    settings TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS libraries (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS boards (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES libraries(id),
    name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_boards_library ON boards(library_id);

CREATE TABLE IF NOT EXISTS vote_events (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id INTEGER NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);
CREATE TABLE IF NOT EXISTS ranking_models (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_session ON items(session_id);
CREATE INDEX IF NOT EXISTS idx_item_ratings_session_item ON item_ratings(session_id, item_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last_accessed ON sessions(last_accessed);
"""


async def init_db() -> None:
    """DB 커넥션을 열고 스키마를 초기화합니다."""
    global _connection, db_write_lock
    db_write_lock = asyncio.Lock()
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


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    """공유 커넥션의 쓰기를 직렬화하고 취소 시에도 롤백합니다."""
    async with db_write_lock:
        db = get_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            yield db
            await db.commit()
        except BaseException:
            # 취소된 요청이 다음 요청에 열린 트랜잭션을 넘기지 않습니다.
            await asyncio.shield(db.rollback())
            raise


class StaleSessionError(RuntimeError):
    """로드 이후 변경되거나 삭제된 세션에 대한 저장입니다."""


async def save_session_data(
    session_id: str,
    data: dict[str, Any],
    *,
    created_at: float,
    last_accessed: float,
    expected_revision: int | None = None,
    history: list[dict[str, Any]] | None = None,
) -> int:
    """변경 행만 갱신하고 데이터와 투표 이력을 함께 확정합니다."""
    async with transaction() as db:
        async with db.execute(
            "SELECT revision FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            existing = await cursor.fetchone()
        revision = existing["revision"] if existing else None
        if revision != expected_revision:
            raise StaleSessionError(
                "세션이 변경되거나 삭제되었습니다. 새로고침해주세요."
            )
        new_revision = (revision or 0) + 1
        await db.execute(
            "INSERT INTO sessions (id, settings, created_at, last_accessed, revision) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET settings=excluded.settings, last_accessed=excluded.last_accessed, revision=excluded.revision",
            (
                session_id,
                json.dumps(data["settings"], ensure_ascii=False),
                created_at,
                last_accessed,
                new_revision,
            ),
        )
        tables = {
            "criteria": (
                ["key", "label", "color", "weight", "battles", "draws", "sort_order"],
                [
                    (
                        c["key"],
                        c["label"],
                        c["color"],
                        c["weight"],
                        c.get("battles", 0),
                        c.get("draws", 0),
                        i,
                    )
                    for i, c in enumerate(data["criteria"])
                ],
                1,
            ),
            "items": (
                ["id", "name", "matches_played"],
                [
                    (item["id"], item["name"], item["matches_played"])
                    for item in data["items"]
                ],
                1,
            ),
            "item_ratings": (
                ["item_id", "criterion_key", "mu", "sigma_sq", "criterion_matches"],
                [
                    (
                        item["id"],
                        key,
                        value,
                        item["sigma_sq"][key],
                        item["criterion_matches"].get(key, 0),
                    )
                    for item in data["items"]
                    for key, value in item["mu"].items()
                ],
                2,
            ),
        }
        for table, (columns, rows, key_count) in tables.items():
            async with db.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE session_id = ?",
                (session_id,),
            ) as cursor:
                previous = {
                    tuple(row[:key_count]): tuple(row)
                    for row in await cursor.fetchall()
                }
            current = {tuple(row[:key_count]): tuple(row) for row in rows}
            removed = previous.keys() - current.keys()
            if removed:
                predicate = " AND ".join(f"{key} = ?" for key in columns[:key_count])
                await db.executemany(
                    f"DELETE FROM {table} WHERE session_id = ? AND {predicate}",
                    [(session_id, *key) for key in removed],
                )
            changed = [
                (session_id, *row)
                for key, row in current.items()
                if previous.get(key) != row
            ]
            if changed:
                names = ["session_id", *columns]
                conflict = ", ".join(["session_id", *columns[:key_count]])
                updates = ", ".join(f"{c}=excluded.{c}" for c in columns[key_count:])
                await db.executemany(
                    f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)}) ON CONFLICT({conflict}) DO UPDATE SET {updates}",
                    changed,
                )
        ar = data.get("active_round")
        if ar:
            await db.execute(
                "INSERT INTO active_rounds (session_id, token, item1_id, item2_id, item3_id, issued_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET token=excluded.token, item1_id=excluded.item1_id, item2_id=excluded.item2_id, item3_id=excluded.item3_id, issued_at=excluded.issued_at",
                (
                    session_id,
                    ar["token"],
                    ar["item1_id"],
                    ar["item2_id"],
                    ar.get("item3_id"),
                    ar["issued_at"],
                ),
            )
        else:
            await db.execute(
                "DELETE FROM active_rounds WHERE session_id = ?", (session_id,)
            )
        if history is not None:
            encoded = {
                event["id"]: json.dumps(event, ensure_ascii=False, allow_nan=False)
                for event in history
            }
            async with db.execute(
                "SELECT id, event FROM vote_events WHERE session_id = ?", (session_id,)
            ) as cursor:
                previous_events = {
                    row["id"]: row["event"] for row in await cursor.fetchall()
                }
            await db.executemany(
                "DELETE FROM vote_events WHERE session_id = ? AND id = ?",
                [(session_id, key) for key in previous_events.keys() - encoded.keys()],
            )
            await db.executemany(
                "INSERT INTO vote_events (session_id, id, event) VALUES (?, ?, ?) ON CONFLICT(session_id,id) DO UPDATE SET event=excluded.event",
                [
                    (session_id, key, value)
                    for key, value in encoded.items()
                    if previous_events.get(key) != value
                ],
            )
        model = {
            key: data.get(key, {})
            for key in ("observations", "exposures", "posteriors")
        }
        if model:
            await db.execute(
                "INSERT INTO ranking_models (session_id, state) VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET state=excluded.state WHERE state != excluded.state",
                (session_id, json.dumps(model, ensure_ascii=False, allow_nan=False)),
            )
        return new_revision
