#!/bin/bash
# ============================================================
#  智慧超市 HDMI 大屏 —— kiosk 启动脚本
# ============================================================
#  板子接 HDMI 显示器后，开机自动全屏显示 8094 的促销大屏
#  （http://127.0.0.1:8094/bigscreen）。
#
#  为什么需要这个脚本，而不是在自启里直接写一条 chromium 命令：
#    1. 8094 是 systemd 服务，开机时未必已就绪 —— 浏览器先开就会停在
#       错误页。这里先轮询等它就绪再开浏览器。
#    2. LXDE 默认带 xscreensaver + DPMS，闲置十分钟会黑屏 ——
#       大屏必须常亮，所以显式关掉。
#    3. chromium 万一崩了要能自动拉起来，否则现场得有人去重启板子。
#    4. 缓存丢到 /dev/shm（内存盘），少写 eMMC，也省根分区空间
#       （根分区只有 1.4 GB 可用）。
#
#  安装：由 install_kiosk.sh 复制到 /home/linaro/ai/kiosk/ 并挂进
#        ~/.config/autostart/bigscreen-kiosk.desktop
#
#  手动跑（调试用）：
#        DISPLAY=:0 XAUTHORITY=/home/linaro/.Xauthority \
#          bash /home/linaro/ai/kiosk/start-kiosk.sh
# ============================================================
set -u

KIOSK_DIR="/home/linaro/ai/kiosk"
URL="${KIOSK_URL:-http://127.0.0.1:8094/bigscreen}"
LOG="${KIOSK_DIR}/kiosk.log"
PROFILE="${KIOSK_DIR}/profile"
CACHE="/dev/shm/chromium-kiosk-cache"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/linaro/.Xauthority}"

mkdir -p "$KIOSK_DIR" "$PROFILE" "$CACHE" 2>/dev/null

log() { echo "[$(date '+%F %T')] $*" >>"$LOG"; }

log "=== kiosk 启动，URL=$URL DISPLAY=$DISPLAY ==="

# ---- 1. 等 8094 就绪（最多 60 秒）----
ready=0
for i in $(seq 1 60); do
  if curl -s --noproxy '*' -m 2 -o /dev/null "http://127.0.0.1:8094/"; then
    ready=1
    log "8094 已就绪（等待 ${i}s）"
    break
  fi
  sleep 1
done
[ "$ready" = "1" ] || log "!! 8094 等了 60s 还没起来，仍然尝试打开浏览器"

# ---- 1.5 分辨率对齐到显示器原生模式 ----
# 为什么：X 默认会落在 1280x720，而这块屏原生是 1366x768（EDID 首选时序），
# 差 86 列会被拉伸缩放，字发虚。xrandr 里带 "+" 的就是当前屏的原生模式。
# 故意不写死分辨率：换个显示器也能自适应；真认不出来就保持现状不动。
OUT="$(xrandr 2>/dev/null | awk '/ connected/ {print $1; exit}')"
if [ -n "$OUT" ]; then
  MODE="$(xrandr 2>/dev/null | awk -v o="$OUT" '
    $1 == o && / connected/ { f = 1; next }
    f && /^[^ \t]/          { exit }
    f { for (i = 1; i <= NF; i++) if ($i ~ /\+/) { print $1; exit } }')"
  if [ -n "$MODE" ]; then
    if xrandr --output "$OUT" --mode "$MODE" 2>/dev/null; then
      log "分辨率已对齐：$OUT $MODE"
    else
      log "!! 设置 $OUT $MODE 失败，保持默认"
    fi
  else
    log "!! 没找到 $OUT 的原生模式，保持默认"
  fi
else
  log "!! 没检测到已连接的显示输出"
fi

# ---- 2. 关屏保 / DPMS（不关的话闲置会黑屏）----
xset s off       2>/dev/null && log "xset s off OK"       || log "!! xset s off 失败"
xset -dpms       2>/dev/null && log "xset -dpms OK"       || log "!! xset -dpms 失败"
xset s noblank   2>/dev/null

# ---- 3. 循环守护：chromium 退出就重新拉起 ----
while true; do
  log "启动 chromium --kiosk"
  /usr/bin/chromium \
    --kiosk \
    --user-data-dir="$PROFILE" \
    --disk-cache-dir="$CACHE" \
    --no-first-run \
    --no-default-browser-check \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-translate \
    --disable-features=Translate,TranslateUI \
    --autoplay-policy=no-user-gesture-required \
    --check-for-update-interval=31536000 \
    --overscroll-history-navigation=0 \
    --touch-events=enabled \
    --window-position=0,0 \
    "$URL" >>"$LOG" 2>&1
  rc=$?
  log "chromium 退出，rc=$rc，3 秒后重启"
  sleep 3
done
