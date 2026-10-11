#!/bin/bash
# ============================================================
#  桌面层加固：关掉「顾客能点到 kiosk 之外」的口子
# ============================================================
#  kiosk 虽然用 chromium --kiosk 全屏，但底下的 X 桌面还活着：
#    · openbox 有 30 条快捷键（C-A-Left 切桌面、A-Tab 切窗口…）
#    · lxpanel 是任务栏；pcmanfm 管桌面图标
#  chromium 崩溃时守护循环有 3 秒空窗，或有人接上键盘，
#  这些口子就能把顾客带出收银界面。
#
#  做两件事（先备份，可回滚）：
#    1. 注释 lxsession autostart 里的 lxpanel / pcmanfm
#    2. 把 openbox lxde-rc.xml 的 <keybind> 全部注释
#
#  用法：
#    bash harden-desktop.sh            # 加固
#    bash harden-desktop.sh --restore  # 回滚
#    bash harden-desktop.sh --status   # 只看现状，不改动
#
#  生效时机：
#    · openbox 快捷键 —— 立即（脚本末尾 openbox --reconfigure）
#    · lxpanel/pcmanfm —— 下次登录（不影响当前会话）
# ============================================================
set -u

AUTOSTART="$HOME/.config/lxsession/LXDE/autostart"
RC="$HOME/.config/openbox/lxde-rc.xml"
BK="$HOME/.kiosk-harden-backup"

log() { echo "[harden] $*"; }

do_status() {
  echo "--- 现状 ---"
  echo "autostart 里仍启用的 lxpanel/pcmanfm："
  grep -nE '^@(lxpanel|pcmanfm)' "$AUTOSTART" 2>/dev/null || echo "  (无 —— 已禁用或文件不存在)"
  printf "openbox 生效中的 keybind 条数 : "
  grep -cE '^[[:space:]]*<keybind' "$RC" 2>/dev/null || echo 0
  printf "openbox 已注释的 keybind 条数 : "
  grep -c '<!--keybind' "$RC" 2>/dev/null || echo 0
}

do_restore() {
  [ -d "$BK" ] || { echo "没有备份目录 $BK，无法回滚"; exit 1; }
  [ -f "$BK/autostart" ]    && cp -f "$BK/autostart"    "$AUTOSTART" 2>/dev/null && log "已还原 autostart"
  [ -f "$BK/lxde-rc.xml" ]  && cp -f "$BK/lxde-rc.xml"  "$RC"        2>/dev/null && log "已还原 lxde-rc.xml"
  openbox --reconfigure 2>/dev/null && log "openbox 已重载"
  echo
  do_status
}

case "${1:-}" in
  --status)  do_status; exit 0 ;;
  --restore) do_restore; exit 0 ;;
esac

mkdir -p "$BK"
[ -f "$AUTOSTART" ] && cp -f "$AUTOSTART" "$BK/autostart"   2>/dev/null
[ -f "$RC" ]        && cp -f "$RC"        "$BK/lxde-rc.xml" 2>/dev/null
log "已备份到 $BK（回滚：bash $0 --restore）"

# ---- 1. 禁 lxpanel（任务栏）/ pcmanfm（桌面图标）----
if [ -f "$AUTOSTART" ]; then
  sed -i -E '/^@(lxpanel|pcmanfm)/ s/^/#/' "$AUTOSTART"
  log "已注释 autostart 里的 lxpanel / pcmanfm（下次登录生效）"
else
  log "!! 找不到 $AUTOSTART，跳过"
fi

# ---- 2. 注释 openbox 的全部 keybind ----
if [ -f "$RC" ]; then
  python3 - "$RC" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p, encoding='utf-8', errors='replace').read()
# 只数「行首（含缩进）就是 <keybind」的，也就是尚未注释的那些。
# 不能用 "文件里有没有 <!--keybind" 来判断 —— 原文件本来就带一条
# 作者自己注释掉的 keybind，那样会误判成「已处理」而整体跳过。
n = len(re.findall(r'(?m)^[ \t]*<keybind\b', s))
if n == 0:
    print("没有未注释的 keybind，跳过")
else:
    s = re.sub(r'(?m)^([ \t]*)<keybind\b', r'\1<!--keybind', s)
    s = re.sub(r'(?m)^([ \t]*)</keybind>', r'\1</keybind-->', s)
    open(p, 'w', encoding='utf-8').write(s)
    print(f"已注释 {n} 条 keybind")
PY
else
  log "!! 找不到 $RC，跳过"
fi

# ---- 3. 让 openbox 立即重载 ----
if openbox --reconfigure 2>/dev/null; then
  log "openbox 已重载 —— 快捷键立即失效"
else
  log "!! openbox --reconfigure 失败，配置将在下次登录生效"
fi

echo
do_status
