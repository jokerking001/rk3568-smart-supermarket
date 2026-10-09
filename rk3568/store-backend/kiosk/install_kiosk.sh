#!/bin/bash
# ============================================================
#  在 RK3568 板端安装 / 卸载 HDMI 大屏 kiosk
# ============================================================
#  用法（在**板子上**执行，需要 sudo）：
#      sudo bash install_kiosk.sh            # 安装 + 立刻启动
#      sudo bash install_kiosk.sh --no-start # 只装，不立刻启动
#      sudo bash install_kiosk.sh --uninstall
#
#  做四件事：
#    1. 把 start-kiosk.sh / bigscreen-kiosk.desktop 放到 /home/linaro/ai/kiosk/
#    2. 在 linaro 的 ~/.config/autostart/ 挂一个自启项（开机自动全屏）
#    3. 检查并处理会挡住大屏/导致黑屏的东西（xscreensaver）
#    4. 立刻在 :0 上拉起来（不用重启就能看到效果）
# ============================================================
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="linaro"
HOME_DIR="/home/${USER_NAME}"
KIOSK_DIR="${HOME_DIR}/ai/kiosk"
AUTOSTART_DIR="${HOME_DIR}/.config/autostart"
DESKTOP_NAME="bigscreen-kiosk.desktop"

DO_START=1
DO_UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-start)  DO_START=0 ;;
    --uninstall) DO_UNINSTALL=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

as_user() { sudo -u "$USER_NAME" -H "$@"; }

# ---------------------------------------------------------------- 卸载
if [ "$DO_UNINSTALL" = "1" ]; then
  echo "== 卸载 kiosk =="
  pkill -f 'start-kiosk.sh' 2>/dev/null || true
  pkill -f 'chromium.*8094/bigscreen' 2>/dev/null || true
  rm -f "${AUTOSTART_DIR}/${DESKTOP_NAME}"
  echo "  已移除自启项；脚本保留在 ${KIOSK_DIR}（要一并删就 rm -rf）"
  exit 0
fi

# ---------------------------------------------------------------- 安装
echo "== 1. 安装文件到 ${KIOSK_DIR} =="
install -d -o "$USER_NAME" -g "$USER_NAME" "$KIOSK_DIR"
install -o "$USER_NAME" -g "$USER_NAME" -m 755 \
        "${HERE}/start-kiosk.sh" "${KIOSK_DIR}/start-kiosk.sh"
install -d -o "$USER_NAME" -g "$USER_NAME" "$AUTOSTART_DIR"
install -o "$USER_NAME" -g "$USER_NAME" -m 644 \
        "${HERE}/${DESKTOP_NAME}" "${AUTOSTART_DIR}/${DESKTOP_NAME}"
echo "  ${KIOSK_DIR}/start-kiosk.sh"
echo "  ${AUTOSTART_DIR}/${DESKTOP_NAME}"

echo
echo "== 2. 检查会干扰大屏的东西 =="
if pgrep -x xscreensaver >/dev/null 2>&1; then
  echo "  !! xscreensaver 在跑 —— 它会覆盖 xset，导致闲置黑屏。已杀掉。"
  pkill -x xscreensaver 2>/dev/null || true
  # 并从 LXDE 自启里注释掉，否则重启又回来
  LXDE_AUTOSTART="${HOME_DIR}/.config/lxsession/LXDE/autostart"
  if [ -f "$LXDE_AUTOSTART" ] && grep -q '^@xscreensaver' "$LXDE_AUTOSTART"; then
    cp -a "$LXDE_AUTOSTART" "${LXDE_AUTOSTART}.bak-$(date +%Y%m%d-%H%M%S)"
    sed -i 's|^@xscreensaver|#@xscreensaver|' "$LXDE_AUTOSTART"
    echo "  已在 LXDE autostart 里注释掉 @xscreensaver（原文件已备份）"
  fi
else
  echo "  xscreensaver 没在跑，OK"
fi

# 屏幕保护相关的 X 扩展
if command -v xset >/dev/null 2>&1; then
  echo "  提示：start-kiosk.sh 每次启动都会执行 xset s off -dpms"
fi

echo
echo "== 3. 当前 HDMI / X 状态 =="
for c in /sys/class/drm/card*/card*-HDMI*/status; do
  [ -e "$c" ] && echo "  $c = $(cat "$c")"
done
as_user env DISPLAY=:0 XAUTHORITY="${HOME_DIR}/.Xauthority" \
        xrandr 2>/dev/null | head -2 || echo "  (xrandr 读不到，可能 X 没起)"

if [ "$DO_START" = "1" ]; then
  echo
  echo "== 4. 立刻启动（不用重启）=="
  pkill -f 'start-kiosk.sh' 2>/dev/null || true
  sleep 1
  as_user env DISPLAY=:0 XAUTHORITY="${HOME_DIR}/.Xauthority" \
          nohup setsid /bin/bash "${KIOSK_DIR}/start-kiosk.sh" \
          >/dev/null 2>&1 &
  sleep 8
  if pgrep -f 'start-kiosk.sh' >/dev/null; then
    echo "  ✅ kiosk 进程已起来"
    pgrep -af 'chromium.*kiosk' | head -3
  else
    echo "  !! 没起来，看日志：${KIOSK_DIR}/kiosk.log"
    tail -20 "${KIOSK_DIR}/kiosk.log" 2>/dev/null
  fi
fi

echo
echo "完成。日志：${KIOSK_DIR}/kiosk.log"
echo "下次开机（linaro 自动登录 LXDE）会自动全屏打开大屏。"
