#!/usr/bin/env bash
# ============================================================
#  安装 git pre-commit 钩子：提交前自动跑凭据残留检查
# ============================================================
#
#  用法（在仓库根目录执行一次即可，钩子不进版本库）：
#      bash tools/install-hooks.sh
#
#  卸载：
#      rm .git/hooks/pre-commit
#
# ============================================================
set -e

REPO_ROOT=$(git rev-parse --show-toplevel)
HOOK="$REPO_ROOT/.git/hooks/pre-commit"

if [ -z "$REPO_ROOT" ]; then
  echo "不在 git 仓库里，先 cd 到仓库根目录。" >&2
  exit 1
fi

cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
# 凭据残留守卫 —— 由 tools/install-hooks.sh 安装
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

PY=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "[pre-commit] 找不到 python，跳过凭据检查" >&2
  exit 0
fi

if ! "$PY" tools/check_literals.py; then
  echo "" >&2
  echo "提交被拦下：检测到明文凭据残留。" >&2
  echo "把它们改到 secrets.h 之后再提交；确要跳过用 git commit --no-verify" >&2
  exit 1
fi

exit 0
HOOK_EOF

chmod +x "$HOOK"
echo "已安装 pre-commit 钩子 -> $HOOK"
echo "试跑一次："
bash "$HOOK"
