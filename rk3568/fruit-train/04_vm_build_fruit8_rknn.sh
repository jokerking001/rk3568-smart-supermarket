#!/usr/bin/env bash
#
# 04_vm_build_fruit8_rknn.sh — 水果 8 类模型在 RKNN 转换机上的一键脚本
#
# 在**转换机**（Ubuntu x86_64，例如 zwb@192.168.190.160）上运行。
# 把包里的 .pt / .onnx 转成 RK3568 能加载的 .rknn，可重复运行，
# 每一步做完就跳过，断了重跑不会从头再来。
#
#   bash 04_vm_build_fruit8_rknn.sh                 # 全流程（默认 INT8 + FP）
#   bash 04_vm_build_fruit8_rknn.sh --check-only    # 只看环境，不动任何东西
#   bash 04_vm_build_fruit8_rknn.sh --dtypes i8     # 只出 INT8，省一半时间
#   bash 04_vm_build_fruit8_rknn.sh --force-calib   # 强制重建校准集
#
# 默认值来自同目录的 bundle.env（由 04_pack_rknn_bundle.sh 生成），
# 命令行参数优先级更高。
#
# 离线优先：包里自带 .pt 和 calib_src/，正常情况全程不联网。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 默认值（bundle.env 会覆盖） ──────────────────────────────────────────
NAME="fruit8_yolo11n"
CALIB_COUNT="80"
CLASSES=""
WORK_DIR="${WORK_DIR:-$HOME/fruit8_rknn}"
DTYPES="i8,fp"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
CHECK_ONLY=0
FORCE_CALIB=0
NO_EXPORT=0

if [ -f "$SCRIPT_DIR/bundle.env" ]; then
  # shellcheck disable=SC1090
  . "$SCRIPT_DIR/bundle.env"
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --check-only)  CHECK_ONLY=1; shift ;;
    --dtypes)      DTYPES="$2"; shift 2 ;;
    --work-dir)    WORK_DIR="$2"; shift 2 ;;
    --calib-count) CALIB_COUNT="$2"; shift 2 ;;
    --name)        NAME="$2"; shift 2 ;;
    --force-calib) FORCE_CALIB=1; shift ;;
    --no-export)   NO_EXPORT=1; shift ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ─────────────────────────────────────────────────────────── 0. 环境 ──
say "0. 环境"
echo "   host       : $(uname -srm)"
echo "   bundle dir : $SCRIPT_DIR"
echo "   work dir   : $WORK_DIR"
echo "   model name : $NAME"
echo "   classes    : ${CLASSES:-（bundle.env 未提供）}"
command -v python3 >/dev/null || die "没有 python3。sudo apt install -y python3 python3-venv python3-pip"
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "   python3    : $PYV  ($(command -v python3))"
case "$PYV" in
  3.6|3.7|3.8|3.9|3.10|3.11) ok "python 版本在 rknn-toolkit2 2.3.2 支持范围内" ;;
  *) warn "rknn-toolkit2 2.3.2 官方支持 3.6-3.11，$PYV 可能装不上" ;;
esac
[ "$(uname -m)" = "x86_64" ] || die "RKNN-Toolkit2 只有 x86_64 Linux 版本，当前是 $(uname -m)"
python3 -c 'import venv' 2>/dev/null || warn "缺 python3-venv：sudo apt install -y python3-venv"
[ "$CHECK_ONLY" = "1" ] && { say "check-only：到此为止"; exit 0; }

mkdir -p "$WORK_DIR"

# ─────────────────────────────────────────────────────── 1. 虚拟环境 ──
say "1. python 环境"
VENV=""
for candidate in "$HOME/venvs/rknn-toolkit2" "$WORK_DIR/venv"; do
  if [ -x "$candidate/bin/python" ] && "$candidate/bin/python" -c 'import rknn' 2>/dev/null; then
    VENV="$candidate"
    ok "复用已有 toolkit 环境：$VENV"
    break
  fi
done
if [ -z "$VENV" ]; then
  VENV="$WORK_DIR/venv"
  if [ ! -x "$VENV/bin/python" ]; then
    echo "   新建 venv：$VENV"
    python3 -m venv "$VENV" || die "venv 创建失败（sudo apt install -y python3-venv）"
  fi
  ok "venv 就绪：$VENV"
  echo "   安装 rknn-toolkit2 2.3.2 + ultralytics（会拉 torch，慢，耐心等）"
  "$VENV/bin/pip" install -q --upgrade pip -i "$PYPI_MIRROR"
  "$VENV/bin/pip" install -q -i "$PYPI_MIRROR" \
      'numpy<2' 'rknn-toolkit2==2.3.2' ultralytics onnx onnxslim \
      opencv-python-headless tqdm psutil 'ruamel.yaml' scipy requests fast-histogram \
    || die "依赖安装失败。重试，或确认 PyPI 镜像可达。"
  ok "依赖装好了"
fi
PY="$VENV/bin/python"
"$PY" -c 'import rknn, numpy, cv2; print("   rknn/numpy/cv2 导入正常，numpy", numpy.__version__)' \
  || die "$VENV 里 toolkit 导入检查失败"

# ultralytics 只有「从 .pt 现场重导 ONNX」才用得上。
# --no-export + 包内自带 .onnx 时根本不会走到那条路，所以别白装一遍
# （它会连带拉 matplotlib/pandas 一堆，网络不通还会直接把脚本 die 掉）。
if [ "$NO_EXPORT" = "1" ] && [ -f "$SCRIPT_DIR/$NAME.onnx" ]; then
  ok "走 --no-export，跳过 ultralytics 检查（用包内 ONNX，不需要重导）"
elif "$PY" -c 'import ultralytics' 2>/dev/null; then
  ok "ultralytics 可用"
else
  echo "   venv 里缺 ultralytics，补装（重导 ONNX 要用）"
  "$VENV/bin/pip" install -q -i "$PYPI_MIRROR" ultralytics || die "ultralytics 安装失败"
  ok "ultralytics 装好了"
fi

# ───────────────────────────────────────────────────────── 2. 权重 ──
say "2. 权重"
PT="$WORK_DIR/$NAME.pt"
ONNX="$WORK_DIR/$NAME.onnx"

if [ -f "$SCRIPT_DIR/$NAME.pt" ]; then
  cp -f "$SCRIPT_DIR/$NAME.pt" "$PT"
  ok "用包内 .pt -> $PT"
elif [ -s "$PT" ]; then
  ok "工作目录已有 .pt：$PT"
else
  die "包里没有 $NAME.pt，也没有现成权重。把训练产出的 best.pt 放进包再跑。"
fi
echo "   .pt 大小: $(du -h "$PT" | cut -f1)  md5: $(md5sum "$PT" | cut -d' ' -f1)"

BUNDLED_ONNX=""
if [ -f "$SCRIPT_DIR/$NAME.onnx" ]; then
  cp -f "$SCRIPT_DIR/$NAME.onnx" "$ONNX"
  BUNDLED_ONNX="$ONNX"
  ok "用包内 .onnx -> $ONNX"
fi

# ─────────────────────────────────────────────────── 3. INT8 校准集 ──
say "3. INT8 校准图"
CALIB="$WORK_DIR/calib_640"
mkdir -p "$CALIB"
if [ "$FORCE_CALIB" = "1" ]; then
  echo "   --force-calib：清空重建"
  rm -f "$CALIB"/*.jpg "$CALIB"/*.png 2>/dev/null || true
fi
have=$(find "$CALIB" -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.png' \) | wc -l | tr -d ' ')
if [ "$have" -ge 8 ] && [ "$FORCE_CALIB" = "0" ]; then
  ok "已有 $have 张校准图，跳过"
elif [ -d "$SCRIPT_DIR/calib_src" ] && [ "$(find "$SCRIPT_DIR/calib_src" -maxdepth 1 -type f | wc -l | tr -d ' ')" -gt 0 ]; then
  echo "   把 calib_src/ 缩放到 640x640 -> $CALIB"
  "$PY" - "$SCRIPT_DIR/calib_src" "$CALIB" "$CALIB_COUNT" <<'PYEOF'
import os, sys
import cv2
import numpy as np
src, dst, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
names = sorted(os.listdir(src))
done = 0
for name in names:
    if done >= limit:
        break
    path = os.path.join(src, name)
    if not os.path.isfile(path):
        continue
    img = cv2.imread(path)
    if img is None:
        print("   skip %s (读不出来)" % name)
        continue
    img = cv2.resize(img, (640, 640), interpolation=cv2.INTER_AREA)
    cv2.imwrite(os.path.join(dst, os.path.splitext(name)[0] + ".jpg"), img)
    done += 1
print("   写出 %d 张校准图" % done)
if done < 8:
    sys.exit("校准图太少；INT8 量化必须有代表性数据")
PYEOF
  ok "校准集就绪：$(find "$CALIB" -maxdepth 1 -type f | wc -l | tr -d ' ') 张"
else
  warn "包里没有 calib_src/，且 $CALIB 里不足 8 张"
  warn "INT8 会退化成没有代表性的量化，精度可能崩。建议放真实水果照片进来。"
  if [ ! -f "$SCRIPT_DIR/$NAME.pt" ] && [ ! -s "$PT" ]; then
    die "连权重都没有，没法继续"
  fi
fi

# ───────────────────────────────────────────────────────── 4. 转换 ──
say "4. 转换 RKNN（$DTYPES）"
BUILD="$SCRIPT_DIR/build_fruit_rknn.py"
[ -f "$BUILD" ] || die "包里没有 build_fruit_rknn.py"
cp -f "$BUILD" "$WORK_DIR/build_fruit_rknn.py"

cd "$WORK_DIR"
set +e
if [ "$NO_EXPORT" = "1" ] && [ -n "$BUNDLED_ONNX" ]; then
  echo "   --no-export：直接用包内 ONNX"
  "$PY" "$WORK_DIR/build_fruit_rknn.py" \
      --onnx "$BUNDLED_ONNX" \
      --no-export \
      --calib-dir "$CALIB" \
      --calib-limit "$CALIB_COUNT" \
      --out-dir "$WORK_DIR/out" \
      --platform rk3568 \
      --dtypes "$DTYPES" \
      --name "$NAME"
else
  echo "   从 .pt 重新导出 ONNX 再转换（保证是单输出布局）"
  "$PY" "$WORK_DIR/build_fruit_rknn.py" \
      --pt "$PT" \
      --calib-dir "$CALIB" \
      --calib-limit "$CALIB_COUNT" \
      --out-dir "$WORK_DIR/out" \
      --platform rk3568 \
      --dtypes "$DTYPES" \
      --name "$NAME"
fi
RC=$?
set -e

# ───────────────────────────────────────────────────────── 5. 结果 ──
say "5. 结果"
if [ "$RC" -ne 0 ]; then
  echo "   转换进程退出码 $RC —— 看上面的日志。"
fi
shopt -s nullglob
found=("$WORK_DIR"/out/*.rknn)
if [ ${#found[@]} -eq 0 ]; then
  die "没有产出任何 .rknn。检查 out/report.json 和上面的日志。"
fi
for f in "${found[@]}"; do
  printf '   %-40s %8s  md5 %s\n' "$(basename "$f")" \
    "$(du -h "$f" | cut -f1)" "$(md5sum "$f" | cut -d' ' -f1)"
done
[ -f "$WORK_DIR/out/report.json" ] && echo "   报告: $WORK_DIR/out/report.json"

cat <<EOF

$(printf '\033[1;32m转换完成\033[0m') —— 把 INT8 模型推到板端：

  pscp "$WORK_DIR/out/${NAME}_i8.rknn" linaro@<板端IP>:/home/linaro/ai/models/${NAME}.rknn

板端 fruit 服务每 2 秒重试加载模型，文件到位后自动生效，不用重启：

  curl -s --noproxy '*' http://127.0.0.1:8089/api/fruit/status

看到 "model_loaded": true 就成了。
然后**务必**在板端跑一次类别对齐校验（模型类别名必须和融合规则 label 一致）：

  python3 05_check_labels.py --classes /home/linaro/ai/models/${NAME}.classes.txt

类别名：${CLASSES:-（见包内 classes.txt）}
EOF
