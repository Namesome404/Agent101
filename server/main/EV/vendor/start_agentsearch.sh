#!/usr/bin/env bash
# 启动本地 AgentSearch（FastAPI，只绑回环，内部调用本机 SearXNG）。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .agentsearch-venv/bin/python ]; then
  echo "[agentsearch] 尚未安装，请先跑 ./setup_search.sh" >&2; exit 1
fi
ENV_FILE=agent_search/.env.native
if [ ! -f "$ENV_FILE" ]; then
  # 机器相关配置不入库，缺失时按本机情况生成；代理探测失败就留空（国内引擎仍可直连）
  PROXY=""
  for p in 7897 7890 1087; do
    if lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then PROXY="http://127.0.0.1:$p"; break; fi
  done
  cat > "$ENV_FILE" <<EOF
SEARXNG_URL=http://127.0.0.1:8088
HOST=127.0.0.1
PORT=3939
DATA_DIR=./data
SQLITE_TIMEOUT=1.0
ADAPTERS_DIR=./adapters
HTTP_PROXY=$PROXY
HTTPS_PROXY=$PROXY
NO_PROXY=127.0.0.1,localhost
AGENT_SEARCH_TOKEN=
EOF
  echo "[agentsearch] 已生成 $ENV_FILE（代理=${PROXY:-无}）"
fi
cd agent_search
set -a; . ./.env.native; set +a
exec ../.agentsearch-venv/bin/python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-3939}"
