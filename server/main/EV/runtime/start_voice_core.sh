#!/usr/bin/env bash
# 语音核心 :8000。必须在 server/ 目录启动；依赖 Muse :8002 已就绪（manager-api）。
set -euo pipefail
MUSE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_DIR="$(cd "$MUSE_DIR/../server" && pwd)"
if [ -x "$SERVER_DIR/.venv/bin/python" ]; then
  PY="$SERVER_DIR/.venv/bin/python"
elif [ -x "$SERVER_DIR/.venv/Scripts/python.exe" ]; then
  PY="$SERVER_DIR/.venv/Scripts/python.exe"
else
  echo "[voice_core] 缺少统一环境：$SERVER_DIR/.venv" >&2
  exit 1
fi
export PATH="$(dirname "$PY"):$PATH"
export MUSE_PYTHON="$PY"
LOG="$MUSE_DIR/tmp/voice_core.log"
mkdir -p "$MUSE_DIR/tmp"

if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[voice_core] :8000 已在监听，跳过"
  exit 0
fi

if ! curl -s -o /dev/null --max-time 2 http://127.0.0.1:8002/; then
  echo "[voice_core] 需要先启动 Muse :8002（manager-api）" >&2
  exit 1
fi

echo "[voice_core] 启动 server/app.py → :8000 ..."
cd "$SERVER_DIR"
# 新会话，避免被父 shell 退出带走
nohup "$PY" -u app.py >>"$LOG" 2>&1 </dev/null &
CORE_PID=$!
disown "$CORE_PID" 2>/dev/null || true

for i in $(seq 1 40); do
  if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[voice_core] 已监听 :8000 (pid=$CORE_PID)"
    exit 0
  fi
  if ! kill -0 "$CORE_PID" 2>/dev/null; then
    echo "[voice_core] 进程已退出，见 $LOG" >&2
    tail -n 40 "$LOG" >&2 || true
    exit 1
  fi
  sleep 0.5
done
echo "[voice_core] 等待 :8000 超时，见 $LOG" >&2
exit 1
