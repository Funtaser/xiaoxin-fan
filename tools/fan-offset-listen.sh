#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 听感对比：两个风扇同转速 vs 错开（EC 自己总让风扇 2 比风扇 1 低约 200 rpm）。同转速时两个风扇的
# 微小差异可能产生“嗡—嗡”的拍频。A / B 交替各 15 s，共两轮；结束（或中断）时一定写 0 交还 EC。
# 用法: sudo bash tools/fan-offset-listen.sh [基准rpm，默认 3000] [错开量，默认 200]
set -u
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
BASE=${1:-3000}; OFF=${2:-200}
[ "$BASE" -ge 2200 ] && [ "$BASE" -le 6000 ] && [ $((BASE - OFF)) -ge 2000 ] && [ "$OFF" -ge 0 ] \
  || { echo "基准须在 2200–6000 之间，且 基准 − 错开量 ≥ 2000"; exit 1; }

acpi() { echo "$1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
le32() { printf '%02x%02x%02x%02x' $(( $1 & 255 )) $(( ($1>>8) & 255 )) $(( ($1>>16) & 255 )) $(( ($1>>24) & 255 )); }
set_fan() { acpi "\\_SB.WM10.WMAE 0 0x12 b$(le32 $1)$(le32 $2)" > /dev/null; }   # $1=功能ID $2=数值
rpm() { echo $(( $(acpi "\\_SB.WM10.WMAE 0 0x11 $1") )); }

restored=0
restore() {
  [ $restored = 1 ] && return; restored=1
  set_fan 0x04030001 0; set_fan 0x04030002 0
  echo "→ 已交还 EC 自动控制。别忘了: sudo systemctl start xiaoxin-fand"
}
trap restore EXIT INT TERM

phase() {   # $1=名称 $2=风扇1 $3=风扇2
  set_fan 0x04030001 "$2"; set_fan 0x04030002 "$3"
  echo "== $1：风扇 1 → $2，风扇 2 → $3（听 15 秒）"
  for i in 1 2 3; do sleep 5; echo "   实际 $(rpm 0x04030001) / $(rpm 0x04030002) rpm"; done
}

for round in 1 2; do
  phase "第 $round 轮 A 同转速" "$BASE" "$BASE"
  phase "第 $round 轮 B 错开 $OFF" "$BASE" $((BASE - OFF))
done
echo "哪一段更顺耳（A 还是 B）？"
