#!/usr/bin/env bash
# 独立启动 Xiaomi-OCR-0 Mini：首次运行自动创建 .venv 并安装 core/requirements.txt
# 前提：本机已安装 llama.cpp 或 Ollama，并已配置 xiaomi-ocr-0（见 README）
set -euo pipefail
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "[mini] 创建虚拟环境并安装依赖…"
  "$PYTHON" -m venv .venv
  ./.venv/bin/pip install -q -r core/requirements.txt
fi
if [ "$#" -eq 0 ]; then
  exec ./.venv/bin/python server.py --port "${PORT:-8791}" --open
else
  exec ./.venv/bin/python server.py "$@"
fi
