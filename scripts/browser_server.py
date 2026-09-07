"""브라우저 테스트가 실제 데이터와 분리된 서버를 사용하도록 합니다."""

import os
import sys
import tempfile
from pathlib import Path

import uvicorn


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ranker-browser-") as directory:
        os.environ.update(
            {
                "DATABASE_PATH": str(Path(directory) / "ranker.db"),
                "SESSION_DIR": str(Path(directory) / "sessions"),
                "COOKIE_SECURE": "false",
            }
        )
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        uvicorn.run("main:app", host="127.0.0.1", port=8097)


if __name__ == "__main__":
    main()
