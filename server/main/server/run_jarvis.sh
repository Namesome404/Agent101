#!/usr/bin/env bash
set -euo pipefail
# Jarvis 核心调试入口；统一使用当前目录的 .venv，不回退系统 Python。
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  PY="$(pwd)/.venv/bin/python"
elif [ -x .venv/Scripts/python.exe ]; then
  PY="$(pwd)/.venv/Scripts/python.exe"
else
  echo "[run_jarvis] 缺少 .venv" >&2
  exit 1
fi
export PATH="$(dirname "$PY"):$PATH"
export MUSE_PYTHON="$PY"
exec "$PY" -X utf8 app.py
