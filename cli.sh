#!/usr/bin/env bash
# cli.sh —— ./cli/ 命令行的便捷包装器。
#
# 背景：直接跑 `python -m cli.main ...` 每次都要先 `source .venv/bin/activate`
# 激活虚拟环境、并保证在项目根目录下执行，很不方便。本脚本自动定位项目根目录
# 和项目自带的 venv，免去手动激活，转发全部参数给 cli.main。
#
# 用法（在任意目录均可）：
#   ./cli.sh start          # 等价于 (venv) python -m cli.main start
#   ./cli.sh stop
#   ./cli.sh restart
#   ./cli.sh clean
#   ./cli.sh model_check [--verify-hash]
#   ./cli.sh --help
#
# 退出码透传 cli.main 的退出码。
set -euo pipefail

# 项目根目录 = 本脚本所在目录（无论从哪里调用都正确解析）
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 优先用项目自带 venv 的解释器，无需 source 激活；缺失时回退系统 python3
VENV_PY="$ROOT_DIR/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  PY="$VENV_PY"
else
  echo "[cli.sh] 警告：未找到项目 venv ($VENV_PY)，回退系统 python3。" >&2
  echo "[cli.sh] 如缺依赖请先执行: bash deploy/bootstrap.sh" >&2
  PY="$(command -v python3 || true)"
  if [ -z "$PY" ]; then
    echo "[cli.sh] 错误：系统也没有 python3。" >&2
    exit 1
  fi
fi

# 在项目根目录执行，保证相对路径（config/settings.yaml、data/ 等）正确
cd "$ROOT_DIR"

# 透传全部参数给 cli.main 的 click group；exec 让退出码/信号直达
exec "$PY" -m cli.main "$@"
