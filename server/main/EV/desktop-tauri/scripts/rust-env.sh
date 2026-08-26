#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

export CARGO_HOME="$PROJECT_DIR/.cargo-home"
export RUSTUP_HOME="$PROJECT_DIR/.rustup-home"
export RUSTUP_DIST_SERVER="https://rsproxy.cn"
export RUSTUP_UPDATE_ROOT="https://rsproxy.cn/rustup"
export PATH="$CARGO_HOME/bin:$PATH"

exec "$@"
