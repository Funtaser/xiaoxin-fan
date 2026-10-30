#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 为当前用户安装 GNOME 快捷设置面板。用法: bash gnome-extension/install.sh   （不要用 sudo）
set -e
[ "$(id -u)" != 0 ] || { echo "请以普通用户运行（不要 sudo）"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(find "$HERE" -mindepth 1 -maxdepth 1 -type d -name '*@*' | head -1)
UUID=$(basename "$SRC")
DST="$HOME/.local/share/gnome-shell/extensions/$UUID"

ver=$(gnome-shell --version 2>/dev/null | grep -oE '[0-9]+' | head -1)
grep -q "\"$ver\"" "$SRC/metadata.json" || echo "!! 面板只在 GNOME Shell 50 上测试过，当前版本: ${ver:-未知}"

rm -rf "$DST"; mkdir -p "$(dirname "$DST")"
cp -r "$SRC" "$DST"
echo "已复制到 $DST"

cur=$(gsettings get org.gnome.shell enabled-extensions)
if ! echo "$cur" | grep -q "'$UUID'"; then
  if [ "$cur" = "@as []" ]; then new="['$UUID']"; else new="${cur%]}, '$UUID']"; fi
  gsettings set org.gnome.shell enabled-extensions "$new"
fi
if [ "$(gsettings get org.gnome.shell disable-user-extensions)" = true ]; then
  echo "!! 系统设置禁用了所有用户扩展（disable-user-extensions=true）。"
  echo "   启用: gsettings set org.gnome.shell disable-user-extensions false"
fi
echo "完成。Wayland 下请注销并重新登录，面板会出现在右上角快捷设置中。"
