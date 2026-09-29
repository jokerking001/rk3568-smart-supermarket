#!/usr/bin/env bash
#
# 04_pack_rknn_bundle.sh — 把训练产物打包成「转换机一键包」
#
# 在**本机**（Windows / Git Bash）运行。产出的包整个拷到 RKNN 转换机
# （Ubuntu x86_64）上，跑一条命令就能出 .rknn。
#
#   bash 04_pack_rknn_bundle.sh --pt runs/fruit8/weights/best.pt
#   bash 04_pack_rknn_bundle.sh --pt runs/fruit8/weights/best.pt \
#                               --onnx artifacts/fruit8_yolo11n.onnx
#   bash 04_pack_rknn_bundle.sh --pt ... --allow-missing-calib   # 没校准图也打包
#
# 包结构：
#   dist/<name>_rknn_vm_bundle/
#     vm_build_<name>_rknn.sh   ← 转换机上跑这个
#     build_fruit_rknn.py       ← 实际转换脚本
#     <name>.pt
#     <name>.onnx               （如果给了 --onnx）
#     calib_src/                ← INT8 校准图（原始尺寸，VM 上缩到 640）
#     classes.txt               ← 类别顺序，板端对表用
#     bundle.env                ← 给 VM 脚本的默认值
#     MANIFEST.txt              ← 文件清单 + md5，拷完对一遍
#     README_VM.md
#
# 为什么不让本机做缩放：本机没装 cv2/PIL（也不该为一个打包脚本装），
# 而 VM 上装了 rknn-toolkit2 就一定有 cv2。所以校准图按原尺寸带过去。
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PT=""
ONNX=""
NAME="fruit8_yolo11n"
CALIB_DIR="$HERE/dataset/fruit8/images/val"
CALIB_COUNT="80"
OUT_ROOT="$HERE/dist"
DATA_YAML="$HERE/dataset/fruit8/data.yaml"
RULES="$HERE/../rk3568-fruit-fusion/fruit_rules.json"
BUILD_SCRIPT=""
VM_SCRIPT="$HERE/04_vm_build_fruit8_rknn.sh"
ALLOW_MISSING_CALIB=0
SKIP_CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --pt)            PT="$2"; shift 2 ;;
    --onnx)          ONNX="$2"; shift 2 ;;
    --name)          NAME="$2"; shift 2 ;;
    --calib-dir)     CALIB_DIR="$2"; shift 2 ;;
    --calib-count)   CALIB_COUNT="$2"; shift 2 ;;
    --out-dir)       OUT_ROOT="$2"; shift 2 ;;
    --data)          DATA_YAML="$2"; shift 2 ;;
    --rules)         RULES="$2"; shift 2 ;;
    --build-script)  BUILD_SCRIPT="$2"; shift 2 ;;
    --vm-script)     VM_SCRIPT="$2"; shift 2 ;;
    --allow-missing-calib) ALLOW_MISSING_CALIB=1; shift ;;
    --skip-check)    SKIP_CHECK=1; shift ;;
    -h|--help)       sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

PY="${PYTHON:-python}"
command -v "$PY" >/dev/null || die "找不到 python，设 PYTHON=<解释器路径> 再跑"
# 本机 python 只用来读 data.yaml / 算 md5，不装任何依赖
"$PY" -c 'import sys; print(sys.version.split()[0])' >/dev/null 2>&1 || die "python 跑不起来"

# Git Bash 下把 POSIX 路径转成 Windows 能认的形式。
# 本机若设了 MSYS_NO_PATHCONV=1，/f/... 这种路径会被 Windows 解释成
# 「当前盘根目录下的 f\...」，python 直接报 No such file。所以显式转。
win() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s' "$1"
  fi
}

BUNDLE="$OUT_ROOT/${NAME}_rknn_vm_bundle"

# ───────────────────────────────────────────────────────── 0. 输入检查 ──
say "0. 输入检查"
if [ -z "$PT" ]; then
  die "必须给 --pt（训练产出的 best.pt）。例如：--pt runs/fruit8/weights/best.pt"
fi
[ -f "$PT" ] || die "找不到权重：$PT"
ok "权重：$PT  ($(du -h "$PT" | cut -f1))"

if [ -n "$ONNX" ]; then
  [ -f "$ONNX" ] || die "找不到 ONNX：$ONNX"
  ok "ONNX：$ONNX  ($(du -h "$ONNX" | cut -f1))"
fi

[ -f "$VM_SCRIPT" ] || die "找不到 VM 侧脚本：$VM_SCRIPT"
ok "VM 脚本：$VM_SCRIPT"

if [ -z "$BUILD_SCRIPT" ]; then
  for candidate in \
      "$HERE/../../2026-09-12/rk3568-fruit-model/convert/build_fruit_rknn.py" \
      "$HERE/../../2026-09-12/rk3568-fruit-model/dist/fruit_rknn_vm_bundle/build_fruit_rknn.py" \
      "$HERE/build_fruit_rknn.py"; do
    if [ -f "$candidate" ]; then
      BUILD_SCRIPT="$candidate"
      break
    fi
  done
fi
if [ -z "$BUILD_SCRIPT" ] || [ ! -f "$BUILD_SCRIPT" ]; then
  echo "   找过这些位置：" >&2
  echo "     $HERE/../../2026-09-12/rk3568-fruit-model/convert/build_fruit_rknn.py" >&2
  echo "     $HERE/../../2026-09-12/rk3568-fruit-model/dist/fruit_rknn_vm_bundle/build_fruit_rknn.py" >&2
  echo "     $HERE/build_fruit_rknn.py" >&2
  die "找不到 build_fruit_rknn.py，用 --build-script 指定"
fi
ok "转换脚本：$BUILD_SCRIPT"

# ─────────────────────────────────────────────── 1. 类别一致性（关键） ──
say "1. 类别一致性校验"
if [ "$SKIP_CHECK" = "1" ]; then
  warn "--skip-check：跳过。类别名对不上时融合会静默失效，别习惯性跳过。"
elif [ -f "$DATA_YAML" ] && [ -f "$RULES" ]; then
  "$PY" "$(win "$HERE/05_check_labels.py")" --yaml "$(win "$DATA_YAML")" --rules "$(win "$RULES")" \
    || die "类别名和融合规则对不上。先改 01_prepare_dataset.py 的 TARGET_CLASSES 再重跑。"
else
  warn "缺 $DATA_YAML 或 $RULES，跳过了校验。"
  warn "这两个文件齐了之后一定要补跑：python 05_check_labels.py"
fi

# ───────────────────────────────────────────────────────── 2. 组装包 ──
say "2. 组装包"
if [ -d "$BUNDLE" ]; then
  echo "   清掉旧包：$BUNDLE"
  rm -rf "$BUNDLE"
fi
mkdir -p "$BUNDLE/calib_src"
ok "包目录：$BUNDLE"

cp -f "$PT" "$BUNDLE/$NAME.pt"
cp -f "$VM_SCRIPT" "$BUNDLE/vm_build_${NAME}_rknn.sh"
chmod +x "$BUNDLE/vm_build_${NAME}_rknn.sh"
cp -f "$BUILD_SCRIPT" "$BUNDLE/build_fruit_rknn.py"
ok "已放入 .pt / VM 脚本 / 转换脚本"

if [ -n "$ONNX" ]; then
  cp -f "$ONNX" "$BUNDLE/$NAME.onnx"
  ok "已放入 .onnx"
fi

# classes.txt：优先从 data.yaml 推，退而求其次从 ONNX 里的名字推不了，就报错
if [ -f "$DATA_YAML" ]; then
  "$PY" "$(win "$HERE/05_check_labels.py")" --yaml "$(win "$DATA_YAML")" --rules "$(win "$RULES")" \
      --write-classes "$(win "$BUNDLE/classes.txt")" >/dev/null \
    || warn "写 classes.txt 时校验没过（前面已经报过了）"
fi
if [ -f "$BUNDLE/classes.txt" ]; then
  CLASSES="$(paste -sd, "$BUNDLE/classes.txt")"
  ok "classes.txt：$CLASSES"
else
  CLASSES=""
  warn "没有 classes.txt —— 板端就对不了表了。确认 dataset/fruit8/data.yaml 存在。"
fi

# ───────────────────────────────────────────────────────── 3. 校准图 ──
say "3. INT8 校准图"
if [ -d "$CALIB_DIR" ]; then
  total=0
  while IFS= read -r img; do
    [ "$total" -ge "$CALIB_COUNT" ] && break
    cp -f "$img" "$BUNDLE/calib_src/" 2>/dev/null || true
    total=$((total + 1))
  done < <(find "$CALIB_DIR" -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' \) | sort)
  if [ "$total" -ge 8 ]; then
    ok "带了 $total 张校准图（来自 $CALIB_DIR）"
  else
    warn "只找到 $total 张（< 8），INT8 量化会不靠谱"
    [ "$ALLOW_MISSING_CALIB" = "1" ] || die "校准图不足。补图，或加 --allow-missing-calib 硬打包。"
  fi
else
  warn "校准图目录不存在：$CALIB_DIR"
  [ "$ALLOW_MISSING_CALIB" = "1" ] || die "没有校准图。用 --calib-dir 指定，或加 --allow-missing-calib 硬打包。"
fi

# ────────────────────────────────────────────────────── 4. 元数据文件 ──
say "4. 元数据"
cat > "$BUNDLE/bundle.env" <<EOF
# 由 04_pack_rknn_bundle.sh 生成，VM 脚本会自动 source 它。
NAME="$NAME"
CALIB_COUNT="$CALIB_COUNT"
CLASSES="$CLASSES"
EOF
ok "bundle.env"

{
  echo "# $NAME RKNN 转换包 — 文件清单"
  echo "# 打包时间：$(date '+%Y-%m-%d %H:%M:%S')"
  echo "# 用法：拷到 VM 后先比对 md5，再跑 vm_build_${NAME}_rknn.sh"
  echo ""
  echo "| 文件 | 大小 | md5 |"
  echo "| --- | --- | --- |"
  for f in "$BUNDLE/$NAME.pt" "$BUNDLE/$NAME.onnx" "$BUNDLE/build_fruit_rknn.py" \
           "$BUNDLE/vm_build_${NAME}_rknn.sh" "$BUNDLE/classes.txt" "$BUNDLE/bundle.env"; do
    [ -f "$f" ] || continue
    printf '| %s | %s | %s |\n' "$(basename "$f")" \
      "$(du -h "$f" | cut -f1)" "$(md5sum "$f" | cut -d' ' -f1)"
  done
  echo ""
  echo "校准图 $(find "$BUNDLE/calib_src" -type f | wc -l | tr -d ' ') 张，"
  echo "合并校验："
  find "$BUNDLE/calib_src" -type f | sort | xargs md5sum | md5sum | awk '{print "  calib_src 汇总 md5: " $1}'
} > "$BUNDLE/MANIFEST.txt"
ok "MANIFEST.txt"

cat > "$BUNDLE/README_VM.md" <<EOF
# ${NAME} → RKNN 转换包（转换机侧，一条命令）

这个包给 **RKNN 转换机**（Ubuntu x86_64，例如 \`zwb@192.168.190.160\`）。
板端（RK3568）不需要这个包，板端只要最后产出的 \`.rknn\`。

## 为什么必须在 x86_64 Linux 上转

RKNN-Toolkit2 只提供 Linux x86_64 的 wheel。板端是 aarch64 装不了，
Windows 没有对应 wheel，本机 WSL 被安全策略拦。所以只能在这台 VM 上转。

## 包内容

| 文件 | 说明 |
| --- | --- |
| \`vm_build_${NAME}_rknn.sh\` | 一键入口：建环境 → 备校准集 → 转换 → 报告 |
| \`build_fruit_rknn.py\` | 实际转换脚本（可单独调用） |
| \`${NAME}.pt\` | 训练产出的最佳权重 |
| \`${NAME}.onnx\` | 导出的 ONNX（如果打包时带了；不带也能现场从 .pt 重导） |
| \`calib_src/\` | INT8 校准图（原始尺寸，脚本会缩到 640×640） |
| \`classes.txt\` | 类别顺序，板端对表用 |
| \`bundle.env\` | 默认参数 |
| \`MANIFEST.txt\` | 文件清单 + md5 |

包内自带权重和校准图，**全程可离线**。

## 跑起来

\`\`\`bash
cd <包所在目录>
bash vm_build_${NAME}_rknn.sh
\`\`\`

只预检、不动任何东西：

\`\`\`bash
bash vm_build_${NAME}_rknn.sh --check-only
\`\`\`

只出 INT8（省一半时间）：

\`\`\`bash
bash vm_build_${NAME}_rknn.sh --dtypes i8
\`\`\`

## 脚本会做什么

1. 环境检查 — x86_64 / python 版本 / python3-venv
2. 虚拟环境 — 优先复用 \`~/venvs/rknn-toolkit2\`，没有就新建并装
   \`rknn-toolkit2==2.3.2\` + \`ultralytics\` + \`numpy<2\`
3. 权重 — 用包内的 \`.pt\`
4. 校准集 — \`calib_src/\` 缩到 640×640
5. 转换 — 从 \`.pt\` 重导 ONNX → 校验输出布局 → 出 INT8 / FP 两个 \`.rknn\`
6. 报告 — 大小 + md5 + 推送命令

## 一个关键坑：ONNX 输出层数

板端 \`yolo11_infer.py\` 的后处理按 **9 输出**写死
（\`pair = len(outputs) // 3\`，每尺度 box_dfl / class_scores / score_sum）。
OAK / Luxonis 那种 \`output1..3_yolov6r2\` 只有 **3 输出**，喂进去直接 IndexError。

所以脚本默认**从 \`.pt\` 用 ultralytics 重导一次**（标准单输出图），
由 RKNN 自己切成 9 输出 —— 板端代码一行都不用改。
\`--no-export\` 只在包内 ONNX 确认是单输出时才用。

## 产出

\`\`\`
~/fruit8_rknn/out/${NAME}_i8.rknn   ← 板端要的就是这个
~/fruit8_rknn/out/${NAME}_fp.rknn
~/fruit8_rknn/out/report.json       ← 布局校验 + 运行时输出形状
\`\`\`

## 推到板端

\`\`\`bash
pscp ~/fruit8_rknn/out/${NAME}_i8.rknn linaro@<板端IP>:/home/linaro/ai/models/${NAME}.rknn
\`\`\`

板端 \`rk3568-fruit.service\` 每 2 秒重试加载，文件到位后自动生效：

\`\`\`bash
curl -s --noproxy '*' http://127.0.0.1:8089/api/fruit/status
\`\`\`

看到 \`"model_loaded": true\` 就成了。

## 板端还要对一次表（别跳过）

模型输出的类别名必须和融合规则 \`fruit_rules.json\` 里的 \`label\` 逐字一致。
名字对不上时融合引擎取到概率 0，**不报错、不告警**，表现就是
「这个水果怎么都识别不了」。

\`\`\`bash
python3 05_check_labels.py --classes /home/linaro/ai/models/${NAME}.classes.txt
\`\`\`

类别：${CLASSES:-（见 classes.txt）}

## 排查

| 现象 | 原因 / 处理 |
| --- | --- |
| \`RKNN-Toolkit2 only runs on x86_64\` | 跑错机器了 |
| \`venv 创建失败\` | \`sudo apt install -y python3-venv python3-pip\` |
| 装依赖卡住 | 换镜像：\`PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple bash ...\` |
| \`没有校准图\` / 精度差 | 放更多真实场景水果照片进 \`calib_src/\` 再跑（\`--force-calib\`） |
| \`IndexError\` / 输出数不对 | ONNX 布局问题，别加 \`--no-export\`，让它从 \`.pt\` 重导 |
| 板端识别不到某个水果 | 先跑 \`05_check_labels.py\`，十有八九是类别名不一致 |
EOF
ok "README_VM.md"

# ───────────────────────────────────────────────────────── 5. 汇总 ──
say "5. 完成"
echo "   包：$BUNDLE"
echo ""
find "$BUNDLE" -maxdepth 1 -type f -printf '   %-40f %8s\n' 2>/dev/null \
  || ls -la "$BUNDLE"
echo "   校准图：$(find "$BUNDLE/calib_src" -type f | wc -l | tr -d ' ') 张"
echo ""
cat <<EOF
拷到转换机（VMware 拖拽 / 共享目录 / pscp -r 都行），然后：

  bash vm_build_${NAME}_rknn.sh --check-only     # 先预检
  bash vm_build_${NAME}_rknn.sh                  # 再转换
EOF
