#!/bin/sh
set -e
# Fly.io 볼륨이 root 소유로 마운트되므로 appuser에게 소유권 이전
chown -R appuser:appuser /data
# 읽지 않는 세션 접근 시각 열을 한 번 지웁니다. 이미 지웠으면 아무것도 하지 않습니다.
setpriv --reuid=1000 --regid=1000 --init-groups -- python -m scripts.drop_last_accessed "$DATABASE_PATH"
# appuser로 권한 강하 후 CMD 실행
exec setpriv --reuid=1000 --regid=1000 --init-groups -- "$@"
