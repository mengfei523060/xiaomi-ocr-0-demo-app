#!/usr/bin/env bash
# 独立启动 Xiaomi-OCR-0 App：首次运行自动创建 app/.venv 并安装 core/requirements.txt
set -euo pipefail
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "[app] 创建虚拟环境并安装依赖…"
  "$PYTHON" -m venv .venv
  ./.venv/bin/pip install -q -r core/requirements.txt
fi
if [ "$#" -eq 0 ]; then
  exec ./.venv/bin/python server.py --port "${PORT:-8790}" --open
else
  exec ./.venv/bin/python server.py "$@"
fi
