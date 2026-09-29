#!/bin/bash
# Deploy the store / scanner / OCR services to the RK3568 board.
#
# Usage:
#   ./deploy_to_board.sh                    # uses the defaults below
#   BOARD=10.181.229.215 ./deploy_to_board.sh
#   ./deploy_to_board.sh --services-only    # skip file upload, just restart units
#
# The board IP changes with the network (handoff section 2), so it is overridable.

set -euo pipefail

BOARD="${BOARD:-10.181.229.215}"
BOARD_USER="${BOARD_USER:-linaro}"
KEY="${KEY:-$HOME/.ssh/id_ed25519_rk3568}"
SSH_OPTS=(-i "$KEY" -o IPQoS=none -o ConnectTimeout=25 -o StrictHostKeyChecking=no -o BatchMode=yes)
TARGET="$BOARD_USER@$BOARD"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES_ONLY=0
if [ "${1:-}" = "--services-only" ]; then
    SERVICES_ONLY=1
fi

say() { echo "[deploy] $*"; }

# ---------------------------------------------------------------- upload
if [ "$SERVICES_ONLY" -eq 0 ]; then
    say "creating target directories on $BOARD"
    ssh "${SSH_OPTS[@]}" "$TARGET" \
        "mkdir -p /home/linaro/ai/store/web /home/linaro/ai/scanner /home/linaro/ai/ocr"

    say "uploading service files"
    # store_ext / store_ext_routes / qr_svg 是 store_service 的扩展层：补的是
    # 原工程 81 个接口里内建没覆盖的那批（会员、RFID、审批、打印队列、顾客端…）。
    # 少任何一个，store_service 会**降级**成只跑内建面板并打印一条日志 ——
    # 不崩，但原工程那套页面全 404。所以这三个跟 store_service.py 同等重要。
    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/store_service.py" \
        "$SRC_DIR/store_ext.py" \
        "$SRC_DIR/store_ext_routes.py" \
        "$SRC_DIR/qr_svg.py" \
        "$SRC_DIR/test_store_e2e.py" \
        "$SRC_DIR/test_store_ext.py" \
        "$SRC_DIR/test_qr_svg.py" \
        "$TARGET:/home/linaro/ai/store/"

    # web/ 是原工程内嵌页面的逐字节副本（tools/extract_legacy_web.py 生成）。
    # 目录没上去的话 h_page_index 会返回 None，`/` 退回内建看板 —— 不会白屏，
    # 但会员/审批/顾客端那些页面全没有。
    say "uploading legacy web pages"
    scp -r "${SSH_OPTS[@]}" \
        "$SRC_DIR/web/." \
        "$TARGET:/home/linaro/ai/store/web/"

    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/scanner_service.py" \
        "$SRC_DIR/test_scanner_decode.py" \
        "$TARGET:/home/linaro/ai/scanner/"

    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/ocr_service.py" \
        "$SRC_DIR/test_ocr_parse.py" \
        "$TARGET:/home/linaro/ai/ocr/"

    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/deploy_model.sh" \
        "$TARGET:/home/linaro/ai/"

    say "installing systemd units"
    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/rk3568-store.service" \
        "$SRC_DIR/rk3568-scanner.service" \
        "$SRC_DIR/rk3568-ocr.service" \
        "$TARGET:/tmp/"

    ssh "${SSH_OPTS[@]}" "$TARGET" "
        sudo install -m 644 /tmp/rk3568-store.service   /etc/systemd/system/ &&
        sudo install -m 644 /tmp/rk3568-scanner.service /etc/systemd/system/ &&
        sudo install -m 644 /tmp/rk3568-ocr.service     /etc/systemd/system/ &&
        chmod 755 /home/linaro/ai/store/store_service.py \
                  /home/linaro/ai/store/store_ext.py \
                  /home/linaro/ai/store/store_ext_routes.py \
                  /home/linaro/ai/store/qr_svg.py \
                  /home/linaro/ai/scanner/scanner_service.py \
                  /home/linaro/ai/ocr/ocr_service.py \
                  /home/linaro/ai/deploy_model.sh &&
        rm -f /tmp/rk3568-*.service &&
        sudo systemctl daemon-reload &&
        echo 'units installed'
    "
fi

# ---------------------------------------------------------------- start
say "enabling and starting services"
ssh "${SSH_OPTS[@]}" "$TARGET" "
    for svc in rk3568-store rk3568-scanner rk3568-ocr; do
        sudo systemctl enable \$svc.service >/dev/null 2>&1 || true
        sudo systemctl restart \$svc.service
    done
    sleep 6
    for svc in rk3568-store rk3568-scanner rk3568-ocr; do
        printf '%-20s %s\n' \"\$svc\" \"\$(systemctl is-active \$svc.service)\"
    done
"

# ---------------------------------------------------------------- verify
say "verifying endpoints"
ssh "${SSH_OPTS[@]}" "$TARGET" "
    for spec in '8094 /api/store/status' '8095 /api/scanner/status' '8096 /api/ocr/status'; do
        set -- \$spec
        printf 'port %s: ' \"\$1\"
        curl -s --noproxy '*' -m 8 \"http://127.0.0.1:\$1\$2\" | head -c 160
        echo
    done
"

# 扩展层有没有真的挂上，必须单独确认 —— 它是**静默降级**的：
# 导入失败时 store_service 照常起来，只是原工程那套页面全 404。
say "verifying the extension layer is mounted"
ssh "${SSH_OPTS[@]}" "$TARGET" "
    printf 'ext status: '
    curl -s --noproxy '*' -m 8 http://127.0.0.1:8094/api/ext/status | head -c 200
    echo
    printf 'legacy index page: '
    curl -s --noproxy '*' -m 8 -o /dev/null -w '%{http_code} %{size_download} bytes' \
        http://127.0.0.1:8094/ ; echo
    printf 'qr-svg: '
    curl -s --noproxy '*' -m 8 -o /dev/null -w '%{http_code}' \
        'http://127.0.0.1:8094/qr-svg?text=deploy-check' ; echo
"

say "done"
