#!/usr/bin/env bash
#
# vm_build_fruit_rknn.sh — one-command fruit-model conversion on the RKNN host
#
# Run this ON THE VM (Ubuntu x86_64, e.g. zwb@192.168.190.160).
# It turns the bundled fruit YOLO11s weights into .rknn files the RK3568 board
# can load, and it is safe to re-run: every step is skipped if already done.
#
#   bash vm_build_fruit_rknn.sh              # full run (offline if bundle present)
#   bash vm_build_fruit_rknn.sh --check-only # just report the environment
#   bash vm_build_fruit_rknn.sh --dtypes i8  # build only the INT8 model
#
# Offline-first: if fruits_yolo11s.pt and calib_640/ sit next to this script it
# uses them and never touches the network.  Otherwise it downloads from
# hf-mirror.com (huggingface.co is unreachable from mainland China).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${WORK_DIR:-$HOME/fruit_rknn}"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
HF="${HF_MIRROR:-https://hf-mirror.com}"
MODEL_REPO="johnatanvq/fruits-yolo-model"
DATA_REPO="datasets/johnatanvq/fruits-dataset"
CALIB_COUNT="${CALIB_COUNT:-80}"
DTYPES="i8,fp"
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1; shift ;;
    --dtypes)     DTYPES="$2"; shift 2 ;;
    --work-dir)   WORK_DIR="$2"; shift 2 ;;
    --calib-count) CALIB_COUNT="$2"; shift 2 ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

say "0. environment"
echo "   host      : $(uname -srm)"
echo "   work dir  : $WORK_DIR"
echo "   bundle dir: $SCRIPT_DIR"
command -v python3 >/dev/null || die "python3 not found. sudo apt install -y python3 python3-venv python3-pip"
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "   python3   : $PYV  ($(command -v python3))"
case "$PYV" in
  3.6|3.7|3.8|3.9|3.10|3.11) ok "python version supported by rknn-toolkit2 2.3.2" ;;
  *) warn "rknn-toolkit2 2.3.2 officially supports python 3.6-3.11; $PYV may fail" ;;
esac
python3 -c 'import venv' 2>/dev/null || warn "python3-venv missing -> sudo apt install -y python3-venv"
[ "$(uname -m)" = "x86_64" ] || die "RKNN-Toolkit2 only runs on x86_64 Linux, this is $(uname -m)"
[ "$CHECK_ONLY" = "1" ] && { say "check-only: stopping here"; exit 0; }

mkdir -p "$WORK_DIR"

# ---------------------------------------------------------------- 1. venv ----
say "1. python environment"
# Prefer the toolkit venv the handoff document says already exists, if it works.
VENV=""
for candidate in "$HOME/venvs/rknn-toolkit2" "$WORK_DIR/venv"; do
  if [ -x "$candidate/bin/python" ] && "$candidate/bin/python" -c 'import rknn' 2>/dev/null; then
    VENV="$candidate"
    ok "reusing existing toolkit venv: $VENV"
    break
  fi
done
if [ -z "$VENV" ]; then
  VENV="$WORK_DIR/venv"
  if [ ! -x "$VENV/bin/python" ]; then
    echo "   creating venv at $VENV"
    python3 -m venv "$VENV" || die "venv creation failed (sudo apt install -y python3-venv)"
  fi
  ok "venv ready: $VENV"
  echo "   installing rknn-toolkit2 2.3.2 + ultralytics (this pulls torch, be patient)"
  "$VENV/bin/pip" install -q --upgrade pip -i "$PYPI_MIRROR"
  "$VENV/bin/pip" install -q -i "$PYPI_MIRROR" \
      'numpy<2' 'rknn-toolkit2==2.3.2' ultralytics onnx onnxslim \
      opencv-python-headless tqdm psutil 'ruamel.yaml' scipy requests fast-histogram \
    || die "dependency install failed. Retry, or check the PyPI mirror is reachable."
  ok "dependencies installed"
fi
PY="$VENV/bin/python"
"$PY" -c 'import rknn, numpy, cv2; print("   rknn/numpy/cv2 imports ok, numpy", numpy.__version__)' \
  || die "toolkit import check failed inside $VENV"
"$PY" -c 'import ultralytics' 2>/dev/null || {
  echo "   ultralytics missing from $VENV, installing (needed for the ONNX re-export)"
  "$VENV/bin/pip" install -q -i "$PYPI_MIRROR" ultralytics || die "ultralytics install failed"
}
ok "ultralytics available"

# ------------------------------------------------------------- 2. weights ----
say "2. weights"
PT="$WORK_DIR/fruits_yolo11s.pt"
if [ -f "$SCRIPT_DIR/fruits_yolo11s.pt" ]; then
  cp -f "$SCRIPT_DIR/fruits_yolo11s.pt" "$PT"
  ok "copied bundled weights -> $PT"
elif [ -s "$PT" ]; then
  ok "weights already present: $PT"
else
  echo "   downloading from $HF (no bundled .pt found)"
  curl -fL --retry 3 --retry-delay 2 -o "$PT.part" \
    "$HF/$MODEL_REPO/resolve/main/my_model_PC/my_model.pt" \
    || die "download failed. Set HF_MIRROR, or drop fruits_yolo11s.pt next to this script."
  mv -f "$PT.part" "$PT"
  ok "downloaded -> $PT"
fi
echo "   size: $(du -h "$PT" | cut -f1)  md5: $(md5sum "$PT" | cut -d' ' -f1)"

# --------------------------------------------------------------- 3. calib ----
say "3. INT8 calibration images"
CALIB="$WORK_DIR/calib_640"
mkdir -p "$CALIB"
have=$(find "$CALIB" -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.png' \) | wc -l | tr -d ' ')
if [ "$have" -ge 8 ]; then
  ok "already have $have calibration images"
elif [ -d "$SCRIPT_DIR/calib_640" ]; then
  cp -f "$SCRIPT_DIR"/calib_640/*.jpg "$CALIB"/ 2>/dev/null || true
  ok "copied bundled calibration set ($(find "$CALIB" -type f | wc -l | tr -d ' ') images)"
else
  echo "   downloading $CALIB_COUNT dataset images from $HF and resizing to 640x640"
  "$PY" - "$HF" "$DATA_REPO" "$CALIB" "$CALIB_COUNT" <<'PYEOF'
import json, os, sys, urllib.request
hf, repo, outdir, limit = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
import cv2
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
tree = json.load(opener.open("%s/api/%s/tree/main/fruitsData/images" % (hf, repo), timeout=60))
names = [e["path"] for e in tree if e.get("type") == "file"]
if not names:
    sys.exit("no images listed in the dataset repo")
step = max(1, len(names) // limit)
picked = names[::step][:limit]
print("   dataset has %d images, taking %d" % (len(names), len(picked)))
done = 0
for path in picked:
    url = "%s/%s/resolve/main/%s" % (hf, repo, path)
    try:
        raw = opener.open(url, timeout=120).read()
    except Exception as exc:
        print("   skip %s (%s)" % (path, exc))
        continue
    import numpy as np
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        continue
    img = cv2.resize(img, (640, 640), interpolation=cv2.INTER_AREA)
    cv2.imwrite(os.path.join(outdir, os.path.basename(path)), img)
    done += 1
    if done % 20 == 0:
        print("   %d/%d" % (done, len(picked)))
print("   wrote %d calibration images" % done)
if done < 8:
    sys.exit("too few calibration images; INT8 quantization needs representative data")
PYEOF
  ok "calibration set ready"
fi

# ------------------------------------------------------------ 4. convert ----
say "4. building RKNN ($DTYPES)"
BUILD="$SCRIPT_DIR/build_fruit_rknn.py"
[ -f "$BUILD" ] || BUILD="$WORK_DIR/build_fruit_rknn.py"
[ -f "$BUILD" ] || die "build_fruit_rknn.py not found next to this script"
cp -f "$BUILD" "$WORK_DIR/build_fruit_rknn.py"

cd "$WORK_DIR"
set +e
"$PY" "$WORK_DIR/build_fruit_rknn.py" \
    --pt "$PT" \
    --calib-dir "$CALIB" \
    --calib-limit "$CALIB_COUNT" \
    --out-dir "$WORK_DIR/out" \
    --platform rk3568 \
    --dtypes "$DTYPES" \
    --name fruits_yolo11s
RC=$?
set -e

# ------------------------------------------------------------- 5. report ----
say "5. result"
if [ "$RC" -ne 0 ]; then
  echo "   conversion exited with code $RC — see the log above."
fi
shopt -s nullglob
found=("$WORK_DIR"/out/*.rknn)
if [ ${#found[@]} -eq 0 ]; then
  die "no .rknn produced. Check out/report.json and the log above."
fi
for f in "${found[@]}"; do
  printf '   %-46s %8s  md5 %s\n' "$(basename "$f")" \
    "$(du -h "$f" | cut -f1)" "$(md5sum "$f" | cut -d' ' -f1)"
done
[ -f "$WORK_DIR/out/report.json" ] && echo "   report: $WORK_DIR/out/report.json"

cat <<EOF

$(printf '\033[1;32mDONE\033[0m') — copy the INT8 model to the board:

  pscp "$WORK_DIR/out/fruits_yolo11s_i8.rknn" linaro@<board-ip>:/home/linaro/ai/models/

then on the board:

  sudo systemctl restart rk3568-fruit
  curl -s --noproxy '*' http://127.0.0.1:8089/api/fruit/status

The fruit service retries loading every 2s, so it picks the model up on its own.
EOF
