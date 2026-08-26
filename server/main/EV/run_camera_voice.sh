#!/usr/bin/env bash
# 兼容旧脚本名 → run_voice_terminal.sh
set -e
cd "$(dirname "$0")"
exec bash runtime/start_voice_terminal.sh
