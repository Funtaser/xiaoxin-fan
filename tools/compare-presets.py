#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""对比风扇预设在负载下的表现（无需 sudo，通过 xiaoxin-fand 的 D-Bus 接口切换预设、读取状态）。

用法:
  python3 tools/compare-presets.py                              # 原厂 vs 性能，4 线程 60 s
  python3 tools/compare-presets.py 极客 --threads 0 --load 180  # 单个预设满载 3 分钟（0 = 全部线程）
每个预设：空闲 → 负载；统计“突发”（负载前 10 s）与“持续”（负载 ≥120 s 时取第 60 s 起，否则最后 30 s）。
"""
import argparse
import json
import os
import subprocess
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def call(method, *args):
    v = bus.call_sync("io.github.XiaoxinFan", "/io/github/XiaoxinFan", "io.github.XiaoxinFan1", method,
                      GLib.Variant("(" + "s" * len(args) + ")", args) if args else None,
                      None, Gio.DBusCallFlags.NONE, 5000, None)
    return v.unpack()[0]


def mhz():
    vals = [float(l.split(":")[1]) for l in open("/proc/cpuinfo") if l.startswith("cpu MHz")]
    return sum(vals) / len(vals)


def sample():
    s = json.loads(call("GetStatus"))
    t = s.get("temps") or {}
    return {"cpu": t.get("cpu"), "sen1": t.get("sen1"), "fan": (s.get("fans") or [0])[0],
            "w": s.get("pkg_w"), "mhz": mhz(), "reason": s.get("reason"), "cap": s.get("power_cap")}


def avg(rows, key):
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else float("nan")


def run(preset, args):
    name = call("SetPreset", preset)
    print(f"\n== {preset}（{name}）  空闲 {args.idle}s …", flush=True)
    time.sleep(args.idle)
    start = sample()
    n = args.threads or os.cpu_count()
    print(f"   起始 CPU {start['cpu']:.0f}°C  SEN1 {start['sen1']:.0f}°C  风扇 {start['fan']}"
          f"  → {n} 线程负载 {args.load}s", flush=True)
    loads = [subprocess.Popen(["bash", "-c", "x=0; while :; do x=$(( (x*7+12345) % 1000003 )); done"])
             for _ in range(n)]
    rows, t0 = [], time.monotonic()
    try:
        while (el := time.monotonic() - t0) < args.load:
            time.sleep(1)
            r = sample()
            r["t"] = el
            rows.append(r)
            if args.abort_sen1 and isinstance(r["sen1"], (int, float)) and r["sen1"] >= args.abort_sen1:
                print(f"   !! SEN1 {r['sen1']:.0f}°C ≥ {args.abort_sen1}°C，中止负载", flush=True)
                break
            if int(el) % 10 == 0:
                print(f"   {el:4.0f}s  CPU {r['cpu']:3.0f}°C  SEN1 {r['sen1']:3.0f}°C  风扇 {r['fan']:5}"
                      f"  {r['w'] or 0:5.1f}W  {r['mhz']:5.0f}MHz  {r['reason']}"
                      + (f"  上限 {r['cap']}W" if r["cap"] else ""), flush=True)
    finally:
        for p in loads:
            p.kill()
    burst = [r for r in rows if r["t"] <= 10]
    since = 60 if args.load >= 120 else args.load - 30
    sus = [r for r in rows if r["t"] >= since]
    res = {k: (avg(burst, k), avg(sus, k)) for k in ("cpu", "sen1", "fan", "w", "mhz")}
    res["sen1_max"] = max((r["sen1"] for r in rows if r["sen1"] is not None), default=float("nan"))
    res["guard_s"] = sum(1 for r in rows if str(r["reason"]).startswith("guard"))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("presets", nargs="*", default=["原厂", "性能"])
    ap.add_argument("--threads", type=int, default=4, help="负载线程数，0 = 全部（默认 4）")
    ap.add_argument("--load", type=int, default=60, help="负载秒数（默认 60）")
    ap.add_argument("--idle", type=int, default=25, help="每个预设前的空闲秒数（默认 25）")
    ap.add_argument("--abort-sen1", type=float, default=92, help="SEN1 达到该温度立即停止负载（默认 92，0 = 不中止）")
    args = ap.parse_args()

    original = json.loads(call("GetStatus"))["preset"]
    try:
        res = {p: run(p, args) for p in args.presets}
    finally:
        call("SetPreset", original)
        print(f"\n已切回原预设 {original}")

    print("\n================ 结果 ================")
    print(f"{'':8}{'突发（前 10 秒）':^30}{'持续':^30}{'SEN1 峰值':>10}{'保护介入':>8}")
    print(f"{'':8}" + f"{'温度':>6}{'风扇':>7}{'功耗':>8}{'频率':>8}" * 2)
    for p, r in res.items():
        line = f"{p:8}"
        for i in (0, 1):
            line += f"{r['cpu'][i]:5.0f}°{r['fan'][i]:7.0f}{r['w'][i]:7.1f}W{r['mhz'][i]:8.0f}"
        print(line + f"{r['sen1_max']:9.0f}°C{r['guard_s']:7}s")
    if len(res) >= 2:
        a, b = list(res)[:2]
        fa, fb = res[a]["mhz"], res[b]["mhz"]
        print(f"\n{b} 相对 {a}：突发频率 {100 * (fb[0] / fa[0] - 1):+.1f}%   持续频率 {100 * (fb[1] / fa[1] - 1):+.1f}%"
              f"   持续温度 {res[b]['cpu'][1] - res[a]['cpu'][1]:+.0f}°C")


if __name__ == "__main__":
    main()
