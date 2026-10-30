#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# 内核对比测试：CPU 单核 / 多核、核显 3D、CPU+GPU 同时负载（模拟游戏）
# 用法: bash tools/bench.sh     （无需 sudo，请插电运行）
cd "$(dirname "$0")"
K=$(uname -r)
OUT="result-$K.txt"
GL="glmark2 --off-screen -s 1920x1080"
temp() { echo $(( $(cat /sys/class/thermal/thermal_zone8/temp) / 1000 )); }
cool() { echo "  冷却 ${1}s..."; sleep "$1"; }
fps() { grep -oE 'FPS: [0-9]+' | awk '{s+=$2;n++} END{printf "%d", s/n}'; }

{
echo "内核: $K"
echo "CPU 调频驱动: $(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_driver)  调速器: $(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor)"
echo "电源模式: $(cat /sys/firmware/acpi/platform_profile)   插电: $(cat /sys/class/power_supply/ADP0/online 2>/dev/null)"
echo "时间: $(date '+%F %T')"
} | tee "$OUT"
[ "$(cat /sys/class/power_supply/ADP0/online 2>/dev/null)" = 1 ] || { echo "!! 未插电：电池供电时 EC 会限制功耗，结果不可比。请插上电源后重新运行。"; rm -f "$OUT"; exit 1; }

echo; echo "[1/5] CPU 单核 (7z, 1 线程) ..."
cool 20
st=$(7z b -mmt1 2>/dev/null | awk '/^Tot:/{print $NF}')
echo "  单核得分: $st"

echo; echo "[2/5] CPU 多核 (7z, 全部线程) ..."
cool 30
mt=$(7z b 2>/dev/null | awk '/^Tot:/{print $NF}')
echo "  多核得分: $mt  (结束时 $(temp)°C)"

SCENES="-b terrain -b refract -b jellyfish -b shading:shading=phong"
echo; echo "[3/5] 核显 3D (glmark2 1080p, CPU 空闲) ..."
cool 40
g1=$($GL $SCENES 2>/dev/null | tee gl-idle.log | fps)
echo "  平均 FPS: $g1  (结束时 $(temp)°C)"

echo; echo "[4/5] 游戏模拟 (glmark2 + 6 线程 CPU 负载) ..."
cool 40
stress-ng --cpu 6 --cpu-method matrixprod --timeout 0 >/dev/null 2>&1 & sp=$!
sleep 3
g2=$($GL $SCENES 2>/dev/null | tee gl-load.log | fps)
kill $sp 2>/dev/null; wait $sp 2>/dev/null
echo "  平均 FPS: $g2  (结束时 $(temp)°C)"

echo; echo "[5/5] 持续多核 3 分钟 (模拟编译大型工程) ..."
cool 40
sus=$(stress-ng --cpu 0 --cpu-method int64 --metrics-brief --timeout 180 2>&1 | awk '/metrc:.* cpu /{print int($(NF-1))}')
echo "  持续吞吐: $sus bogo ops/s  (结束时 $(temp)°C)"

{
echo
echo "================ 结果 ($K) ================"
echo "CPU 单核 (7z MIPS)        : $st"
echo "CPU 多核 (7z MIPS)        : $mt"
echo "核显 FPS (CPU 空闲)       : $g1"
echo "核显 FPS (CPU 同时满载)   : $g2"
echo "持续多核 3 分钟 (ops/s)   : $sus"
} | tee -a "$OUT"
echo; echo "结果已保存到 tools/$OUT"
