#!/usr/bin/env bash
# 启动本地 SearXNG（仅监听 127.0.0.1，供 AgentSearch 内部调用）。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .searxng-venv/bin/python ]; then
  echo "[searxng] 尚未安装，请先跑 ./setup_search.sh" >&2; exit 1
fi
export SEARXNG_SETTINGS_PATH="$PWD/searxng-conf/settings.yml"
cd searxng-src
exec ../.searxng-venv/bin/python -m searx.webapp
