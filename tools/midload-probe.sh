#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 中等负载风扇曲线测试：balanced / performance / GKM 三种模式，
# 阶梯负载 2 → 4 → 8 线程，记录温度、双风扇转速、功耗、频率。
# 用法: sudo bash tools/midload-probe.sh     （插电运行，约 9 分钟）
# 可调: sudo env MODES="performance GKM" STEP_S=40 bash ...（只测两种模式约 5 分钟）
set -u
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
[ "$(cat /sys/class/power_supply/ADP0/online)" = 1 ] || { echo "请插上电源"; exit 1; }

IDLE=${IDLE:-15}; STEP_S=${STEP_S:-50}; COOL=${COOL:-20}; DT=2; STEPS="2 4 8"
MODES=${MODES:-"balanced performance GKM"}
OUT=$(dirname "$(readlink -f "$0")")/results/midload-$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT"
CSV=$OUT/samples.csv
echo "mode,elapsed_s,phase,threads,temp_c,fan1_rpm,fan2_rpm,pkg_w,mhz" > "$CSV"

acpi() { echo "$1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
dytc() { acpi "\\_SB.PC00.LPCB.EC0.VPC0.DYTC $1"; }
rpm()  { echo $(( $(acpi "\\_SB.WM10.WMAE 0 0x11 $1") )); }
fcmo() { acpi '\_SB.PC00.LPCB.EC0.FCMO'; }
PROFILE=/sys/firmware/acpi/platform_profile
ORIG=$(cat $PROFILE)
RAPL=/sys/class/powercap/intel-rapl:0
TZ=$(grep -l '^x86_pkg_temp$' /sys/class/thermal/thermal_zone*/type | head -1 | xargs dirname)
mhz() { awk '/MHz/{s+=$4;n++} END{printf "%d", s/n}' /proc/cpuinfo; }

pids=()
stop_load() { kill "${pids[@]}" 2>/dev/null; wait "${pids[@]}" 2>/dev/null; pids=(); }
start_load() { stop_load; for i in $(seq "$1"); do ( x=0; while :; do x=$(( (x*7+12345) % 1000003 )); done ) & pids+=($!); done; }
cleanup() {
  stop_load
  dytc 0x0F100B >/dev/null
  echo low-power > $PROFILE; sleep 0.5; echo "$ORIG" > $PROFILE
  echo "已恢复: platform_profile=$(cat $PROFILE) FCMO=$(fcmo)"
  chown -R "${SUDO_USER:-root}" "$(dirname "$OUT")"
}
trap cleanup EXIT INT TERM

sample() {  # $1=mode $2=phase $3=threads $4=t0 $5=秒数
  local end=$((SECONDS + $5)) e0 e1
  while [ $SECONDS -lt $end ]; do
    e0=$(cat $RAPL/energy_uj); sleep $DT; e1=$(cat $RAPL/energy_uj)
    printf "%s,%d,%s,%s,%d,%s,%s,%.1f,%s\n" "$1" $((SECONDS - $4)) "$2" "$3" \
      $(( $(cat $TZ/temp)/1000 )) "$(rpm 0x04030001)" "$(rpm 0x04030002)" \
      "$(echo "($e1-$e0)/$DT/1000000" | bc -l)" "$(mhz)" | tee -a "$CSV" |
      awk -F, '$2%10<2{printf "   %4ss %-5s %sT  %3s°C  风扇 %5s/%5s rpm  %5.1fW  %sMHz\n",$2,$3,$4,$5,$6,$7,$8,$9}'
  done
}

run() {  # $1=模式名
  local t0=$SECONDS
  echo "== $1  FCMO=$(fcmo)" | tee -a "$OUT/summary.txt"
  sample "$1" idle 0 $t0 $IDLE
  for n in $STEPS; do start_load $n; sample "$1" load $n $t0 $STEP_S; done
  stop_load
  sample "$1" cool 0 $t0 $COOL
}

echo "结果目录: $OUT"
for m in $MODES; do
  case $m in
    balanced)    dytc 0x0F100B >/dev/null; echo balanced > $PROFILE ;;
    performance) dytc 0x0F100B >/dev/null; echo performance > $PROFILE ;;
    GKM)         echo performance > $PROFILE; sleep 0.5; dytc 0x1F100B >/dev/null ;;
  esac
  sleep 1; run $m
done

echo; echo "================ 汇总：每个负载阶梯最后 20 秒的平均 ================" | tee -a "$OUT/summary.txt"
for m in $MODES; do
  for n in 0 $STEPS; do
    awk -F, -v m=$m -v n=$n -v S=$STEP_S -v I=$IDLE '
      NR>1 && $1==m && $4==n && (($3=="idle") || ($3=="load")) { rows[++c]=$0 }
      END { if (!c) exit; from = (c>10 ? c-9 : 1)
            for (i=from;i<=c;i++){ split(rows[i],f,","); t+=f[5]; a+=f[6]; b+=f[7]; p+=f[8]; z+=f[9]; k++ }
            printf "%-12s %s线程  温度 %3.0f°C  风扇 %5.0f / %5.0f rpm  功耗 %5.1fW  频率 %4.0fMHz\n", m, (n==0?"空闲":n), t/k, a/k, b/k, p/k, z/k }' "$CSV"
  done
done | tee -a "$OUT/summary.txt"
