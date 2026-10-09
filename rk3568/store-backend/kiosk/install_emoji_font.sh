#!/bin/bash
# ============================================================
#  给板子装一个「能渲染」的 emoji 字体
# ============================================================
#  为什么需要这个脚本：板子自带的字体全都盖不住项目用到的 emoji，
#  页面上会出现「有字无图」的空白格。2026-10-09 实测结论：
#
#    · /usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf
#      （fonts-symbola）—— 轮廓字体，能渲染，但只到 Unicode 6.1 左右。
#      Emoji 11/12 新增的字符一个都没有，例如：
#        U+1F9FB 🧻 卷纸（日用品分类图标）→ 维达抽纸这类商品图标全空白
#        U+1F9FE 🧾 收据   U+1F96C 🥬 绿叶菜（生鲜）
#        U+1F9E9 🧩 拼图   U+1F9CA 🧊 冰块   U+1F7E2 🟢 绿圆
#
#    · /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf
#      （fonts-noto-color-emoji）—— **CBDT 位图字体，这个 Chromium 的
#      FreeType 完全不渲染**（连豆腐块都没有，整行直接空白）。
#      装了等于没装，别浪费时间。
#
#  所以这里装 OpenMoji-Black.ttf（单色**轮廓**字体，glyf 表）：
#    · 覆盖 1799 个码点，项目用到的 68 个 emoji 里能盖住 66 个
#      （剩下 U+2713 ✓ / U+2715 ✕ 是 dingbat，DejaVu 本来就有）
#    · 单色字体跟随 CSS 文字颜色 → 深色大屏上是白图标，正合适
#    · 10.6 MB，装一次就行
#
#  ⚠️ 别用 Google Fonts CSS API 给的那个 gstatic 地址
#     （fonts.gstatic.com/s/notoemoji/...）—— 那是**按 unicode-range
#     切好的子集**，只覆盖一小段码点，实测 6 个缺字里只补上 2 个。
#     要装就装完整的字体文件。
#
#  用法：sudo bash install_emoji_font.sh
# ============================================================
set -eu

FONT_URL="https://cdn.jsdelivr.net/npm/openmoji@latest/font/OpenMoji-Black.ttf"
DEST_DIR="/usr/share/fonts/truetype/openmoji"
DEST="$DEST_DIR/OpenMoji-Black.ttf"

if [ "$(id -u)" != "0" ]; then
  echo "需要 root：sudo bash $0"
  exit 1
fi

mkdir -p "$DEST_DIR"

if [ -s "$DEST" ]; then
  echo "已经装过了：$DEST"
else
  echo "下载 OpenMoji-Black.ttf ..."
  # --noproxy '*' 是必须的：板子的 apt 配置里塞了一个不可达的代理，
  # 环境变量/配置继承下来会让 curl 连不上（见 docs §12.4）。
  curl -sL --noproxy '*' -m 300 -o "$DEST" "$FONT_URL"
  chmod 644 "$DEST"
fi

ls -l "$DEST"
echo "文件头（应为 00010000，即 TrueType 轮廓字体）："
head -c 4 "$DEST" | od -An -tx1

fc-cache -f >/dev/null 2>&1
echo
echo "fontconfig 现在认到的 emoji 字体："
fc-list | grep -iE 'openmoji|symbola|emoji' || true
echo
echo "谁能渲染 U+1F9FB（🧻 日用品图标）："
fc-list ':charset=1f9fb' family file || echo "!! 没有字体能渲染它"

cat <<'NOTE'

接下来（页面侧）：三个页面的 font-family 里要带上 OpenMoji，且排在
Symbola 前面 —— 否则命中的还是老的 Symbola：

  web/index.html / admin.html / bigscreen.html
    font-family: ...,OpenMoji,Symbola,'Noto Color Emoji',...

改完部署 + 刷新页面即可。验证办法：截屏看图标，或者
  chromium --headless --screenshot 渲染一个只含目标码点的测试页。
NOTE
