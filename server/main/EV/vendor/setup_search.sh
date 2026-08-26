#!/usr/bin/env bash
# 一次性安装本地搜索底座：SearXNG 源码 + 独立 venv。
# 源码与 venv 不入库（见 .gitignore），换机后跑这个脚本即可重建。
set -euo pipefail
cd "$(dirname "$0")"
PY="${SEARXNG_PYTHON:-/opt/homebrew/bin/python3.11}"
[ -x "$PY" ] || PY="$(command -v python3)"

if [ ! -d searxng-src ]; then
  echo "[setup] 拉取 SearXNG 源码…"
  git clone --depth 1 https://github.com/searxng/searxng.git searxng-src
fi
if [ ! -x .searxng-venv/bin/python ]; then
  echo "[setup] 建立 venv（$PY）…"
  "$PY" -m venv .searxng-venv
fi
echo "[setup] 安装依赖…"
.searxng-venv/bin/pip install -q --upgrade pip
.searxng-venv/bin/pip install -q -r searxng-src/requirements.txt
# --- AgentSearch（检索编排 + 正文抽取），独立 venv 避免与 EV 主环境版本冲突 ---
if [ ! -x .agentsearch-venv/bin/python ]; then
  echo "[setup] 建立 AgentSearch venv…"
  "$PY" -m venv .agentsearch-venv
fi
echo "[setup] 安装 AgentSearch 依赖…"
.agentsearch-venv/bin/pip install -q --upgrade pip
.agentsearch-venv/bin/pip install -q -r agent_search/requirements.txt
if [ "${SKIP_CHROMIUM:-0}" != "1" ]; then
  echo "[setup] 安装 chromium（浏览器渲染抓 JS 动态页用，约 95MB）…"
  .agentsearch-venv/bin/python -m playwright install chromium
fi

echo "[setup] 完成。先 ./start_searxng.sh，再 ./start_agentsearch.sh。"
