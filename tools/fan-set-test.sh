#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 手动设定风扇转速的安全测试：只向上设定，结束（或中断）时一定写 0 恢复 EC 自动控制。
#   SetFeatureValue = \_SB.WM10.WMAE(0, 0x12, 8字节缓冲区: DWORD 功能ID + DWORD 数值)，小端序
#   数值 0 → 恢复自动（EC 命令 0x63/3）；数值 N≥200 → 目标 N rpm（EC 命令 0x61/0x62）
#   警告：数值 1 会让风扇目标转速为 0（停转），本脚本拒绝 <2000 的值。
# 用法: sudo bash tools/fan-set-test.sh [目标rpm 2000–6000，默认 5000]
set -u
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
TARGET=${1:-5000}
[ "$TARGET" -ge 2000 ] && [ "$TARGET" -le 6000 ] || { echo "目标转速须在 2000–6000 之间（硬件满速约 6100）"; exit 1; }

acpi() { echo "$1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
le32() { printf '%02x%02x%02x%02x' $(( $1 & 255 )) $(( ($1>>8) & 255 )) $(( ($1>>16) & 255 )) $(( ($1>>24) & 255 )); }
set_fan() { acpi "\\_SB.WM10.WMAE 0 0x12 b$(le32 $1)$(le32 $2)"; }   # $1=功能ID $2=数值
rpm() { echo $(( $(acpi "\\_SB.WM10.WMAE 0 0x11 $1") )); }
TZ=$(grep -l '^x86_pkg_temp$' /sys/class/thermal/thermal_zone*/type | head -1 | xargs dirname)
show() { printf "  %-10s %3ds  CPU %3d°C  风扇 %5s / %5s rpm\n" "$1" "$2" $(( $(cat $TZ/temp)/1000 )) "$(rpm 0x04030001)" "$(rpm 0x04030002)"; }

restored=0
restore() {
  [ $restored = 1 ] && return; restored=1
  echo "→ 恢复自动控制: 风扇1 返回 $(set_fan 0x04030001 0)  风扇2 返回 $(set_fan 0x04030002 0)"
}
trap restore EXIT INT TERM

echo "== 当前（EC 自动）"; for i in 1 2 3; do show auto $i; sleep 1; done

echo "== 设定双风扇目标 ${TARGET} rpm"
echo "   风扇1 返回 $(set_fan 0x04030001 $TARGET)  风扇2 返回 $(set_fan 0x04030002 $TARGET)   （0 = EC 确认成功）"
for i in $(seq 12); do sleep 1; show manual $i; done

restore
echo "== 恢复后观察 20 秒（应回落到 EC 自动转速）"
for i in $(seq 20); do sleep 1; show auto $i; done
