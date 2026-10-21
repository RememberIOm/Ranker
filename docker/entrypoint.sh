#!/bin/sh
set -e
# Fly.io 볼륨이 root 소유로 마운트되므로 appuser에게 소유권 이전
chown -R appuser:appuser /data
# appuser로 권한 강하 후 CMD 실행
exec setpriv --reuid=1000 --regid=1000 --init-groups -- "$@"
