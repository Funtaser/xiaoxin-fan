#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 安装 xiaoxin-fan。用法: sudo bash ~/xiaoxin-fan/install.sh
# 首次安装后预设为「原厂」，行为与未安装时完全相同；用 fanctl set <预设> 切换。
set -e
[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行"; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)
DST=/opt/xiaoxin-fan

model=$(cat /sys/class/dmi/id/product_name 2>/dev/null)
[ "$model" = 83D4 ] || { echo "!! 本程序只在小新 Pro 16 IMH9（83D4）上验证过，当前机型: $model"; read -rp "仍要继续？[y/N] " a; [ "$a" = y ]; }
modinfo acpi_call >/dev/null 2>&1 || { echo "缺少 acpi_call 模块：sudo apt install acpi-call-dkms"; exit 1; }

echo "[1/5] 运行单元测试"
python3 -B -m unittest discover -s "$SRC/tests" 2>&1 | tail -1

echo "[2/5] 复制程序到 $DST"
systemctl stop xiaoxin-fand 2>/dev/null || true
rm -rf "$DST"; mkdir -p "$DST"
cp -r "$SRC/xiaoxin_fan" "$SRC/bin" "$DST/"
find "$DST" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$DST"; chmod 755 "$DST"/bin/*
ln -sf "$DST/bin/fanctl" /usr/local/bin/fanctl

echo "[3/5] D-Bus 权限策略 / 开机加载 acpi_call"
install -m 644 "$SRC/data/io.github.XiaoxinFan.conf" /etc/dbus-1/system.d/
echo acpi_call > /etc/modules-load.d/xiaoxin-fan.conf
busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig >/dev/null 2>&1 || true
mkdir -p /etc/xiaoxin-fan

echo "[4/5] systemd 服务"
install -m 644 "$SRC/data/xiaoxin-fand.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xiaoxin-fand

echo "[5/5] 当前状态"
sleep 2
systemctl --no-pager --lines=5 status xiaoxin-fand | sed -n '1,4p'
echo
sudo -u "${SUDO_USER:-root}" fanctl status || true
echo
echo "安装完成。常用命令: fanctl list | fanctl set 性能 | fanctl watch | fanctl set 原厂"
