#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Linux 侧对照测试：与 Windows fan-probe.ps1 相同的负载类型与时长，
# 比较 performance（MMC）与 GKM（推测 = Windows 极客模式）下的持续功耗/频率/温度。
# 用法: sudo bash tools/linux-probe.sh      （插电运行，约 9 分钟）
set -u
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
POWER=ac
if [ "$(cat /sys/class/power_supply/ADP0/online)" != 1 ]; then
  [ "${ALLOW_BATTERY:-0}" = 1 ] || { echo "请插上电源（或用 ALLOW_BATTERY=1 在电池下测试，结果不可与 Windows 插电数据对比）"; exit 1; }
  POWER=battery; echo "!! 电池供电测试：EC 会限制功耗，结果仅用于模式间对比"
fi

IDLE=30; LOAD=180; COOL=60; STEP=2
OUT=$(dirname "$(readlink -f "$0")")/results/linux-$(date +%Y%m%d-%H%M%S)-$POWER
mkdir -p "$OUT"; chown "${SUDO_USER:-root}" "$OUT" "$(dirname "$OUT")"
CSV=$OUT/samples.csv
echo "mode,elapsed_s,phase,pkg_w,core_w,mhz,temp_c" > "$CSV"

M='\_SB.PC00.LPCB.EC0.VPC0.DYTC'
acpi() { echo "$1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
dytc() { acpi "$M $1"; }
fcmo() { acpi '\_SB.PC00.LPCB.EC0.FCMO'; }
PROFILE=/sys/firmware/acpi/platform_profile
ORIG=$(cat $PROFILE)
RAPL=/sys/class/powercap/intel-rapl:0
CORE=/sys/class/powercap/intel-rapl:0:0
TZ=$(grep -l '^x86_pkg_temp$' /sys/class/thermal/thermal_zone*/type | head -1 | xargs dirname)

pids=()
cleanup() {
  kill "${pids[@]}" 2>/dev/null
  dytc 0x0F100B >/dev/null                     # GKM off
  echo low-power > $PROFILE; sleep 0.5; echo "$ORIG" > $PROFILE
  echo "已恢复: platform_profile=$(cat $PROFILE) DYTC=$(dytc 0x02) FCMO=$(fcmo)"
  chown -R "${SUDO_USER:-root}" "$OUT"
}
trap cleanup EXIT INT TERM

mhz() { awk '/MHz/{s+=$4;n++} END{printf "%d", s/n}' /proc/cpuinfo; }

run() {  # $1=模式名
  local mode=$1 t0=$SECONDS
  local e0 c0 e1 c1 el ph
  limits=$(for c in 0 1; do printf "%s=%sW " "$(cat $RAPL/constraint_${c}_name)" $(( $(cat $RAPL/constraint_${c}_power_limit_uw)/1000000 )); done)
  echo "== $mode  DYTC=$(dytc 0x02)  FCMO=$(fcmo)  MSR限制: $limits" | tee -a "$OUT/summary.txt"
  for ph in idle load cool; do
    case $ph in
      idle) dur=$IDLE ;;
      load) dur=$LOAD; for i in $(seq $(nproc)); do ( x=0; while :; do x=$(( (x*7+12345) % 1000003 )); done ) & pids+=($!); done ;;
      cool) dur=$COOL; kill "${pids[@]}" 2>/dev/null; wait "${pids[@]}" 2>/dev/null; pids=() ;;
    esac
    local end=$((SECONDS + dur))
    while [ $SECONDS -lt $end ]; do
      e0=$(cat $RAPL/energy_uj); c0=$(cat $CORE/energy_uj)
      sleep $STEP
      e1=$(cat $RAPL/energy_uj); c1=$(cat $CORE/energy_uj)
      el=$((SECONDS - t0))
      printf "%s,%d,%s,%.1f,%.1f,%s,%d\n" "$mode" $el $ph \
        "$(echo "($e1-$e0)/$STEP/1000000" | bc -l)" "$(echo "($c1-$c0)/$STEP/1000000" | bc -l)" \
        "$(mhz)" $(( $(cat $TZ/temp)/1000 )) | tee -a "$CSV" | awk -F, '$2%10<2{printf "   %4ss %-4s pkg=%5.1fW core=%5.1fW %5sMHz %s°C\n",$2,$3,$4,$5,$6,$7}'
    done
  done
}

echo "结果目录: $OUT"
echo performance > $PROFILE; sleep 1
run performance
dytc 0x1F100B >/dev/null; sleep 1          # GKM on
run GKM

echo; echo "================ 汇总（满载第 60–180 秒平均）================" | tee -a "$OUT/summary.txt"
awk -F, 'NR>1 && $3=="load" && $2>='"$IDLE"'+60 {p[$1]+=$4; c[$1]+=$5; f[$1]+=$6; t[$1]+=$7; n[$1]++}
  END {for (m in n) printf "%-12s pkg=%.1fW core=%.1fW freq=%.0fMHz temp=%.0f°C\n", m, p[m]/n[m], c[m]/n[m], f[m]/n[m], t[m]/n[m]}' "$CSV" | tee -a "$OUT/summary.txt"
echo "供电: $POWER；电量: $(cat /sys/class/power_supply/BAT0/capacity)%" | tee -a "$OUT/summary.txt"
echo "Windows 极客模式（插电参考）  pkg≈64.5W core≈57.8W freq≈3070MHz" | tee -a "$OUT/summary.txt"
