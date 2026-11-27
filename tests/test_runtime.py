"""패키지 실행과 현재 백업 형식의 경계."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ranker.store import InvalidSessionDataError, open_store


@pytest.mark.parametrize("version", [None, 1, 2, 3, 4.5, "4"])
async def test_obsolete_backup_versions_leave_saved_data(store_with_items, version):
    original = await store_with_items.export_json()
    backup = json.loads(original)
    if version is None:
        del backup["schema_version"]
    else:
        backup["schema_version"] = version
    with pytest.raises(InvalidSessionDataError):
        await store_with_items.import_json(json.dumps(backup))
    loaded = await open_store(store_with_items._session_id)
    assert await loaded.export_json() == original


@pytest.mark.parametrize(
    "damage", ["missing_rating", "draw_count", "elo", "observations"]
)
async def test_current_backup_is_rejected_without_repair(store_with_items, damage):
    original = await store_with_items.export_json()
    backup = json.loads(original)
    if damage == "missing_rating":
        backup["items"][0]["mu"] = {}
    elif damage == "draw_count":
        backup["criteria"][0]["draws"] = 99
    elif damage == "elo":
        backup["items"][0]["ratings"] = backup["items"][0].pop("mu")
    else:
        del backup["observations"]
    with pytest.raises((ValidationError, InvalidSessionDataError)):
        await store_with_items.import_json(json.dumps(backup))
    loaded = await open_store(store_with_items._session_id)
    assert await loaded.export_json() == original


def test_app_resources_and_lifespan_from_another_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from fastapi.testclient import TestClient
from ranker.main import app

with TestClient(app) as client:
    assert client.get('/health').json() == {'status': 'ok'}
    assert not client.cookies
    assert client.get('/').status_code == 200
    assert client.get('/static/battle.js').status_code == 200
    assert client.post('/start').status_code == 200
    assert client.get('/manage').status_code == 200
""",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "DATABASE_PATH": str(tmp_path / "ranker.db"),
            "COOKIE_SECURE": "false",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
