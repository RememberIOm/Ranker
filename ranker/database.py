"""SQLite 스키마, 공유 커넥션과 트랜잭션, 랭킹 상태와 투표 기록의 저장."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from ranker.schemas import MAX_BACKUP_BYTES, MAX_HISTORY_EVENTS, vote_ids

DB_PATH: Path = Path(os.getenv("DATABASE_PATH", "./data/ranker.db"))

_connection: aiosqlite.Connection | None = None

# 단일 커넥션을 모든 코루틴이 공유하므로, 멀티 스테이트먼트 트랜잭션 중간에
# 다른 코루틴의 execute/commit이 끼어들면 트랜잭션이 뒤섞입니다.
# 모든 DB 접근은 이 락을 잡고 수행해야 합니다.
_db_lock = asyncio.Lock()

# 세션별 (revision, vote_events 건수, event JSON 총 바이트) 캐시.
# 저장마다 vote_events 전 행을 스캔하지 않고 증분 갱신합니다.
_EVENT_STATS_MAX = 2048
_event_stats: dict[str, tuple[int, int, int]] = {}

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    settings TEXT NOT NULL,
    created_at REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    next_item_id INTEGER NOT NULL DEFAULT 1
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

-- event holds the immutable ballot JSON; status flags change in place.
CREATE TABLE IF NOT EXISTS vote_events (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    mode TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    event TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);
CREATE TABLE IF NOT EXISTS ranking_models (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_imports (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    raw TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class StaleSessionError(RuntimeError):
    """로드 이후 변경되거나 삭제된 세션에 대한 저장입니다."""


class SessionSaveError(RuntimeError):
    """세션 저장에 실패했을 때 발생합니다 (디스크 풀, 권한 거부 등)."""


class BackupLimitError(SessionSaveError):
    """저장하면 백업 파일로 다시 가져올 수 없는 크기가 됩니다."""


async def init_db() -> None:
    """DB 커넥션을 열고 스키마를 초기화합니다."""
    global _connection, _db_lock
    _db_lock = asyncio.Lock()
    _event_stats.clear()
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
    _event_stats.clear()
    if _connection is not None:
        await _connection.close()
        _connection = None


def clear_event_stats(session_id: str) -> None:
    _event_stats.pop(session_id, None)


def _remember_event_stats(
    session_id: str, revision: int, count: int, event_bytes: int
) -> None:
    if len(_event_stats) >= _EVENT_STATS_MAX and session_id not in _event_stats:
        _event_stats.pop(next(iter(_event_stats)))
    _event_stats[session_id] = (revision, count, event_bytes)


@asynccontextmanager
async def transaction(*, write: bool = True) -> AsyncIterator[aiosqlite.Connection]:
    """공유 커넥션 접근을 직렬화하고 취소 시에도 롤백합니다."""
    async with _db_lock:
        db = get_db()
        try:
            await db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            await db.commit()
        except BaseException:
            # 취소된 요청이 다음 요청에 열린 트랜잭션을 넘기지 않습니다.
            await asyncio.shield(db.rollback())
            raise


def encode_event(event: dict[str, Any]) -> tuple:
    """Split an exported event into status columns and the immutable ballot."""
    body = {key: event[key] for key in ("payload", "names", "labels")}
    return (
        event["id"],
        event["created_at"],
        event["mode"],
        int(event["undone"]),
        int(event["archived"]),
        json.dumps(body, ensure_ascii=False, allow_nan=False, sort_keys=True),
    )


def decode_event(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "mode": row["mode"],
        "undone": bool(row["undone"]),
        "archived": bool(row["archived"]),
        **json.loads(row["event"]),
    }


_EVENT_COLUMNS = "id, created_at, mode, undone, archived, event"


@dataclass
class EventChanges:
    """Vote history changes committed together with the session state."""

    insert: dict[str, Any] | None = None  # new event, id assigned on insert
    undo: int | None = None
    archive_all: bool = False
    replace: list[dict[str, Any]] | None = None

    def __bool__(self) -> bool:
        return bool(
            self.insert or self.undo or self.archive_all or self.replace is not None
        )


async def _apply_event_changes(
    db: aiosqlite.Connection, session_id: str, changes: EventChanges
) -> tuple[bool, int, int]:
    """투표 내역 변경을 적용하고 (전체 교체 여부, 건수/증분, 바이트/증분)을 반환합니다."""
    replaced = False
    delta_count = 0
    delta_bytes = 0
    if changes.replace is not None:
        replaced = True
        encoded = [(session_id, *encode_event(event)) for event in changes.replace]
        await db.execute("DELETE FROM vote_events WHERE session_id = ?", (session_id,))
        await db.executemany(
            f"INSERT INTO vote_events (session_id, {_EVENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            encoded,
        )
        delta_count = len(encoded)
        delta_bytes = sum(len(row[-1].encode()) for row in encoded)
    if changes.archive_all:
        await db.execute(
            "UPDATE vote_events SET archived = 1 WHERE session_id = ?", (session_id,)
        )
    if changes.undo is not None:
        await db.execute(
            "UPDATE vote_events SET undone = 1 WHERE session_id = ? AND id = ?",
            (session_id, changes.undo),
        )
    if changes.insert is not None:
        async with db.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM vote_events WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            (event_id,) = await cursor.fetchone()
        _, *columns = encode_event({**changes.insert, "id": event_id})
        await db.execute(
            f"INSERT INTO vote_events (session_id, {_EVENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, event_id, *columns),
        )
        delta_count += 1
        delta_bytes += len(columns[-1].encode())
    return replaced, delta_count, delta_bytes


async def _resolve_event_stats(
    db: aiosqlite.Connection,
    session_id: str,
    previous_revision: int | None,
    *,
    replaced: bool,
    delta_count: int,
    delta_bytes: int,
) -> tuple[int, int]:
    if replaced:
        return delta_count, delta_bytes
    if previous_revision is None:
        return delta_count, delta_bytes
    cached = _event_stats.get(session_id)
    if cached is not None and cached[0] == previous_revision:
        return cached[1] + delta_count, cached[2] + delta_bytes
    async with db.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(event AS BLOB))), 0) "
        "FROM vote_events WHERE session_id = ?",
        (session_id,),
    ) as cursor:
        count, event_bytes = await cursor.fetchone()
    return int(count), int(event_bytes)


def _check_backup_size(count: int, event_bytes: int, core_bytes: int) -> None:
    """Keep every saved state importable: the backup limits bound the export."""
    # Each exported event adds id, time and status fields to the stored ballot.
    if (
        count > MAX_HISTORY_EVENTS
        or core_bytes + event_bytes + 120 * count > MAX_BACKUP_BYTES
    ):
        raise BackupLimitError(
            "저장 한도(64MB 또는 투표 내역 10만 건)가 찼습니다. "
            "백업을 받은 뒤 내역을 비우세요."
        )


async def save_session_data(
    session_id: str,
    data: dict[str, Any],
    *,
    created_at: float,
    expected_revision: int | None,
    events: EventChanges,
    core_bytes: int,
    board: tuple[str, str] | None = None,
) -> int:
    """변경 행만 갱신하고 데이터와 투표 이력 변경을 함께 확정합니다.

    새 랭킹은 board(목록 ID, 이름)로 같은 트랜잭션에서 목록에 넣어,
    목록에 없는 랭킹이 생기지 않게 합니다.
    """
    async with transaction() as db:
        async with db.execute(
            "SELECT revision FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            existing = await cursor.fetchone()
        revision = existing["revision"] if existing else None
        if revision != expected_revision:
            raise StaleSessionError(
                "다른 탭이나 기기에서 랭킹이 바뀌었거나 삭제되었습니다. 새로고침하세요."
            )
        new_revision = (revision or 0) + 1
        await db.execute(
            "INSERT INTO sessions (id, settings, created_at, revision, next_item_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET settings=excluded.settings, "
            "revision=excluded.revision, next_item_id=excluded.next_item_id",
            (
                session_id,
                json.dumps(data["settings"], ensure_ascii=False),
                created_at,
                new_revision,
                data["next_item_id"],
            ),
        )
        if board is not None:
            library, name = board
            await db.execute(
                "INSERT INTO libraries(id, created_at) VALUES (?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (library, created_at),
            )
            await db.execute(
                "INSERT INTO boards(session_id, library_id, name) VALUES (?, ?, ?)",
                (session_id, library, name),
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
                        c["battles"],
                        c["draws"],
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
                        item["criterion_matches"][key],
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
        await _write_active_round(db, session_id, data.get("active_round"))
        replaced, delta_count, delta_bytes = await _apply_event_changes(
            db, session_id, events
        )
        model = {key: data[key] for key in ("observations", "exposures", "posteriors")}
        await db.execute(
            "INSERT INTO ranking_models (session_id, state) VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET state=excluded.state WHERE state != excluded.state",
            (session_id, json.dumps(model, ensure_ascii=False, allow_nan=False)),
        )
        # Any saved change makes a staged import preview out of date.
        await db.execute(
            "DELETE FROM pending_imports WHERE session_id = ?", (session_id,)
        )
        count, event_bytes = await _resolve_event_stats(
            db,
            session_id,
            revision,
            replaced=replaced,
            delta_count=delta_count,
            delta_bytes=delta_bytes,
        )
        _check_backup_size(count, event_bytes, core_bytes)
    _remember_event_stats(session_id, new_revision, count, event_bytes)
    return new_revision


async def _write_active_round(
    db: aiosqlite.Connection, session_id: str, active_round: dict[str, Any] | None
) -> None:
    if not active_round:
        await db.execute(
            "DELETE FROM active_rounds WHERE session_id = ?", (session_id,)
        )
        return
    await db.execute(
        "INSERT INTO active_rounds (session_id, token, item1_id, item2_id, item3_id, issued_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET token=excluded.token, item1_id=excluded.item1_id, item2_id=excluded.item2_id, item3_id=excluded.item3_id, issued_at=excluded.issued_at",
        (
            session_id,
            active_round["token"],
            active_round["item1_id"],
            active_round["item2_id"],
            active_round.get("item3_id"),
            active_round["issued_at"],
        ),
    )


async def save_active_round(session_id: str, active_round: dict[str, Any]) -> None:
    """Issue a round without rewriting the session.

    A round is not ranking data, so it neither bumps the revision nor discards
    a staged import. Every mutation rereads the latest round under the lock.
    """
    ids = vote_ids(active_round)
    async with transaction() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM items WHERE session_id = ? AND id IN ({', '.join('?' for _ in ids)})",
            (session_id, *ids),
        ) as cursor:
            (found,) = await cursor.fetchone()
        if found != len(ids):
            raise LookupError("대결 항목이 없습니다.")
        await _write_active_round(db, session_id, active_round)


async def session_exists(session_id: str) -> bool:
    """세션 스냅샷을 전부 읽지 않고 존재 및 모델 레코드 유무만 확인합니다."""
    async with (
        transaction(write=False) as db,
        db.execute(
            "SELECT 1 FROM sessions s "
            "JOIN ranking_models m ON m.session_id = s.id WHERE s.id = ?",
            (session_id,),
        ) as cursor,
    ):
        return await cursor.fetchone() is not None


async def stage_pending_import(
    session_id: str, expected_revision: int, raw: str, created_at: float
) -> None:
    """세션이 현재 revision 그대로 남아 있을 때만 가져오기 미리보기를 저장합니다."""
    async with transaction() as db:
        async with db.execute(
            "SELECT revision FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["revision"] != expected_revision:
            raise StaleSessionError(
                "다른 탭이나 기기에서 랭킹이 바뀌었거나 삭제되었습니다. 새로고침하세요."
            )
        await db.execute(
            "INSERT INTO pending_imports (session_id, revision, raw, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "revision=excluded.revision, raw=excluded.raw, created_at=excluded.created_at",
            (session_id, expected_revision, raw, created_at),
        )


async def fetch_recent_votes(session_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """취소되지 않은 최근 투표의 대결 구성만 매칭용으로 읽습니다."""
    async with (
        transaction(write=False) as db,
        db.execute(
            "SELECT event FROM vote_events WHERE session_id = ? AND undone = 0 "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ) as cursor,
    ):
        return [json.loads(r["event"])["payload"] for r in await cursor.fetchall()]


async def fetch_events(
    session_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    newest_first: bool = False,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    where = "session_id = ?" + (
        " AND undone = 0 AND archived = 0" if active_only else ""
    )
    order = "DESC" if newest_first else "ASC"
    sql = f"SELECT {_EVENT_COLUMNS} FROM vote_events WHERE {where} ORDER BY id {order}"
    params: tuple = (session_id,)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += (limit, offset)
    async with transaction(write=False) as db, db.execute(sql, params) as cursor:
        return [decode_event(row) for row in await cursor.fetchall()]


async def count_events(session_id: str) -> int:
    async with (
        transaction(write=False) as db,
        db.execute(
            "SELECT COUNT(*) FROM vote_events WHERE session_id = ?", (session_id,)
        ) as cursor,
    ):
        (count,) = await cursor.fetchone()
    return count
