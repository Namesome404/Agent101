#!/usr/bin/env bash
# EV 唯一运行入口：控制面、语音终端和 Python 子进程全部使用同一 venv。
set -euo pipefail
MUSE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$MUSE_DIR"
if [ -x ../server/.venv/bin/python ]; then
  PY="$(cd ../server/.venv/bin && pwd)/python"
elif [ -x ../server/.venv/Scripts/python.exe ]; then
  PY="$(cd ../server/.venv/Scripts && pwd)/python.exe"
else
  echo "[run_muse] 缺少统一环境：server/main/server/.venv" >&2
  exit 1
fi
VENV_BIN="$(dirname "$PY")"
export PATH="$VENV_BIN:$PATH"
export MUSE_PYTHON="$PY"
export VOICE_INPUT="${VOICE_INPUT:-auto}"
export VOICE_OUTPUT="${VOICE_OUTPUT:-pc}"

echo "[run_muse] Python=$PY"
echo "[run_muse] 启动 EV :8002（语音终端由主进程看护）..."
exec "$PY" -X utf8 app.py
