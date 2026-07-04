#!/usr/bin/env bash
# 版本发布脚本 —— 在 DgxSpark 服务器的项目 git 仓库里执行。
#
# 背景：CLAUDE.md 约定"每一次推送代码到服务器都要做版本发布，记录每次问题
# 和改进内容"。这个脚本把那套手工流程标准化：暂存代码改动（运行态文件由
# .gitignore 排除）、用统一格式提交、打一个带版本号的 git tag，并把这一行
# 追加到 docs/Task_Tracking.md 的版本表里，避免每次手敲 git 命令时格式/遗漏不一致。
#
# 用法：
#   bash tools/release.sh <版本号> <一句话变更说明>
# 例：
#   bash tools/release.sh v1.2.0 "新增单模型加载限制与API健康检测"
#
# 选项（环境变量）：
#   NO_TAG=1     只提交不打 tag
#   NO_TRACK=1   不自动往 docs/Task_Tracking.md 追加版本行
#
# 退出码：0 成功；1 参数/环境错误；2 git 操作失败。
set -euo pipefail

VERSION="${1:-}"
MESSAGE="${2:-}"

if [ -z "$VERSION" ] || [ -z "$MESSAGE" ]; then
  echo "用法: bash tools/release.sh <版本号> <变更说明>" >&2
  echo "例:   bash tools/release.sh v1.2.0 \"新增单模型加载限制与API健康检测\"" >&2
  exit 1
fi

# 版本号格式校验：vMAJOR.MINOR.PATCH
if ! printf '%s' "$VERSION" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "[ERROR] 版本号格式应为 vX.Y.Z（如 v1.2.0），收到: $VERSION" >&2
  exit 1
fi

# 定位项目根目录（脚本在 tools/ 下）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d .git ]; then
  echo "[ERROR] 当前目录不是 git 仓库: $ROOT_DIR" >&2
  exit 1
fi

# 提交者身份兜底（服务器上常见 user.name/email 未配置的警告）
if ! git config user.name >/dev/null 2>&1; then
  git config user.name "gb10-release"
fi
if ! git config user.email >/dev/null 2>&1; then
  git config user.email "release@gb10.local"
fi

# tag 重名保护
if git rev-parse "$VERSION" >/dev/null 2>&1; then
  echo "[ERROR] tag $VERSION 已存在，请换一个版本号" >&2
  exit 1
fi

TODAY="$(date +%Y-%m-%d)"

# 可选：把版本行追加进 docs/Task_Tracking.md 的版本表（紧跟在表头分隔行之后）
if [ "${NO_TRACK:-0}" != "1" ] && [ -f docs/Task_Tracking.md ]; then
  python3 - "$VERSION" "$TODAY" "$MESSAGE" <<'PYEOF'
import sys
version, today, message = sys.argv[1], sys.argv[2], sys.argv[3]
path = "docs/Task_Tracking.md"
with open(path, encoding="utf-8") as f:
    content = f.read()
row = f"| {version} | {today} | {message} |\n"
# 插入到"| 版本 | 日期 | 内容 |"表头下面的分隔行之后
marker = "| --- | --- | --- |\n"
idx = content.find(marker)
if idx != -1 and row.strip() not in content:
    pos = idx + len(marker)
    content = content[:pos] + row + content[pos:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  已追加版本行到 docs/Task_Tracking.md: {version}")
else:
    print("  docs/Task_Tracking.md 未改动（未找到版本表或该行已存在）")
PYEOF
fi

echo "== 暂存改动（运行态文件由 .gitignore 排除）=="
git add -A
git status --short

if git diff --cached --quiet; then
  echo "[ERROR] 没有可提交的改动（暂存区为空）" >&2
  exit 2
fi

COMMIT_MSG="$VERSION: $MESSAGE"
echo "== 提交：$COMMIT_MSG =="
if ! git commit -m "$COMMIT_MSG"; then
  echo "[ERROR] git commit 失败" >&2
  exit 2
fi

if [ "${NO_TAG:-0}" != "1" ]; then
  echo "== 打 tag：$VERSION =="
  if ! git tag -a "$VERSION" -m "$COMMIT_MSG"; then
    echo "[ERROR] git tag 失败" >&2
    exit 2
  fi
fi

echo
echo "[OK] 发布完成：$VERSION"
git --no-pager log --oneline -1
[ "${NO_TAG:-0}" != "1" ] && echo "tag: $VERSION"
exit 0
