#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""逐秒记录守护进程状态到 CSV（无需 sudo、只读，通过 xiaoxin-fand 的 D-Bus 接口）。

每行：温度、平滑温度、目标/实际转速、控制原因、封装功耗、功耗保护上限、MSR/MMIO PL1、平均频率、预设、电源。
适合在真实负载（如大型编译）时开着，事后分析功耗保护与风扇的长期表现。

用法:
  python3 tools/record-trace.py trace.csv                       # 一直记录，Ctrl-C 结束
  python3 tools/record-trace.py trace.csv --seconds 600         # 记录 10 分钟
  python3 tools/record-trace.py trace.csv --burst 4,3,20,4      # 同时产生突发负载：4 线程 × 3 s，每 20 s 一次，共 4 次
"""
import argparse
import csv
import json
import subprocess
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

LOOP = "x=0; while :; do x=$(( (x*7+12345) % 1000003 )); done"
RAPL = "/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"
RAPL_MMIO = "/sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw"
FIELDS = ["t", "load", "cpu", "cpu_s", "sen1", "sen2", "nvme0", "target", "fan1", "fan2", "reason",
          "pkg_w", "cap", "msr_pl1", "mmio_pl1", "mhz", "preset", "source"]


def status(bus):
    v = bus.call_sync("io.github.XiaoxinFan", "/io/github/XiaoxinFan", "io.github.XiaoxinFan1",
                      "GetStatus", None, None, Gio.DBusCallFlags.NONE, 3000, None)
    return json.loads(v.unpack()[0])


def watts(path):
    try:
        with open(path) as f:
            return int(f.read()) / 1e6
    except (OSError, ValueError):
        return None


def mhz():
    vals = [float(line.split(":")[1]) for line in open("/proc/cpuinfo") if line.startswith("cpu MHz")]
    return round(sum(vals) / len(vals)) if vals else None


def burst_spec(text):
    try:
        n, on, every, times = (int(x) for x in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("格式: 线程数,持续秒,间隔秒,次数") from None
    if n < 1 or on < 1 or every <= on or times < 1:
        raise argparse.ArgumentTypeError("须满足 线程数 ≥1、持续 ≥1、间隔 > 持续、次数 ≥1")
    return {"n": n, "on": on, "every": every, "times": times}


def row(s, elapsed, load):
    t = s.get("temps") or {}
    fans = s.get("fans") or [None, None]
    w = s.get("pkg_w")
    return [round(elapsed, 1), load, t.get("cpu"), s.get("cpu_smoothed"), t.get("sen1"), t.get("sen2"),
            t.get("nvme0"), s.get("target"), fans[0], fans[1], s.get("reason"),
            round(w, 1) if isinstance(w, (int, float)) else None, s.get("power_cap"),
            watts(RAPL), watts(RAPL_MMIO), mhz(), s.get("preset"), s.get("source")]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", help="输出 CSV 文件")
    ap.add_argument("--seconds", type=float, default=None, help="记录时长（默认一直记录到 Ctrl-C）")
    ap.add_argument("--burst", type=burst_spec, default=None, metavar="N,ON,EVERY,TIMES",
                    help="同时产生突发负载（第 10 s 起）；记录到最后一次突发后 30 s 为止")
    args = ap.parse_args()

    secs = args.seconds if args.seconds is not None else float("inf")
    b = args.burst
    if b:
        secs = min(secs, 10 + b["every"] * b["times"] + 30)
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    procs, t0, rows = [], time.monotonic(), 0
    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        try:
            while (el := time.monotonic() - t0) < secs:
                if b:
                    k, phase = divmod(el - 10, b["every"])
                    want = el >= 10 and k < b["times"] and phase < b["on"]
                    if want and not procs:
                        procs = [subprocess.Popen(["bash", "-c", LOOP]) for _ in range(b["n"])]
                    elif not want and procs:
                        for p in procs:
                            p.kill()
                        procs = []
                try:
                    w.writerow(row(status(bus), el, len(procs)))
                    f.flush()
                    rows += 1
                except GLib.Error as e:          # 守护进程重启等：跳过这一秒
                    print(f"{el:6.0f}s  读取状态失败: {e.message}", flush=True)
                time.sleep(max(0.0, 1 - (time.monotonic() - t0 - el)))
        except KeyboardInterrupt:
            pass
        finally:
            for p in procs:
                p.kill()
    print(f"已写入 {args.output}（{rows} 行）")


if __name__ == "__main__":
    main()
