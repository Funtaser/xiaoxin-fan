# SPDX-License-Identifier: GPL-3.0-or-later
"""fanctl：xiaoxin-fand 命令行客户端。"""

import argparse
import json
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .daemon import BUS_NAME, IFACE, OBJECT_PATH  # noqa: E402

REASONS = {"curve": "按曲线", "ec-idle": "低温交给 EC", "stock": "EC 自动", "sensor-fault": "传感器异常→EC", "error": "出错→EC"}


def proxy(session):
    bus = Gio.BusType.SESSION if session else Gio.BusType.SYSTEM
    return Gio.DBusProxy.new_for_bus_sync(bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                                          BUS_NAME, OBJECT_PATH, IFACE, None)


def call(p, method, *args):
    sig = "(" + "s" * len(args) + ")"
    v = p.call_sync(method, GLib.Variant(sig, args) if args else None, Gio.DBusCallFlags.NONE, 5000, None)
    return v.unpack()[0]


def reason_text(r):
    if r and r.startswith("guard:"):
        return f"安全保护（{r[6:]}）"
    return REASONS.get(r, r or "-")


def fmt(s):
    if not s:
        return "（暂无状态）"
    t = s.get("temps") or {}
    temps = "  ".join(f"{k} {v:.0f}°C" for k, v in t.items() if v is not None)
    fans = s.get("fans") or ["-", "-"]
    target = s.get("target")
    tgt = "EC 自动" if not target else f"{target}"
    w = s.get("pkg_w")
    lines = [
        f"预设     {s['label']}（{s['preset']}）   EC 档位 {s.get('profile')} {'+' + '+'.join(s['extras']) if s.get('extras') else ''}  FCMO={s.get('fcmo')}",
        f"风扇     {fans[0]} / {fans[1]} rpm   目标 {tgt}   {reason_text(s.get('reason'))}",
        f"温度     {temps}",
        (f"功耗     {w:.1f} W" if isinstance(w, (int, float)) else "功耗     -")
        + (f"   功耗保护：上限 {s['power_cap']} W（主板温度过高）" if s.get("power_cap") else ""),
    ]
    if s.get("paused"):
        lines.append("状态     已暂停（休眠/退出中）")
    if s.get("error"):
        lines.append(f"错误     {s['error']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fanctl", description="小新风扇控制")
    ap.add_argument("--session", action="store_true", help="连接会话总线上的模拟守护进程（测试用）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="显示当前状态（默认）")
    sub.add_parser("list", help="列出预设")
    sp = sub.add_parser("set", help="切换预设，如 fanctl set 性能")
    sp.add_argument("preset")
    sub.add_parser("watch", help="实时刷新状态")
    args = ap.parse_args(argv)

    try:
        p = proxy(args.session)
        if args.cmd in (None, "status"):
            print(fmt(json.loads(call(p, "GetStatus"))))
        elif args.cmd == "list":
            for k, v in json.loads(call(p, "ListPresets")).items():
                curve = " ".join(f"{t:g}°→{r}" for t, r in v["curve"]) if v["curve"] else "EC 自动"
                if v.get("ec_below"):
                    curve = f"<{v['ec_below']}°C 交给 EC；" + curve
                print(f"{v['label']:<4} {k:<12} {v['description']}\n{'':17}曲线: {curve}")
        elif args.cmd == "set":
            name = call(p, "SetPreset", args.preset)
            time.sleep(1.2)
            print(f"已切换到 {name}\n" + fmt(json.loads(call(p, "GetStatus"))))
        elif args.cmd == "watch":
            loop = GLib.MainLoop()

            def on_signal(_p, _sender, signal, params):
                if signal == "StatusChanged":
                    print("\033[2J\033[H" + fmt(json.loads(params.unpack()[0])) + "\n\n(Ctrl+C 退出)", flush=True)

            p.connect("g-signal", on_signal)
            try:
                loop.run()
            except KeyboardInterrupt:
                pass
    except GLib.Error as e:
        remote = Gio.DBusError.get_remote_error(e) or ""
        Gio.DBusError.strip_remote_error(e)
        msg = e.message
        if "ServiceUnknown" in remote or "was not provided" in msg:
            msg = "守护进程未运行（sudo systemctl start xiaoxin-fand）"
        elif "AccessDenied" in remote:
            msg = "没有权限：切换预设需要 sudo 组成员"
        print(f"fanctl: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
