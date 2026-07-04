#!/usr/bin/env bash
# make_release.sh —— 生成正式版本发布包（用于上传/同步必要的代码）。
#
# 设计：基于 `git archive`，发布包只包含「git 跟踪的文件」——也就是这套代码
# 真正需要的源码 + 文档 + 部署脚本 + 配置模板。凡是 .gitignore 排除的东西
# （运行态 data/、含明文密钥的 config/settings.yaml、frontend/node_modules、
# frontend/dist、.venv、*.pid、日志、备份等）都不会进入发布包，天然干净、
# 不泄密。
#
# 用法：
#   bash tools/make_release.sh            # 用最新的版本 tag（没有则用 HEAD）
#   bash tools/make_release.sh v2.2.0     # 指定某个已存在的版本 tag
#
# 产物：
#   release/gb10-manager-<版本>.tar.gz    （release/ 已在 .gitignore，不入库）
#   解压后顶层目录为 gb10-manager-<版本>/
#
# 目标机部署：解压后 `cd gb10-manager-<版本> && bash deploy/bootstrap.sh`
# 会建 venv、装依赖、生成 config/settings.yaml；前端 `cd frontend && npm install
# && npm run build`；再 `./cli.sh start`。
#
# 退出码：0 成功；1 参数/环境错误；2 git 操作失败。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d .git ]; then
  echo "[ERROR] 当前目录不是 git 仓库: $ROOT_DIR" >&2
  exit 1
fi

# 选定要打包的 ref：参数指定的 tag > 最新 tag > HEAD
REQUESTED="${1:-}"
if [ -n "$REQUESTED" ]; then
  if ! git rev-parse "$REQUESTED" >/dev/null 2>&1; then
    echo "[ERROR] 找不到 ref/tag: $REQUESTED" >&2
    exit 1
  fi
  REF="$REQUESTED"
  VERSION="$REQUESTED"
else
  REF="$(git describe --tags --abbrev=0 2>/dev/null || true)"
  if [ -n "$REF" ]; then
    VERSION="$REF"
    echo "[make_release] 未指定版本，使用最新 tag: $REF"
  else
    REF="HEAD"
    VERSION="$(git rev-parse --short HEAD)"
    echo "[make_release] 仓库无 tag，使用 HEAD ($VERSION)"
  fi
fi

# 提示：git archive 打包的是「已提交」内容，不含未提交的工作区改动
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[make_release] 注意：工作区有未提交改动，发布包只含已提交内容（$REF）。" >&2
fi

PREFIX="gb10-manager-${VERSION}"
OUT_DIR="$ROOT_DIR/release"
OUT_FILE="$OUT_DIR/${PREFIX}.tar.gz"
mkdir -p "$OUT_DIR"

echo "[make_release] 打包 ref=$REF → $OUT_FILE"
git archive --format=tar.gz --prefix="${PREFIX}/" -o "$OUT_FILE" "$REF" || {
  echo "[ERROR] git archive 失败" >&2; exit 2; }

# 摘要：文件数 + 体积（从产出的 tar.gz 直接统计，排除目录条目）
FILE_COUNT="$(tar -tzf "$OUT_FILE" 2>/dev/null | grep -vc '/$' || true)"
SIZE="$(du -h "$OUT_FILE" | cut -f1)"

echo
echo "[OK] 发布包已生成：$OUT_FILE"
echo "     版本: $VERSION   文件数: ${FILE_COUNT}   体积: ${SIZE}"
echo "     顶层目录: ${PREFIX}/"
echo
echo "  上传/同步示例："
echo "    scp $OUT_FILE user@host:/path/"
echo "  目标机部署："
echo "    tar xzf ${PREFIX}.tar.gz && cd ${PREFIX}"
echo "    bash deploy/bootstrap.sh && (cd frontend && npm install && npm run build) && ./cli.sh start"
