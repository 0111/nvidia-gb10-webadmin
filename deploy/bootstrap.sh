#!/usr/bin/env bash
# bootstrap.sh — 两步式自动部署脚本（对应 Project_Task.md 2.3.1）
#
# 第一步：运行环境 checklist 检测（复用 core.env_doctor.run_all_checks），
#         打印满足/不满足项及处理建议，要求用户确认后才继续。
# 第二步：创建 venv、安装依赖、首次生成 config/settings.yaml（复用
#         core.config.load_config 的自动持久化 + 随机密钥生成逻辑）、
#         创建 data/ 目录结构。
#
# 用法：
#   cd /home/spark/nvidia-gb10-manager
#   bash deploy/bootstrap.sh
#
# 本脚本只能在 DGX Spark (ARM64 Linux) 服务器上执行。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${PROJECT_ROOT}"

echo "============================================================"
echo " NVIDIA DGX Spark / GB10 Manager — 自动部署脚本"
echo " 项目目录: ${PROJECT_ROOT}"
echo "============================================================"

echo
echo "---- 第一步：环境 Checklist 检测 ----"
echo "(复用 core.env_doctor.run_all_checks，逐项打印满足/不满足及处理建议)"
echo

"${PYTHON_BIN}" - <<'PYEOF'
import sys

sys.path.insert(0, ".")

try:
    from core import env_doctor
except Exception as exc:  # pragma: no cover - first run before deps installed
    print(f"[WARN] 无法导入 core.env_doctor（可能尚未安装依赖），跳过详细检测: {exc}")
    sys.exit(0)

ICON = {"ok": "[OK]   ", "warning": "[WARN] ", "error": "[ERROR]"}

report = env_doctor.run_all_checks()
for check in report.checks:
    icon = ICON.get(check.status, "[?]    ")
    print(f"  {icon} [{check.name}] {check.message}")
    if check.suggested_command:
        print(f"           建议命令: {check.suggested_command}")

print()
print(f"  总体状态: {report.overall_status}")
PYEOF

echo
echo "以上为当前环境检测结果。对于标记为 [WARN]/[ERROR] 的项目，"
echo "建议先手动执行对应的建议命令（或后续通过 CLI: python -m cli.main start 交互式修复）。"
echo

read -r -p "环境检测已完成，是否继续执行部署（创建 venv / 安装依赖 / 生成配置）？[y/N] " CONFIRM
if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    echo "用户取消，部署已中止。"
    exit 1
fi

echo
echo "---- 第二步：部署项目代码、依赖与配置 ----"

echo "[1/5] 创建 Python 虚拟环境: ${VENV_DIR}"
if [[ ! -d "${VENV_DIR}" ]]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
    echo "      已存在，跳过创建"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "[2/5] 安装依赖: requirements.txt"
pip install --upgrade pip >/dev/null
pip install -r "${PROJECT_ROOT}/requirements.txt"

echo "[3/5] 生成/确认配置文件 config/settings.yaml（含随机密钥）"
python - <<'PYEOF'
import sys

sys.path.insert(0, ".")

from core.config import DEFAULT_CONFIG_PATH, load_config

# load_config 在文件不存在时会自动创建带随机 secret_key / admin_password
# 的默认配置并立即写盘；若已存在则原样加载，不会覆盖。
config = load_config()
print(f"      配置文件路径: {DEFAULT_CONFIG_PATH}")
print(f"      管理员账号: {config.admin_username}")
print(f"      Web 端口: {config.web_port}")
print(f"      SearXNG URL: {config.searxng_url}")
PYEOF

echo "[4/5] 创建 data/ 目录结构"
mkdir -p "${PROJECT_ROOT}/data"
mkdir -p "${PROJECT_ROOT}/data/compose"
mkdir -p "${PROJECT_ROOT}/data/logs"
mkdir -p "${PROJECT_ROOT}/data/reports"

echo "[5/5] 构建已打补丁的 vllm 镜像 gb10-vllm:26.06-py3-patched"
# 26.06-py3 基础镜像内 fastapi 0.137.1 + prometheus_fastapi_instrumentator 8.0.0
# 不兼容，会让 /v1/* 推理接口全部 500（'_IncludedRouter' object has no attribute
# 'path'）。deploy/vllm-patch.Dockerfile 用一行 sed 修掉，config.vllm_image 默认
# 即指向这个补丁镜像。此处幂等构建（已存在则 docker build 走缓存秒过）。
PATCHED_IMAGE="gb10-vllm:26.06-py3-patched"
if command -v docker >/dev/null 2>&1; then
    if docker image inspect "${PATCHED_IMAGE}" >/dev/null 2>&1; then
        echo "      已存在 ${PATCHED_IMAGE}，跳过（如需重建: docker build -f deploy/vllm-patch.Dockerfile -t ${PATCHED_IMAGE} deploy/）"
    else
        echo "      构建中（首次需先 docker pull 基础镜像 nvcr.io/nvidia/vllm:26.06-py3）..."
        docker build -f "${PROJECT_ROOT}/deploy/vllm-patch.Dockerfile" -t "${PATCHED_IMAGE}" "${PROJECT_ROOT}/deploy/" \
            && echo "      ${PATCHED_IMAGE} 构建完成" \
            || echo "      [WARN] 补丁镜像构建失败，请确认已能拉取基础镜像后手动重试"
    fi
else
    echo "      [WARN] 未检测到 docker，跳过补丁镜像构建（部署模型前需先构建 ${PATCHED_IMAGE}）"
fi

echo
echo "============================================================"
echo " 部署完成。"
echo " 后续步骤："
echo "   1. 启动 SearXNG（可选，用于本地搜索引擎集成）："
echo "      docker compose -f deploy/searxng-compose.yml up -d"
echo "   2. 启动 Web 后端："
echo "      source .venv/bin/activate && uvicorn web.main:app --host 0.0.0.0 --port 8000"
echo "   3. 使用 CLI："
echo "      ./cli.sh start"
echo "============================================================"
