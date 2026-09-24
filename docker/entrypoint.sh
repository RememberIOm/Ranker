#!/bin/sh
set -e
# Fly.io 볼륨이 root 소유로 마운트되므로 appuser에게 소유권 이전
chown -R appuser:appuser /data
# 이전 스키마 DB가 있으면 한 번 변환합니다. 이미 현재 형식이면 아무것도 하지 않습니다.
setpriv --reuid=1000 --regid=1000 --init-groups -- python -m scripts.convert_legacy_db "$DATABASE_PATH"
# appuser로 권한 강하 후 CMD 실행
exec setpriv --reuid=1000 --regid=1000 --init-groups -- "$@"
