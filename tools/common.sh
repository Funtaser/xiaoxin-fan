#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 探测/测试脚本的公共检查。这些脚本直接调用 BIOS/EC 方法，只在已验证机型上运行。

require_root() { [ "$(id -u)" = 0 ] || { echo "请用 sudo 运行"; exit 1; }; }

require_model() {
  local product; product=$(cat /sys/class/dmi/id/product_name 2>/dev/null)
  if [ "$product" != 83D4 ] && [ "${XIAOXIN_ALLOW_UNTESTED:-0}" != 1 ]; then
    echo "!! 本脚本只在小新 Pro 16 IMH9（83D4）上验证过，当前机型: $product"
    echo "   其它机型上同名 ACPI 方法可能含义不同。确认要尝试请设置 XIAOXIN_ALLOW_UNTESTED=1（风险自负）。"
    exit 1
  fi
}

require_daemon_stopped() {
  if systemctl is-active --quiet xiaoxin-fand 2>/dev/null; then
    echo "!! xiaoxin-fand 正在运行，会与本脚本争用 /proc/acpi/call。"
    echo "   请先: sudo systemctl stop xiaoxin-fand    测完后: sudo systemctl start xiaoxin-fand"
    echo "   （只想看转速可直接用: fanctl status）"
    exit 1
  fi
}

require_acpi_call() { modprobe acpi_call || { echo "缺少 acpi_call：sudo apt install acpi-call-dkms"; exit 1; }; }
