#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 隐藏 DYTC 模式满载对比：low-power / AQM / IBS（20/35 W 组）与 performance / APM / IEP（70/70 W 组）。
# 风扇全程由 EC 控制（本脚本不写风扇转速），比较 EC 在各模式下的原生行为：
#   突发功耗与 PL2 持续时间、持续功耗 / 频率 / 温度、EC 风扇上限、SEN1。
# 每个模式：至少空闲 IDLE 秒（让 RAPL 的功耗额度恢复，PL1 时间窗约 55 s）且 CPU ≤ 60°C → 全线程满载 LOAD 秒。
# SEN1 ≥ 92°C 立即停止该模式的负载。
# 用法: sudo systemctl stop xiaoxin-fand; sudo bash tools/dytc-modes-full.sh; sudo systemctl start xiaoxin-fand
set -u
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_root; require_model; require_daemon_stopped; require_acpi_call
[ "$(cat /sys/class/power_supply/ADP0/online)" = 1 ] || { echo "请接上外部电源（充电器或充电宝）"; exit 1; }
command -v stress-ng >/dev/null || { echo "缺少 stress-ng：sudo apt install stress-ng"; exit 1; }

LOAD=${LOAD:-60}
IDLE=${IDLE:-60}
SEN1_ABORT=92
OUT=$(dirname "$(readlink -f "$0")")/results/dytc-full-$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT"; CSV=$OUT/samples.csv; SUM=$OUT/summary.txt
echo "mode,t_s,cpu_c,sen1_c,fan1,fan2,pkg_w,mhz,fcmo" > "$CSV"

acpi() { echo "$1" > /proc/acpi/call; tr -d '\0' < /proc/acpi/call; }
dytc() { acpi "\\_SB.PC00.LPCB.EC0.VPC0.DYTC $1" >/dev/null; }
fcmo() { echo $(( $(acpi '\_SB.PC00.LPCB.EC0.FCMO') )); }
rpm() { echo $(( $(acpi "\\_SB.WM10.WMAE 0 0x11 $1") )); }
PROFILE=/sys/firmware/acpi/platform_profile; ORIG=$(cat $PROFILE)
RAPL=/sys/class/powercap/intel-rapl:0; MMIO=/sys/class/powercap/intel-rapl-mmio:0
zone() { grep -lx "$1" /sys/class/thermal/thermal_zone*/type | head -1 | xargs dirname; }
TZ_CPU=$(zone x86_pkg_temp); TZ_SEN1=$(zone SEN1)
cpu_t() { echo $(( $(cat $TZ_CPU/temp) / 1000 )); }
sen1_t() { echo $(( $(cat $TZ_SEN1/temp) / 1000 )); }
mhz() { awk -F: '/^cpu MHz/ {s += $2; n++} END {printf "%d", s / n}' /proc/cpuinfo; }
pl() { echo "$(( $(cat $MMIO/constraint_0_power_limit_uw) / 1000000 ))/$(( $(cat $MMIO/constraint_1_power_limit_uw) / 1000000 ))W τ=$(( $(cat $MMIO/constraint_0_time_window_us) / 1000000 ))s"; }

# 清除所有附加模式。GKM 必须最先关（关 GKM 会写 FCMO=1，之后的 SET 才会按档位重算）
FUNCS_OFF="0x0F4001 0x0F5001 0x0F6001 0x0F7001 0x0F8001"
clear_modes() { dytc 0x0F100B; for c in $FUNCS_OFF; do dytc $c; done; }

LOAD_PID=
load_off() { [ -n "$LOAD_PID" ] && { kill "$LOAD_PID" 2>/dev/null; wait "$LOAD_PID" 2>/dev/null; }; LOAD_PID=; }
cleanup() {
  load_off
  acpi "\\_SB.WM10.WMAE 0 0x12 b0100030400000000" >/dev/null     # 风扇 1 交还 EC（写 0）
  acpi "\\_SB.WM10.WMAE 0 0x12 b0200030400000000" >/dev/null     # 风扇 2 交还 EC（写 0）
  clear_modes; echo "$ORIG" > $PROFILE
  echo "→ 已恢复: 风扇=EC 自动, platform_profile=$(cat $PROFILE), FCMO=$(fcmo)"
  echo "  记得启动守护进程: sudo systemctl start xiaoxin-fand"
  chown -R "${SUDO_USER:-root}" "$OUT"
}
trap cleanup EXIT INT TERM
note() { echo "$*" | tee -a "$SUM"; }

# 附加模式的计算顺序为 AQM→APM→IBS→IEP→MMC→STP，MMC（low-power/performance）会覆盖前面的结果，
# 所以 AQM/IBS/APM/IEP 以 balanced（MMC 无效）为底。
set_mode() {  # $1=profile $2=DYTC SET 码或 -
  clear_modes; echo "$1" > $PROFILE; sleep 0.3
  [ "$2" != - ] && dytc "$2"
  sleep 1
}

run_mode() {  # $1=名称 $2=profile $3=SET 码
  local name=$1
  set_mode "$2" "$3"
  printf "\n== %s  FCMO=%s\n" "$name" "$(fcmo)"          # PL 要 1–4 s 才到位，空闲结束后再读
  # 只等温度不够：连续测试时后测的模式功耗额度没恢复，短时功耗维持时间会明显偏短
  local i=0
  while { [ $i -lt "$IDLE" ] || [ "$(cpu_t)" -gt 60 ]; } && [ $i -lt $((IDLE + 60)) ]; do sleep 1; i=$((i + 1)); done
  printf "   空闲 %ds（CPU %d°C, SEN1 %d°C, PL=%s）\n" $i "$(cpu_t)" "$(sen1_t)" "$(pl)"

  stress-ng --cpu 0 --timeout "$((LOAD + 30))s" --quiet & LOAD_PID=$!    # 由采样循环计满 LOAD 次后结束
  local t=0 e0 e1 w aborted=0
  local ws=() ms=() cs=() ss=() fs=()
  while [ $t -lt "$LOAD" ] && kill -0 "$LOAD_PID" 2>/dev/null; do      # 负载结束即停止采样
    e0=$(cat $RAPL/energy_uj); sleep 1; e1=$(cat $RAPL/energy_uj); t=$((t + 1))
    w=$(( (e1 - e0) / 100000 ))             # 0.1 W
    local c s f1 f2 m
    c=$(cpu_t); s=$(sen1_t); f1=$(rpm 0x04030001); f2=$(rpm 0x04030002); m=$(mhz)
    ws+=($w); ms+=($m); cs+=($c); ss+=($s); fs+=($f1)
    echo "$name,$t,$c,$s,$f1,$f2,$((w / 10)).$((w % 10)),$m,$(fcmo)" >> "$CSV"
    [ $((t % 10)) = 0 ] && printf "   %3ds  CPU %3d°C  SEN1 %3d°C  风扇 %4d/%4d  %3d.%dW  %4d MHz\n" \
      $t $c $s $f1 $f2 $((w / 10)) $((w % 10)) $m
    if [ "$s" -ge $SEN1_ABORT ]; then echo "   !! SEN1 ${s}°C ≥ ${SEN1_ABORT}°C，停止负载"; aborted=1; break; fi
  done
  load_off

  # 汇总：突发 = 前 10 s；持续 = 最后 20 s；PL2 时长 = 功耗高于（最后 20 s 平均 + 5 W）的秒数
  python3 -I - "$name" "$(fcmo)" "$(pl)" $aborted "${ws[*]}" "${ms[*]}" "${cs[*]}" "${ss[*]}" "${fs[*]}" <<'EOF' | tee -a "$SUM"
import sys
name, fc, pl, aborted = sys.argv[1:5]
w, m, c, s, f = ([int(x) for x in a.split()] for a in sys.argv[5:10])
w = [x / 10 for x in w]
avg = lambda a: sum(a) / len(a)
tail = slice(-20, None)
sus = avg(w[tail])
boost = sum(1 for x in w if x > sus + 5)
print(f"{name:<12} FCMO={fc:<2} PL={pl:<16} 突发 {avg(w[:10]):5.1f}W 峰值 {max(w):5.1f}W 高功耗 {boost:2d}s | "
      f"持续 {sus:5.1f}W {avg(m[tail]):5.0f}MHz CPU {avg(c[tail]):3.0f}°C 风扇 {max(f):4d} SEN1 峰值 {max(s)}°C"
      + ("  [SEN1 中止]" if aborted == "1" else ""))
EOF
}

note "结果目录: $OUT"
note "供电: ADP0=1  电池 $(cat /sys/class/power_supply/BAT0/capacity)%   每个模式空闲 ≥${IDLE}s、满载 ${LOAD}s（$(nproc) 线程）"
note ""
run_mode low-power   low-power   -
run_mode AQM         balanced    0x1F6001
run_mode IBS         balanced    0x1F8001
run_mode performance performance -
run_mode APM         balanced    0x1F5001
run_mode IEP         balanced    0x1F7001
note ""
note "（突发 = 前 10 s 平均；持续 = 最后 20 s 平均；高功耗 = 功耗高于持续值 5 W 以上的秒数）"
