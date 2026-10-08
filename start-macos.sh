#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "此脚本仅用于 macOS。" >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "请先安装 ffmpeg：brew install ffmpeg" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  uv venv --python 3.11 .venv
fi

uv pip install --python .venv/bin/python -r backend/requirements-macos.txt

exec .venv/bin/python launcher.py
