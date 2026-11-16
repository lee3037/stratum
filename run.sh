#!/usr/bin/env bash
# 启动 Stratum 层析服务。用法：./run.sh [端口]，默认 8000
# 同目录下存在 stratum.env 时先加载其中的环境变量（格式见 deploy/stratum.env.example）。
set -euo pipefail
cd "$(dirname "$0")"
if [ -f stratum.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./stratum.env
  set +a
fi
PORT="${1:-${PORT:-8000}}"
HOST="${HOST:-0.0.0.0}"

is_py312() { "$1" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; }

if [ ! -x .venv/bin/python ]; then
  echo "首次运行：创建虚拟环境并安装依赖…"
  if command -v uv >/dev/null 2>&1; then
    uv venv -q .venv --python 3.12
    uv pip install -q --python .venv/bin/python -r requirements.txt
  else
    PY=""
    for candidate in python3.12 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && is_py312 "$candidate"; then PY="$candidate"; break; fi
    done
    if [ -z "$PY" ]; then
      echo "需要 Python 3.12。可以用 conda 创建：conda create -n stratum python=3.12，激活后重新运行；或安装 uv。" >&2
      exit 1
    fi
    "$PY" -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
  fi
elif ! is_py312 .venv/bin/python; then
  echo "警告：.venv 不是 Python 3.12（$(.venv/bin/python --version 2>&1)），建议删除 .venv 后重新运行。" >&2
fi
exec .venv/bin/python -m uvicorn app.server:app --host "$HOST" --port "$PORT"
