#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 读取风扇实时转速（只读）。通过 BIOS 的 LENOVO_OTHER_METHOD.GetFeatureValue：
#   \_SB.WM10.WMAE(instance=0, method=0x11, IDs)  0x04030001=风扇1  0x04030002=风扇2
# 用法: sudo bash tools/fan-rpm.sh [次数，默认 10]
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
call() { echo "\\_SB.WM10.WMAE 0 0x11 $1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
TZ=$(grep -l '^x86_pkg_temp$' /sys/class/thermal/thermal_zone*/type | head -1 | xargs dirname)
for i in $(seq "${1:-10}"); do
  printf "%s  CPU %3d°C  风扇1=%-8s 风扇2=%-8s 模式FCMO=%s\n" "$(date +%T)" $(( $(cat $TZ/temp)/1000 )) \
    "$(( $(call 0x04030001) ))" "$(( $(call 0x04030002) ))" \
    "$(echo '\_SB.PC00.LPCB.EC0.FCMO' > /proc/acpi/call; tr -d '\0' < /proc/acpi/call)"
  sleep 1
done
