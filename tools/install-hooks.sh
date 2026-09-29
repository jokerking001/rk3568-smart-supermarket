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
# 提交前守卫 —— 由 tools/install-hooks.sh 安装
#   1) 凭据残留：扫全仓库
#   2) Python 3.7 兼容：只扫本次暂存的 .py（板子是 3.7.3，3.8+ 语法上板必炸）
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

PY=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "[pre-commit] 找不到 python，跳过检查" >&2
  exit 0
fi

if ! "$PY" tools/check_literals.py; then
  echo "" >&2
  echo "提交被拦下：检测到明文凭据残留。" >&2
  echo "把它们改到 secrets.h 之后再提交；确要跳过用 git commit --no-verify" >&2
  exit 1
fi

# 只挑暂存区的 .py —— 全仓库扫太慢，而且没改的文件上次已经过了
PY_FILES=()
while IFS= read -r f; do
  [ -n "$f" ] && [ -f "$f" ] && PY_FILES+=("$f")
done < <(git diff --cached --name-only --diff-filter=ACM | grep -E '\.py$' || true)

if [ ${#PY_FILES[@]} -gt 0 ]; then
  if ! "$PY" tools/check_py37.py "${PY_FILES[@]}"; then
    echo "" >&2
    echo "提交被拦下：板端 Python 是 3.7.3，上面这些写法上板会炸。" >&2
    echo "确要跳过用 git commit --no-verify" >&2
    exit 1
  fi
fi

exit 0
HOOK_EOF

chmod +x "$HOOK"
echo "已安装 pre-commit 钩子 -> $HOOK"
echo "试跑一次："
bash "$HOOK"
