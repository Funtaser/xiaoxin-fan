#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""对比两个风扇预设在中等负载下的表现（无需 sudo，通过 xiaoxin-fand 的 D-Bus 接口）。
用法: python3 tools/compare-presets.py [预设A 预设B]   默认: 原厂 性能
每个预设：空闲 25 s → 4 线程负载 60 s；统计“突发”（负载前 10 s）与“持续”（最后 30 s）。
"""
import json
import os
import subprocess
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

IDLE, LOAD, THREADS, BURST, SUSTAIN = 25, 60, 4, 10, 30

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
    return {"cpu": (s.get("temps") or {}).get("cpu"), "fan": (s.get("fans") or [0])[0],
            "w": s.get("pkg_w"), "mhz": mhz()}


def avg(rows, key):
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else float("nan")


def run(preset):
    name = call("SetPreset", preset)
    print(f"\n== {preset}（{name}）  空闲 {IDLE}s …", flush=True)
    time.sleep(IDLE)
    start = sample()
    print(f"   起始 CPU {start['cpu']:.0f}°C  风扇 {start['fan']}  → {THREADS} 线程负载 {LOAD}s", flush=True)
    loads = [subprocess.Popen(["bash", "-c", "x=0; while :; do x=$(( (x*7+12345) % 1000003 )); done"])
             for _ in range(THREADS)]
    rows, t0 = [], time.monotonic()
    try:
        while (el := time.monotonic() - t0) < LOAD:
            time.sleep(1)
            r = sample()
            r["t"] = el
            rows.append(r)
            if int(el) % 10 == 0:
                print(f"   {el:4.0f}s  CPU {r['cpu']:3.0f}°C  风扇 {r['fan']:5}  {r['w'] or 0:5.1f}W  {r['mhz']:5.0f}MHz", flush=True)
    finally:
        for p in loads:
            p.kill()
    burst = [r for r in rows if r["t"] <= BURST]
    sus = [r for r in rows if r["t"] >= LOAD - SUSTAIN]
    return {k: (avg(burst, k), avg(sus, k)) for k in ("cpu", "fan", "w", "mhz")}


def main():
    a, b = (sys.argv[1:3] if len(sys.argv) >= 3 else ("原厂", "性能"))
    original = json.loads(call("GetStatus"))["preset"]
    try:
        res = {p: run(p) for p in (a, b)}
    finally:
        call("SetPreset", original)
        print(f"\n已切回原预设 {original}")

    print("\n================ 结果 ================")
    print(f"{'':10}{'突发（前 10 秒）':^34}{'持续（最后 30 秒）':^34}")
    print(f"{'':10}" + f"{'温度':>7}{'风扇':>8}{'功耗':>8}{'频率':>9}" * 2)
    for p, r in res.items():
        line = f"{p:10}"
        for i in (0, 1):
            line += f"{r['cpu'][i]:6.0f}°{r['fan'][i]:8.0f}{r['w'][i]:7.1f}W{r['mhz'][i]:9.0f}"
        print(line)
    fa, fb = res[a]["mhz"], res[b]["mhz"]
    print(f"\n{b} 相对 {a}：突发频率 {100 * (fb[0] / fa[0] - 1):+.1f}%   持续频率 {100 * (fb[1] / fa[1] - 1):+.1f}%"
          f"   持续温度 {res[b]['cpu'][1] - res[a]['cpu'][1]:+.0f}°C")


if __name__ == "__main__":
    main()
