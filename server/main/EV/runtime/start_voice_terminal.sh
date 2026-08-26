#!/usr/bin/env bash
set -euo pipefail
# 兼容调试入口。生产启动只使用 run_muse.sh；此脚本仍强制同一 venv。
MUSE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$MUSE_DIR"

if [ -x ../server/.venv/bin/python ]; then
  PY=../server/.venv/bin/python
elif [ -x ../server/.venv/Scripts/python.exe ]; then
  PY=../server/.venv/Scripts/python.exe
else
  echo "[voice_terminal] 缺少统一环境：server/main/server/.venv" >&2
  exit 1
fi
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
export PATH="$(dirname "$PY"):$PATH"
export MUSE_PYTHON="$PY"

export VOICE_INPUT="${VOICE_INPUT:-${CAMERA_VOICE_INPUT:-auto}}"
export VOICE_OUTPUT="${VOICE_OUTPUT:-${CAMERA_VOICE_OUTPUT:-pc}}"

exec "$PY" -X utf8 -m devices.voice.terminal
