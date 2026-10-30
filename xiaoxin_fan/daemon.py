# SPDX-License-Identifier: GPL-3.0-or-later
"""xiaoxin-fand：风扇控制守护进程。

  · 每秒：读温度 → Controller 计算目标 → 目标变化时写 EC（另每 30 秒重写一次）
  · D-Bus：io.github.XiaoxinFan（系统总线；--fake 时用会话总线）
  · 休眠：持有 logind 延迟锁，休眠前交还 EC，唤醒后重新应用预设
  · 跟随 GNOME 电源菜单：platform_profile 被外部修改时切换到对应预设
  · 插拔电源：插电 / 电池各记住一个预设，电源稳定变化后自动切换
  · 退出（SIGTERM/SIGINT/异常）：交还 EC
"""

import argparse
import json
import logging
import os
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .control import Controller, Curve, PowerGuard, SourceSwitch  # noqa: E402
from .hw import HardwareError  # noqa: E402
from .presets import PROFILE_TO_PRESET, USER_PRESETS, load_presets, resolve  # noqa: E402

BUS_NAME = "io.github.XiaoxinFan"
OBJECT_PATH = "/io/github/XiaoxinFan"
IFACE = "io.github.XiaoxinFan1"
STATE_FILE = "/var/lib/xiaoxin-fan/state.json"
REWRITE_INTERVAL = 30.0
STARTUP_SETTLE = 10.0     # 启动后这段时间内不跟随 platform_profile 变化（等 power-profiles-daemon 恢复状态）

INTROSPECTION = f"""
<node>
  <interface name="{IFACE}">
    <method name="GetStatus"><arg type="s" name="json" direction="out"/></method>
    <method name="ListPresets"><arg type="s" name="json" direction="out"/></method>
    <method name="SetPreset">
      <arg type="s" name="name" direction="in"/>
      <arg type="s" name="active" direction="out"/>
    </method>
    <signal name="StatusChanged"><arg type="s" name="json"/></signal>
  </interface>
</node>
"""

log = logging.getLogger("xiaoxin-fand")


class Daemon:
    def __init__(self, hw, bus_type, state_file, presets_path):
        self.hw = hw
        self.bus_type = bus_type
        self.state_file = state_file
        self.presets = load_presets(presets_path)
        state = self._load_state()
        self.preset = state["preset"]
        self.switch = SourceSwitch({k: v for k, v in state["by_source"].items() if v in self.presets})
        self.ctrl = Controller(None)
        self.pguard = PowerGuard()
        self.power_cap = None          # 当前生效的功耗上限（W），None = 原厂
        self.power_guard_ok = True     # 写入失败（如被 BIOS 锁定）后关闭功耗保护
        self.written = None
        self.last_write = 0.0
        self.expected_profile = None
        self.paused = False
        self.error = None
        self.started = time.monotonic()
        self.status = {}
        self.conn = None
        self.inhibit_fd = None
        self.loop = GLib.MainLoop()

    # ---------- 状态持久化 ----------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        name = data.get("preset", "stock")
        by_source = data.get("by_source")
        return {"preset": name if name in self.presets else "stock",
                "by_source": by_source if isinstance(by_source, dict) else {}}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"preset": self.preset, "by_source": self.switch.remembered}, f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("无法保存状态: %s", e)

    # ---------- 预设 ----------
    def apply_preset(self, name, persist=True):
        p = self.presets[name]
        self.hw.set_extras([])
        if p["profile"]:
            self.hw.set_profile(p["profile"])
        self.hw.set_extras(p["extras"])
        self.expected_profile = self.hw.get_profile()
        self.ctrl = Controller(Curve(p["curve"]) if p["curve"] else None, p["ec_below"])
        self.written = None                         # 强制下个周期写入
        self.preset = name
        if persist:
            self._save_state()
        log.info("应用预设 %s（%s）profile=%s extras=%s", name, p["label"], self.expected_profile, p["extras"])

    def _follow_profile(self):
        prof = self.hw.get_profile()
        if self.expected_profile is None or prof == self.expected_profile:
            return
        if time.monotonic() - self.started < STARTUP_SETTLE:
            log.info("启动期间 platform_profile 被改为 %s，重新应用 %s", prof, self.preset)
            self.apply_preset(self.preset, persist=False)
        elif self.preset == "stock":
            self.expected_profile = prof
        else:
            target = PROFILE_TO_PRESET.get(prof)
            log.info("电源模式被外部切换为 %s → 预设 %s", prof, target)
            if target:
                self.apply_preset(target)
            else:
                self.expected_profile = prof

    def _follow_source(self, now):
        target = self.switch.step(self.hw.power_source(), now, self.preset)
        if target and target != self.preset and target in self.presets:
            log.info("电源切换为%s → 预设 %s", "插电" if self.switch.source == "ac" else "电池", target)
            self.apply_preset(target)

    # ---------- 控制循环 ----------
    def _write(self, target):
        self.hw.set_rpm(target)
        self.written, self.last_write = target, time.monotonic()

    def tick(self):
        if self.paused:
            return True
        now = time.monotonic()
        try:
            self._follow_source(now)
            self._follow_profile()
            temps = self.hw.read_temps()
            d = self.ctrl.step(temps, now)
            if d.target != self.written or (d.target and now - self.last_write >= REWRITE_INTERVAL):
                self._write(d.target)
            self._power_step(temps, now)
            fans = self.hw.read_rpm()
            self.error = None
            self._publish(temps, d, fans)
        except Exception as e:  # noqa: BLE001 —— 任何异常都先确保交还 EC
            self.error = str(e)
            log.error("控制周期出错，交还 EC: %s", e)
            self.ctrl.reset()
            try:
                self._write(0)
            except Exception as e2:  # noqa: BLE001
                log.error("交还 EC 失败: %s", e2)
                self.written = None
            self._publish({}, None, None)
        return True

    def _set_power_cap(self, cap):
        if cap == self.power_cap:
            return
        try:
            self.hw.set_power_cap(cap)
        except Exception as e:  # noqa: BLE001 —— 功耗保护失败不应影响风扇控制
            log.error("功耗保护不可用，已关闭: %s", e)
            self.power_guard_ok = False
            self.pguard.reset()
            return
        if cap is None:
            log.info("SEN 已回落，功耗上限恢复原厂")
        elif self.power_cap is None or abs(cap - self.power_cap) >= 5:
            log.info("SEN 超过 %.0f°C → 功耗上限 %dW", self.pguard.limit, cap)
        self.power_cap = cap

    def _power_step(self, temps, now):
        if self.preset == "stock" or not self.power_guard_ok:
            self._set_power_cap(None)
            return
        self._set_power_cap(self.pguard.step(temps, now))

    def _publish(self, temps, d, fans):
        p = self.presets[self.preset]
        self.status = {
            "preset": self.preset, "label": p["label"],
            "profile": self.expected_profile, "extras": p["extras"],
            "fcmo": self._safe(self.hw.fcmo),
            "temps": temps, "cpu_smoothed": d.cpu_smoothed if d else None,
            "fans": list(fans) if fans else None,
            "target": self.written, "reason": d.reason if d else "error",
            "pkg_w": self._safe(self.hw.package_watts),
            "power_cap": self.power_cap,
            "source": self.switch.source, "by_source": self.switch.remembered,
            "paused": self.paused, "error": self.error, "time": time.time(),
        }
        if self.conn:
            self.conn.emit_signal(None, OBJECT_PATH, IFACE, "StatusChanged",
                                  GLib.Variant("(s)", (json.dumps(self.status, ensure_ascii=False),)))

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    def release(self, why):
        self.pguard.reset()
        self._set_power_cap(None)
        try:
            self.hw.release()
            self.written = 0
            log.info("已交还 EC（%s）", why)
        except Exception as e:  # noqa: BLE001
            log.error("交还 EC 失败（%s）: %s", why, e)

    # ---------- D-Bus ----------
    def _on_method(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "GetStatus":
                invocation.return_value(GLib.Variant("(s)", (json.dumps(self.status, ensure_ascii=False),)))
            elif method == "ListPresets":
                data = {k: {"label": v["label"], "description": v["description"], "profile": v["profile"],
                            "extras": v["extras"], "curve": v["curve"],
                            "ec_below": v["ec_below"]} for k, v in self.presets.items()}
                invocation.return_value(GLib.Variant("(s)", (json.dumps(data, ensure_ascii=False),)))
            elif method == "SetPreset":
                name = resolve(self.presets, params.unpack()[0])
                self.switch.remember(self.switch.source, name)      # 手动选择 → 记为当前电源下的预设
                self.apply_preset(name)
                self.tick()
                invocation.return_value(GLib.Variant("(s)", (name,)))
        except KeyError as e:
            invocation.return_dbus_error(f"{IFACE}.Error.UnknownPreset", f"未知预设: {e}")
        except Exception as e:  # noqa: BLE001
            invocation.return_dbus_error(f"{IFACE}.Error.Failed", str(e))

    def _on_bus_acquired(self, conn, name):
        self.conn = conn
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        conn.register_object(OBJECT_PATH, node.interfaces[0], self._on_method, None, None)

    def _on_name_lost(self, conn, name):
        log.error("无法获得总线名 %s（已有实例在运行？）", name)
        self.loop.quit()

    # ---------- 休眠 ----------
    def _take_inhibitor(self):
        if self.bus_type != Gio.BusType.SYSTEM:
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            res, fds = bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                "Inhibit", GLib.Variant("(ssss)", ("sleep", "xiaoxin-fan", "休眠前把风扇交还给 EC", "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
            self.inhibit_fd = fds.steal_fds()[res.unpack()[0]]
        except GLib.Error as e:
            log.warning("无法获取休眠延迟锁: %s", e.message)

    def _on_prepare_for_sleep(self, conn, sender, path, iface, signal, params):
        going = params.unpack()[0]
        if going:
            self.paused = True
            self.release("即将休眠")
            if self.inhibit_fd is not None:
                os.close(self.inhibit_fd)
                self.inhibit_fd = None
        else:
            log.info("已唤醒，重新应用预设")
            self.paused = False
            self.ctrl.reset()
            GLib.timeout_add_seconds(2, self._resume)

    def _resume(self):
        try:
            self.apply_preset(self.preset, persist=False)
        except Exception as e:  # noqa: BLE001
            log.error("唤醒后应用预设失败: %s", e)
        self._take_inhibitor()
        return False

    # ---------- 主流程 ----------
    def _quit(self, why):
        self.paused = True
        self.release(why)
        try:
            self.hw.set_extras([])
        except Exception:  # noqa: BLE001
            pass
        self.loop.quit()
        return False

    def run(self):
        # 开机时按当前电源选预设（例如插电时关机、开机时用电池）
        src = self._safe(self.hw.power_source)
        self.switch.step(src, time.monotonic(), self.preset)
        if src and self.switch.preferred(src, self.preset) in self.presets:
            self.preset = self.switch.preferred(src, self.preset)
        self.apply_preset(self.preset, persist=False)
        Gio.bus_own_name(self.bus_type, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                         self._on_bus_acquired, None, self._on_name_lost)
        if self.bus_type == Gio.BusType.SYSTEM:
            sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            sysbus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager",
                                    "PrepareForSleep", "/org/freedesktop/login1", None,
                                    Gio.DBusSignalFlags.NONE, self._on_prepare_for_sleep)
            self._take_inhibitor()
        for sig in (2, 15):  # SIGINT, SIGTERM
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._quit, f"信号 {sig}")
        GLib.timeout_add_seconds(1, self.tick)
        self.tick()
        try:
            self.loop.run()
        finally:
            if not self.paused:
                self._quit("退出")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="xiaoxin-fand", description="小新风扇控制守护进程")
    ap.add_argument("--fake", action="store_true", help="模拟硬件 + 会话总线（测试用，不碰真实 EC）")
    ap.add_argument("--release", action="store_true", help="交还 EC 后退出（systemd ExecStopPost 兜底）")
    ap.add_argument("--state", default=None, help=f"状态文件（默认 {STATE_FILE}）")
    ap.add_argument("--presets", default=USER_PRESETS, help="用户预设文件")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.release:
        from .hw import release_now
        release_now()
        return 0

    if args.fake:
        from .fake import FakeHardware
        hw, bus = FakeHardware(), Gio.BusType.SESSION
        state = args.state or os.path.join(GLib.get_user_runtime_dir(), "xiaoxin-fan-fake.json")
    else:
        from .hw import Hardware, load_config
        try:
            hw = Hardware(allow_untested=load_config().get("allow_untested_model", False))
        except HardwareError as e:
            log.error("%s", e)
            return 1
        log.info("%s", hw.model_note)
        bus, state = Gio.BusType.SYSTEM, args.state or STATE_FILE
    Daemon(hw, bus, state, args.presets).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
