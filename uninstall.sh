#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 卸载 xiaoxin-fan，风扇交还 EC。用法: sudo bash ~/xiaoxin-fan/uninstall.sh
[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行"; exit 1; }
systemctl disable --now xiaoxin-fand 2>/dev/null     # ExecStopPost 会交还 EC
[ -x /opt/xiaoxin-fan/bin/xiaoxin-fand ] && /opt/xiaoxin-fan/bin/xiaoxin-fand --release
rm -f /etc/systemd/system/xiaoxin-fand.service /etc/dbus-1/system.d/io.github.XiaoxinFan.conf \
      /etc/modules-load.d/xiaoxin-fan.conf /usr/local/bin/fanctl
rm -rf /opt/xiaoxin-fan /var/lib/xiaoxin-fan
systemctl daemon-reload
busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig >/dev/null 2>&1 || true
echo "已卸载。/etc/xiaoxin-fan（用户自定义预设）保留，如不需要请手动删除。"
