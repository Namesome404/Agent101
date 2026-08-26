#!/usr/bin/env bash
# 兼容旧脚本名 → runtime/start_voice_terminal.sh
exec bash "$(cd "$(dirname "$0")" && pwd)/start_voice_terminal.sh" "$@"
