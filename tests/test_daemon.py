# SPDX-License-Identifier: GPL-3.0-or-later
"""守护进程逻辑（模拟硬件，不连 D-Bus）。需要 python3-gi，没有时跳过。"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from gi.repository import Gio
    from xiaoxin_fan import daemon
    from xiaoxin_fan.fake import FakeHardware
except ImportError:          # CI 中没有 python3-gi
    daemon = None


@unittest.skipIf(daemon is None, "需要 python3-gi")
class ExternalSwitchTest(unittest.TestCase):
    """Fn+Q 等外部切换：何时记为当前电源下的预设。"""

    def make(self, source="ac", battery=80):
        hw = FakeHardware()
        hw.source = source
        hw.battery_percent = lambda: battery
        tmp = tempfile.mkdtemp()
        d = daemon.Daemon(hw, Gio.BusType.SESSION, os.path.join(tmp, "state.json"), os.path.join(tmp, "none.json"))
        d.started -= daemon.STARTUP_SETTLE + 1          # 跳过启动静默期
        d.switch.step(source, 0, d.preset)              # 已确认的电源
        d.apply_preset("geek", persist=False)
        return d, hw

    def press_fn_q(self, d, hw, profile):
        hw.profile = profile
        d._follow_profile()

    def test_fn_q_is_remembered(self):
        d, hw = self.make("ac")
        self.press_fn_q(d, hw, "balanced")
        self.assertEqual(d.preset, "balanced")
        self.assertEqual(d.switch.remembered.get("ac"), "balanced")

    def test_low_battery_power_saver_not_remembered(self):
        d, hw = self.make("battery", battery=18)
        d.switch.remember("battery", "balanced")
        self.press_fn_q(d, hw, "low-power")
        self.assertEqual(d.preset, "quiet")                          # 仍然跟随
        self.assertEqual(d.switch.remembered["battery"], "balanced")  # 但不记住

    def test_power_saver_with_enough_battery_is_remembered(self):
        d, hw = self.make("battery", battery=60)
        self.press_fn_q(d, hw, "low-power")
        self.assertEqual(d.switch.remembered.get("battery"), "quiet")

    def test_not_remembered_while_plugging(self):
        d, hw = self.make("battery")
        d.switch.remember("battery", "balanced")
        d.switch.step("ac", time.monotonic(), d.preset)              # 刚插电，尚未确认
        self.press_fn_q(d, hw, "performance")
        self.assertEqual(d.switch.remembered["battery"], "balanced")
        self.assertNotIn("ac", d.switch.remembered)



@unittest.skipIf(daemon is None, "需要 python3-gi")
class PowerStepTest(unittest.TestCase):
    def test_package_power_read_once_per_tick_and_reported(self):
        hw = FakeHardware()
        tmp = tempfile.mkdtemp()
        d = daemon.Daemon(hw, Gio.BusType.SESSION, os.path.join(tmp, "state.json"), os.path.join(tmp, "none.json"))
        d.apply_preset("geek", persist=False)
        calls, real = [], hw.package_watts
        hw.package_watts = lambda: calls.append(1) or real()     # 有状态的读数，同一周期读两次会得到错误的值
        d.tick()
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(d.status["pkg_w"], float)
        self.assertEqual(set(d.status["pl1"]), {"msr", "mmio"})

    def test_stock_resets_guard(self):
        hw = FakeHardware()
        tmp = tempfile.mkdtemp()
        d = daemon.Daemon(hw, Gio.BusType.SESSION, os.path.join(tmp, "state.json"), os.path.join(tmp, "none.json"))
        d.apply_preset("geek", persist=False)
        d.pguard.step({"sen1": 85.0}, 0.0, 65.0)
        d.apply_preset("stock", persist=False)
        d.tick()
        self.assertIsNone(d.pguard.cap)
        self.assertIsNone(hw.power_cap)


if __name__ == "__main__":
    unittest.main()
