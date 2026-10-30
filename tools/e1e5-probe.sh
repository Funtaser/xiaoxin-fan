#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# E1：手动风扇设定的控制语义（能否压低到 EC 目标以下？切换模式后是否保持？）
# E5：隐藏 DYTC 模式的风扇上限 / 功耗策略摸底
# 低功耗设计：只用 2 线程负载（约 28W），总时长约 5 分钟。
# 用法: sudo bash tools/e1e5-probe.sh
set -u
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
[ "$(cat /sys/class/power_supply/ADP0/online)" = 1 ] || { echo "请接上外部电源（充电器或充电宝）"; exit 1; }

GUARD=88            # 超过此温度立即交还 EC
OUT=$(dirname "$(readlink -f "$0")")/results/e1e5-$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT"; CSV=$OUT/samples.csv; SUM=$OUT/summary.txt
echo "t_s,stage,temp_c,fan1,fan2,pkg_w,fcmo" > "$CSV"

acpi() { echo "$1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
dytc() { acpi "\\_SB.PC00.LPCB.EC0.VPC0.DYTC $1"; }
fcmo() { acpi '\_SB.PC00.LPCB.EC0.FCMO'; }
le32() { printf '%02x%02x%02x%02x' $(( $1 & 255 )) $(( ($1>>8) & 255 )) $(( ($1>>16) & 255 )) $(( ($1>>24) & 255 )); }
setfan() {  # $1=rpm（0=自动，否则须 ≥2000）
  if [ "$1" != 0 ] && [ "$1" -lt 2000 ]; then echo "拒绝写入 $1"; return 1; fi
  acpi "\\_SB.WM10.WMAE 0 0x12 b$(le32 0x04030001)$(le32 $1)" >/dev/null
  acpi "\\_SB.WM10.WMAE 0 0x12 b$(le32 0x04030002)$(le32 $1)" >/dev/null
}
rpm() { echo $(( $(acpi "\\_SB.WM10.WMAE 0 0x11 $1") )); }
PROFILE=/sys/firmware/acpi/platform_profile; ORIG=$(cat $PROFILE)
RAPL=/sys/class/powercap/intel-rapl:0; MMIO=/sys/class/powercap/intel-rapl-mmio:0
TZ=$(grep -l '^x86_pkg_temp$' /sys/class/thermal/thermal_zone*/type | head -1 | xargs dirname)
temp() { echo $(( $(cat $TZ/temp)/1000 )); }
pl() { printf "MSR PL1=%sW PL2=%sW | MMIO PL1=%sW PL2=%sW" \
  $(( $(cat $RAPL/constraint_0_power_limit_uw)/1000000 )) $(( $(cat $RAPL/constraint_1_power_limit_uw)/1000000 )) \
  $(( $(cat $MMIO/constraint_0_power_limit_uw)/1000000 )) $(( $(cat $MMIO/constraint_1_power_limit_uw)/1000000 )); }

T0=$SECONDS; LAST_F1=0; LAST_F2=0; LAST_W=0
sample() {  # $1=阶段名 $2=秒数；温度超限则交还 EC
  local end=$((SECONDS + $2)) e0 e1
  while [ $SECONDS -lt $end ]; do
    e0=$(cat $RAPL/energy_uj); sleep 1; e1=$(cat $RAPL/energy_uj)
    local t=$(temp); LAST_F1=$(rpm 0x04030001); LAST_F2=$(rpm 0x04030002)
    LAST_W=$(echo "($e1-$e0)/1000000" | bc -l)
    printf "%d,%s,%d,%s,%s,%.1f,%s\n" $((SECONDS-T0)) "$1" $t $LAST_F1 $LAST_F2 $LAST_W "$(fcmo)" >> "$CSV"
    printf "   %4ds %-22s %3d°C  风扇 %5s / %5s  %5.1fW\n" $((SECONDS-T0)) "$1" $t $LAST_F1 $LAST_F2 $LAST_W
    if [ $t -ge $GUARD ]; then echo "   !! ${t}°C ≥ ${GUARD}°C，交还 EC"; setfan 0; GUARDED=1; fi
  done
}

pids=()
load_on()  { for i in 1 2; do ( x=0; while :; do x=$(( (x*7+12345) % 1000003 )); done ) & pids+=($!); done; }
load_off() { kill "${pids[@]}" 2>/dev/null; wait "${pids[@]}" 2>/dev/null; pids=(); }
FUNCS_OFF="0x0F4001 0x0F5001 0x0F6001 0x0F7001 0x0F8001"
cleanup() {
  load_off; setfan 0
  for c in $FUNCS_OFF; do dytc $c >/dev/null; done; dytc 0x0F100B >/dev/null
  echo low-power > $PROFILE; sleep 0.5; echo "$ORIG" > $PROFILE
  echo "→ 已恢复: 风扇=EC 自动, platform_profile=$(cat $PROFILE), FCMO=$(fcmo)"
  chown -R "${SUDO_USER:-root}" "$OUT"
}
trap cleanup EXIT INT TERM
note() { echo "$*" | tee -a "$SUM"; }

note "结果目录: $OUT"
note "供电: ADP0=1  电池 $(cat /sys/class/power_supply/BAT0/capacity)%"
echo performance > $PROFILE; sleep 1

echo; note "################ E1：控制语义 ################"
note "[E1-a] 空闲时手动设定 4500"
setfan 4500; sample "E1a 手动4500" 8
note "   结果: 风扇 $LAST_F1 / $LAST_F2"
note "[E1-b] 切换电源模式（balanced → performance），看手动设定是否保持"
echo balanced > $PROFILE; sleep 0.5; echo performance > $PROFILE
sample "E1b 切换模式后" 10
note "   结果: 风扇 $LAST_F1 / $LAST_F2   （≈4500 = 保持；≈2300 = 被 EC 覆盖）"
note "[E1-c] 2 线程负载（EC 自动会拉到 3900），手动设 3000"
GUARDED=0; load_on; setfan 3000; sample "E1c 负载+手动3000" 25
note "   结果: 风扇 $LAST_F1 / $LAST_F2  温度 $(temp)°C   （≈3000 = 可压低；≈3900 = EC 强制下限）$([ $GUARDED = 1 ] && echo '  [触发温度保护]')"
setfan 0

echo; note "################ E5：隐藏模式摸底（2 线程负载持续）################"
mode() {  # $1=名称 $2=profile $3=DYTC SET 码或 -
  for c in $FUNCS_OFF; do dytc $c >/dev/null; done
  echo "$2" > $PROFILE; sleep 0.3
  [ "$3" != - ] && dytc "$3" >/dev/null
  sleep 0.5
  setfan 2500; sleep 3; setfan 0          # 先压低，让风扇从下往上爬到该模式上限
  sample "E5 $1" 22
  note "$(printf '  %-10s FCMO=%-4s 风扇 %5s / %5s  %5.1fW  %s' "$1" "$(fcmo)" $LAST_F1 $LAST_F2 $LAST_W "$(pl)")"
}
mode low-power   low-power  -
mode balanced    balanced   -
mode AQM         balanced   0x1F6001
mode APM         balanced   0x1F5001
mode IBS         balanced   0x1F8001
mode IEP         balanced   0x1F7001
mode STP         balanced   0x1F4001
note "（参考：performance 上限 3900，GKM 上限 4300，硬件满速约 6100）"
