#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 记录除尘模式下的风扇转速变化（默认 45 秒），结束/中断时写 fan_mode=0 退出除尘。
# 用法: sudo bash tools/dust-record.sh [秒数]
set -u
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
DUR=${1:-45}
FM=$(ls /sys/bus/platform/devices/VPC2004:*/fan_mode | head -1)
OUT=$(dirname "$(readlink -f "$0")")/results/dust-$(date +%Y%m%d-%H%M%S).csv
acpi() { echo "$1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
rpm() { echo $(( $(acpi "\\_SB.WM10.WMAE 0 0x11 $1") )); }

stop_dust() { echo 0 > "$FM"; echo; echo "→ 已退出除尘：fan_mode 读回 $(cat "$FM")"; chown "${SUDO_USER:-root}" "$OUT"; }
trap stop_dust EXIT INT TERM

echo "t_s,fan1,fan2,fan_mode" > "$OUT"
echo "除尘前: 风扇 $(rpm 0x04030001)/$(rpm 0x04030002) rpm, fan_mode=$(cat "$FM")"
echo 2 > "$FM"
start=$(date +%s.%N)
while :; do
  t=$(echo "$(date +%s.%N) - $start" | bc)
  (( $(echo "$t > $DUR" | bc) )) && break
  a=$(rpm 0x04030001); b=$(rpm 0x04030002); m=$(cat "$FM")
  printf "%.1f,%s,%s,%s\n" "$t" "$a" "$b" "$m" >> "$OUT"
  printf "  %5.1fs  风扇 %5s / %5s rpm  fan_mode=%s\n" "$t" "$a" "$b" "$m"
  sleep 0.5
done
echo "记录已保存: $OUT"
