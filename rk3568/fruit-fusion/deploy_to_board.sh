#!/bin/bash
# Deploy the fruit vision+weight fusion service (8099) to the RK3568 board.
#
# Usage:
#   ./deploy_to_board.sh                    # 用下面的默认值
#   BOARD=10.176.240.215 ./deploy_to_board.sh
#   ./deploy_to_board.sh --services-only    # 跳过上传，只重装 unit 并重启
#   ./deploy_to_board.sh --skip-tests       # 不在板端跑那 268 项自测
#
# 为什么单独一个脚本（而不是并进 store-backend/deploy_to_board.sh）：
# 8099 是新加的服务，跟 8094/8095/8096 那批的依赖完全不同 —— 它要读 8089 的
# 识别结果，还要读 8094 的购物车接口。项目里 VLM 也是自己一个部署脚本，
# 这里沿用同样的惯例：**一个模块一个部署脚本**。
#
# 板端 IP 随网络变（热点一换就变），所以做成可覆盖的，不要写死。

set -euo pipefail

BOARD="${BOARD:-10.176.240.215}"
BOARD_USER="${BOARD_USER:-linaro}"
KEY="${KEY:-$HOME/.ssh/id_ed25519_rk3568}"
SSH_OPTS=(-i "$KEY" -o IPQoS=none -o ConnectTimeout=25 -o StrictHostKeyChecking=no -o BatchMode=yes)
TARGET="$BOARD_USER@$BOARD"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/home/linaro/ai/fruit-fusion"
MODEL_PATH="/home/linaro/ai/models/fruit8_yolo11n_i8.rknn"

SERVICES_ONLY=0
RUN_TESTS=1
for arg in "$@"; do
    case "$arg" in
        --services-only) SERVICES_ONLY=1 ;;
        --skip-tests)    RUN_TESTS=0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

say() { echo "[deploy] $*"; }

# ---------------------------------------------------------------- upload
if [ "$SERVICES_ONLY" -eq 0 ]; then
    say "creating $REMOTE_DIR on $BOARD"
    ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p $REMOTE_DIR"

    say "uploading service files"
    # fruit_rules.json 必须一起上去：fruit_fusion.py 从**自己所在目录**加载它，
    # 缺了不会报错，会静默回落默认值 —— 那种"看着在跑其实规则不对"最难查。
    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/fruit_fusion.py" \
        "$SRC_DIR/fruit_fusion_service.py" \
        "$SRC_DIR/fruit_fusion_bridge.py" \
        "$SRC_DIR/vision_observer.py" \
        "$SRC_DIR/fruit_rules.json" \
        "$SRC_DIR/README.md" \
        "$TARGET:$REMOTE_DIR/"

    say "uploading self-tests"
    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/test_fruit_fusion.py" \
        "$SRC_DIR/test_vision_observer.py" \
        "$SRC_DIR/test_fruit_fusion_service.py" \
        "$SRC_DIR/test_fruit_fusion_bridge.py" \
        "$TARGET:$REMOTE_DIR/"

    say "installing systemd units"
    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/deploy/rk3568-fruit-fusion.service" \
        "$SRC_DIR/deploy/rk3568-fruit.service" \
        "$TARGET:/tmp/"

    ssh "${SSH_OPTS[@]}" "$TARGET" "
        sudo install -m 644 /tmp/rk3568-fruit-fusion.service /etc/systemd/system/ &&
        sudo install -m 644 /tmp/rk3568-fruit.service        /etc/systemd/system/ &&
        chmod 755 $REMOTE_DIR/fruit_fusion_service.py &&
        rm -f /tmp/rk3568-fruit*.service &&
        sudo systemctl daemon-reload &&
        echo 'units installed'
    "
fi

# ------------------------------------------------- 8089 的重启闸门（重要）
# README 里写死了这条：模型没到位就重启 8089，它会一直报 model_loaded: false，
# 而且比不重启更难查（服务是活的，就是不干活）。
# 所以这里先探模型文件在不在，不在就**只装 unit、不重启**，并明确说出来。
say "checking whether the 8-class RKNN model is in place"
if ssh "${SSH_OPTS[@]}" "$TARGET" "test -f $MODEL_PATH"; then
    MODEL_READY=1
    say "model found at $MODEL_PATH —— 可以重启 8089"
else
    MODEL_READY=0
    say "⚠ 模型不在 $MODEL_PATH"
    say "  → 8089 保持现状不动（重启了只会变成 model_loaded=false，更难查）"
    say "  → 模型要从转换机出，转换机口令那个老问题见 docs/交接文档-v2.md §14.5"
fi

# ---------------------------------------------------------------- start
say "enabling and starting rk3568-fruit-fusion"
ssh "${SSH_OPTS[@]}" "$TARGET" "
    sudo systemctl enable rk3568-fruit-fusion.service >/dev/null 2>&1 || true
    sudo systemctl restart rk3568-fruit-fusion.service
    if [ $MODEL_READY -eq 1 ]; then
        sudo systemctl restart rk3568-fruit.service
        sleep 8
        printf '%-26s %s\n' 'rk3568-fruit' \"\$(systemctl is-active rk3568-fruit.service)\"
    else
        printf '%-26s %s\n' 'rk3568-fruit' \"\$(systemctl is-active rk3568-fruit.service) （未重启）\"
    fi
    sleep 4
    printf '%-26s %s\n' 'rk3568-fruit-fusion' \"\$(systemctl is-active rk3568-fruit-fusion.service)\"
"

# ---------------------------------------------------------------- tests
if [ "$RUN_TESTS" -eq 1 ]; then
    say "running the 268 board-side self-tests"
    ssh "${SSH_OPTS[@]}" "$TARGET" "
        cd $REMOTE_DIR
        for t in test_fruit_fusion.py test_vision_observer.py \
                 test_fruit_fusion_service.py test_fruit_fusion_bridge.py; do
            line=\$(python3 \$t 2>&1 | tail -1)
            printf '%-32s %s\n' \"\$t\" \"\$line\"
        done
    "
fi

# ---------------------------------------------------------------- verify
say "verifying endpoints"
ssh "${SSH_OPTS[@]}" "$TARGET" "
    printf '8099 /api/fusion/status: '
    curl -s --noproxy '*' -m 8 http://127.0.0.1:8099/api/fusion/status | head -c 260
    echo
    printf '8099 /health:            '
    curl -s --noproxy '*' -m 8 http://127.0.0.1:8099/health | head -c 120
    echo
    printf '8089 /api/fruit/status:  '
    curl -s --noproxy '*' -m 8 http://127.0.0.1:8089/api/fruit/status | head -c 160
    echo
    if [ $MODEL_READY -eq 0 ]; then
        echo '  ↑ model_loaded 仍会是 false —— 模型没到位，不是服务的问题'
    fi
"

say "done"
