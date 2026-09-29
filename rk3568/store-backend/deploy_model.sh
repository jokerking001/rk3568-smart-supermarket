#!/bin/bash
# Safe model replacement for the RK3568 vision service.
#
# Handoff section 10.5 requires three things before the live model is touched:
#   1. keep a copy of the old model,
#   2. verify the new one offline on a single image,
#   3. only then restart the vision service.
#
# This script enforces that order and refuses to install a model that fails the
# offline check, so a bad .rknn can never take down the live detection pipeline.
#
# Usage:
#   sudo ./deploy_model.sh /path/to/new_model.rknn
#   sudo ./deploy_model.sh /path/to/new_model.rknn --image /home/linaro/ai/yolo11/i8_camera_out.jpg
#   sudo ./deploy_model.sh --verify-only /path/to/model.rknn
#   sudo ./deploy_model.sh --rollback
#   sudo ./deploy_model.sh --list
#
# Exit codes: 0 success, 1 usage error, 2 validation failed, 3 offline test failed.

set -uo pipefail

MODEL_DIR="${MODEL_DIR:-/home/linaro/ai/models}"
LIVE_MODEL="$MODEL_DIR/yolo11n_i8.rknn"
BACKUP_DIR="$MODEL_DIR/backups"
INFER="${INFER:-/home/linaro/ai/yolo11/yolo11_infer.py}"
TEST_IMAGE="${TEST_IMAGE:-/home/linaro/ai/yolo11/i8_camera_out.jpg}"
SERVICE="${SERVICE:-rk3568-vision.service}"
# The RKNN runtime is installed in the service account's user site-packages
# (~/.local/lib/python3.7/site-packages/rknnlite), NOT system-wide.  Running the
# offline check as root therefore fails with ModuleNotFoundError, so every
# inference is executed as this user with HOME set.
SERVICE_USER="${SERVICE_USER:-linaro}"
WORK_DIR="$(mktemp -d /tmp/model-swap.XXXXXX)"
# root creates WORK_DIR; the service user must be able to write the output files.
chmod 777 "$WORK_DIR"

MODE="install"
CANDIDATE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --verify-only) MODE="verify"; shift ;;
        --rollback)    MODE="rollback"; shift ;;
        --list)        MODE="list"; shift ;;
        --image)       TEST_IMAGE="$2"; shift 2 ;;
        -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
        *)             CANDIDATE="$1"; shift ;;
    esac
done

log()  { echo "[model-swap] $*"; }
fail() { echo "[model-swap] ERROR: $*" >&2; }

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        fail "请用 root 运行（sudo $0 ...）"
        exit 1
    fi
}

check_magic() {
    # Every RKNN container starts with the ASCII magic "RKNN" followed by four
    # zero bytes.  Catching this early gives a clear message instead of the
    # runtime's "Invalid RKNN format" seen with the old mobilenet_ssd.rknn.
    local file="$1"
    local head
    head="$(head -c 8 "$file" | od -An -tx1 | tr -d ' \n')"
    if [ "$head" != "524b4e4e00000000" ]; then
        fail "$file 不是有效的 RKNN 文件（magic=$head，期望 524b4e4e00000000）"
        return 1
    fi
    return 0
}

run_offline_test() {
    # Single-image inference with the candidate model, before it goes live.
    local model="$1"
    local out="$WORK_DIR/verify.jpg"
    local json="$WORK_DIR/verify.json"
    if [ ! -f "$INFER" ]; then
        fail "找不到推理脚本 $INFER"
        return 1
    fi
    if [ ! -f "$TEST_IMAGE" ]; then
        fail "找不到测试图片 $TEST_IMAGE（可用 --image 指定）"
        return 1
    fi
    log "离线单图验证: $(basename "$model") <- $TEST_IMAGE"
    # -H makes sudo set HOME to the target user's home, which is what makes the
    # per-user rknnlite install importable.
    if ! timeout 180 sudo -u "$SERVICE_USER" -H python3 "$INFER" --model "$model" \
            --image "$TEST_IMAGE" --output "$out" --json "$json" \
            > "$WORK_DIR/infer.log" 2>&1; then
        fail "推理失败，日志尾部："
        tail -20 "$WORK_DIR/infer.log" >&2
        return 1
    fi
    if ! python3 - "$json" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as handle:
        data = json.load(handle)
except Exception as exc:
    print("无法解析推理 JSON: %s" % exc)
    sys.exit(1)
dets = data.get("detections")
if dets is None:
    print("推理 JSON 缺少 detections 字段: %s" % list(data)[:10])
    sys.exit(1)
print("离线验证通过：检测到 %d 个目标" % len(dets))
PY
    then
        fail "离线验证未通过，保留原模型"
        return 1
    fi
    return 0
}

do_list() {
    log "当前模型:"
    ls -lh "$LIVE_MODEL" 2>/dev/null || log "  (无)"
    log "备份:"
    if [ -d "$BACKUP_DIR" ]; then
        ls -lh "$BACKUP_DIR" | tail -n +2 || true
    else
        log "  (无备份)"
    fi
}

do_rollback() {
    require_root
    if [ ! -d "$BACKUP_DIR" ]; then
        fail "没有备份目录 $BACKUP_DIR"
        exit 2
    fi
    local latest
    latest="$(ls -1t "$BACKUP_DIR"/*.rknn 2>/dev/null | head -n1 || true)"
    if [ -z "$latest" ]; then
        fail "备份目录中没有 .rknn 文件"
        exit 2
    fi
    log "回滚到 $latest"
    cp -a "$LIVE_MODEL" "$BACKUP_DIR/pre-rollback-$(date +%Y%m%d_%H%M%S).rknn" 2>/dev/null || true
    cp -a "$latest" "$LIVE_MODEL"
    chmod 644 "$LIVE_MODEL"
    systemctl restart "$SERVICE"
    sleep 12
    curl -s --noproxy '*' -m 10 http://127.0.0.1:8088/api/vision/result | head -c 300
    echo
    log "回滚完成"
}

do_install() {
    require_root
    if [ -z "$CANDIDATE" ]; then
        fail "请提供新的 .rknn 文件路径"
        exit 1
    fi
    if [ ! -f "$CANDIDATE" ]; then
        fail "文件不存在: $CANDIDATE"
        exit 1
    fi

    log "1/5 校验 RKNN 文件"
    check_magic "$CANDIDATE" || exit 2

    log "2/5 离线单图验证（不影响线上模型）"
    run_offline_test "$CANDIDATE" || exit 3

    log "3/5 备份当前模型"
    mkdir -p "$BACKUP_DIR"
    if [ -f "$LIVE_MODEL" ]; then
        local stamp backup
        stamp="$(date +%Y%m%d_%H%M%S)"
        backup="$BACKUP_DIR/yolo11n_i8.rknn.$stamp"
        cp -a "$LIVE_MODEL" "$backup"
        log "  已备份 -> $backup"
    else
        log "  线上模型不存在，跳过备份"
    fi

    log "4/5 安装新模型"
    install -m 644 "$CANDIDATE" "$LIVE_MODEL"

    log "5/5 重启视觉服务并检查"
    systemctl restart "$SERVICE"
    sleep 12
    local body
    body="$(curl -s --noproxy '*' -m 12 http://127.0.0.1:8088/api/vision/result || true)"
    if [ -z "$body" ]; then
        fail "视觉服务重启后 API 无响应，正在回滚"
        do_rollback
        exit 3
    fi
    echo "$body" | head -c 400
    echo
    log "模型切换完成。若需回滚: sudo $0 --rollback"
}

case "$MODE" in
    list)     do_list ;;
    rollback) do_rollback ;;
    verify)
        if [ -z "$CANDIDATE" ]; then fail "请提供 .rknn 路径"; exit 1; fi
        check_magic "$CANDIDATE" || exit 2
        run_offline_test "$CANDIDATE" || exit 3
        log "验证通过（未安装）"
        ;;
    install)  do_install ;;
esac
