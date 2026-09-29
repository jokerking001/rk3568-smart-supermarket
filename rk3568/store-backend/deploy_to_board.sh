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
        "mkdir -p /home/linaro/ai/store /home/linaro/ai/scanner /home/linaro/ai/ocr"

    say "uploading service files"
    scp "${SSH_OPTS[@]}" \
        "$SRC_DIR/store_service.py" \
        "$SRC_DIR/test_store_e2e.py" \
        "$TARGET:/home/linaro/ai/store/"

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

say "done"
