# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from xiaoxin_fan.control import (Controller, Curve, CPU_HOT_HOLD, INVALID_GRACE,  # noqa: E402
                                 POWER_MIN_W, POWER_START_W, PowerGuard)
from xiaoxin_fan.hw import check_model, read_dmi  # noqa: E402
from xiaoxin_fan.presets import DEFAULT_PRESETS, load_presets, resolve  # noqa: E402

CURVE = Curve([[0, 2000], [60, 2000], [80, 4000], [90, 5000]])


def run(ctrl, temps_seq, t0=0.0):
    """按 1 秒间隔喂入温度序列，返回每步决策。"""
    return [ctrl.step(t if isinstance(t, dict) else {"cpu": t}, t0 + i) for i, t in enumerate(temps_seq)]


class CurveTest(unittest.TestCase):
    def test_interpolation_and_clamp(self):
        self.assertEqual(CURVE.at(30), 2000)
        self.assertEqual(CURVE.at(70), 3000)
        self.assertEqual(CURVE.at(85), 4500)
        self.assertEqual(CURVE.at(120), 5000)

    def test_rejects_unsafe_rpm(self):
        with self.assertRaises(ValueError):
            Curve([[0, 1]])            # 1 = 停转
        with self.assertRaises(ValueError):
            Curve([[0, 7000]])


class ControllerTest(unittest.TestCase):
    def test_stock_always_releases(self):
        d = Controller(None).step({"cpu": 99}, 0)
        self.assertEqual((d.target, d.reason), (0, "stock"))

    def test_steady_temperature_follows_curve(self):
        d = run(Controller(CURVE), [70] * 10)[-1]
        self.assertEqual((d.target, d.reason), (3000, "curve"))

    def test_rise_is_fast_fall_is_rate_limited(self):
        c = Controller(CURVE)
        up = run(c, [50] * 3 + [90] * 15)
        self.assertGreaterEqual(up[-1].target, 4900)
        down = run(c, [50] * 5, t0=len(up))
        drops = [a.target - b.target for a, b in zip(up[-1:] + down, down)]
        self.assertTrue(all(d <= 100 for d in drops), drops)

    def test_rate_limit_after_stall(self):
        c = Controller(CURVE)
        run(c, [90] * 15)
        d = c.step({"cpu": 40}, 1000)                         # 卡顿近 1000 秒后
        self.assertGreaterEqual(d.target, 5000 - 200)

    def test_hysteresis_holds_small_dips(self):
        c = Controller(CURVE)
        run(c, [75] * 15)
        before = c._last
        after = run(c, [73.5] * 15, t0=100)[-1].target     # 降 1.5°C < 回差 3°C
        self.assertEqual(after, before)

    def test_sensor_fault_releases_after_grace(self):
        c = Controller(CURVE)
        run(c, [70] * 5)
        res = run(c, [{"cpu": None}] * int(INVALID_GRACE + 2), t0=10)
        self.assertNotEqual(res[0].target, 0)               # 短暂丢失：维持
        self.assertEqual((res[-1].target, res[-1].reason), (0, "sensor-fault"))

    def test_fault_on_first_sample_releases_immediately(self):
        d = Controller(CURVE).step({"cpu": None}, 0)
        self.assertEqual(d.target, 0)

    def test_board_sensor_guard(self):
        d = run(Controller(CURVE), [{"cpu": 50, "sen1": 71}] * 3)[-1]
        self.assertEqual(d.reason, "curve")                    # 原厂工作区间内不介入
        d = run(Controller(CURVE), [{"cpu": 50, "sen1": 73}] * 3)[-1]
        self.assertEqual((d.target, d.reason), (4500, "guard:sen1"))
        d = run(Controller(CURVE), [{"cpu": 50, "sen2": 77}] * 3)[-1]
        self.assertEqual(d.target, 6000)

    def test_nvme_guard(self):
        d = run(Controller(CURVE), [{"cpu": 50, "nvme1": 81}] * 3)[-1]
        self.assertEqual((d.target, d.reason), (6000, "guard:nvme1"))

    def test_invalid_board_sensor_ignored(self):
        d = run(Controller(CURVE), [{"cpu": 50, "sen3": None}] * 3)[-1]
        self.assertEqual(d.reason, "curve")

    def test_cpu_hot_floor_needs_duration(self):
        quiet = Curve([[0, 2000], [100, 2500]])
        res = run(Controller(quiet), [99] * int(CPU_HOT_HOLD + 3))
        self.assertLess(res[5].target, 4500)
        self.assertEqual((res[-1].target, res[-1].reason), (4500, "guard:cpu"))

    def test_output_always_safe(self):
        c = Controller(CURVE)
        for i, t in enumerate([20, 200, -10, 95, 60, None, 100, 30]):
            d = c.step({"cpu": t}, i)
            self.assertTrue(d.target == 0 or 2000 <= d.target <= 6000)
            self.assertEqual(d.target % 100, 0)


class EcIdleTest(unittest.TestCase):
    def test_hands_back_when_cool_and_takes_over_when_warm(self):
        c = Controller(CURVE, ec_below=55)
        cool = run(c, [45] * 5)
        self.assertEqual((cool[-1].target, cool[-1].reason), (0, "ec-idle"))
        warm = run(c, [70] * 10, t0=5)
        self.assertEqual(warm[-1].reason, "curve")
        self.assertGreater(warm[-1].target, 0)

    def test_hysteresis_band_keeps_state(self):
        c = Controller(CURVE, ec_below=55)
        run(c, [60] * 10)                                       # 接管
        self.assertEqual(run(c, [54] * 10, t0=10)[-1].reason, "curve")   # 54 > 53：仍接管
        self.assertEqual(run(c, [50] * 10, t0=20)[-1].target, 0)         # 50 < 53：交出
        self.assertEqual(run(c, [54] * 10, t0=30)[-1].target, 0)         # 54 < 55：仍交出

    def test_guard_overrides_ec_idle(self):
        d = run(Controller(CURVE, ec_below=55), [{"cpu": 40, "nvme0": 82}] * 3)[-1]
        self.assertEqual((d.target, d.reason), (6000, "guard:nvme0"))


class PowerGuardTest(unittest.TestCase):
    def feed(self, g, sens, t0=0):
        return [g.step({"cpu": 90, "sen1": s, "sen3": None}, t0 + i) for i, s in enumerate(sens)]

    def test_idle_below_limit_no_cap(self):
        self.assertEqual(self.feed(PowerGuard(), [70] * 10)[-1], None)

    def test_engages_and_lowers_faster_when_hotter(self):
        g = PowerGuard()
        slow = self.feed(g, [81] * 5)
        self.assertEqual(slow[0], POWER_START_W - 2)
        self.assertEqual(slow[-1], POWER_START_W - 10)
        fast = self.feed(g, [87] * 3, t0=5)
        self.assertEqual(slow[-1] - fast[-1], 15)

    def test_floor(self):
        self.assertEqual(self.feed(PowerGuard(), [95] * 60)[-1], POWER_MIN_W)

    def test_holds_in_band_then_releases_fully(self):
        g = PowerGuard()
        self.feed(g, [85] * 4)
        held = self.feed(g, [78] * 10, t0=4)                 # 77 < 78 < 80：保持
        self.assertEqual(len(set(held)), 1)
        rel = self.feed(g, [70] * 200, t0=14)
        self.assertEqual(rel[-1], None)                      # 放开到原值后撤销限制
        self.assertTrue(all(b is None or a is None or b >= a for a, b in zip(rel, rel[1:])))

    def test_missing_sensor_keeps_state(self):
        g = PowerGuard()
        self.feed(g, [85] * 3)
        cap = g.cap
        self.assertEqual(g.step({"cpu": 90}, 10), round(cap))


class ModelCheckTest(unittest.TestCase):
    BASE = {"vendor": "LENOVO", "product": "83D4", "family": "XiaoXinPro 16 IMH9", "bios": "MECN69WW"}

    def test_verified(self):
        ok, note = check_model(self.BASE)
        self.assertTrue(ok)
        self.assertIn("已验证", note)

    def test_known_model_unknown_bios_allowed_with_warning(self):
        ok, note = check_model({**self.BASE, "bios": "MECN99WW"})
        self.assertTrue(ok)
        self.assertIn("未经验证", note)

    def test_unknown_model_refused_unless_opted_in(self):
        other = {**self.BASE, "product": "82SN"}
        self.assertFalse(check_model(other)[0])
        self.assertTrue(check_model(other, allow_untested=True)[0])

    def test_read_dmi(self):
        with tempfile.TemporaryDirectory() as d:
            for k, v in {"sys_vendor": "LENOVO", "product_name": "83D4", "bios_version": "X"}.items():
                with open(os.path.join(d, k), "w") as f:
                    f.write(v + "\n")
            dmi = read_dmi(d)
        self.assertEqual((dmi["vendor"], dmi["product"], dmi["family"], dmi["bios"]), ("LENOVO", "83D4", "", "X"))


class PresetsTest(unittest.TestCase):
    def test_defaults_valid_and_resolvable(self):
        p = load_presets("/nonexistent")
        self.assertEqual(set(p), set(DEFAULT_PRESETS))
        self.assertEqual(resolve(p, "极客"), "geek")
        with self.assertRaises(KeyError):
            resolve(p, "turbo")

    def test_user_override_merges(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"quiet": {"curve": [[0, 2100], [80, 3000]]},
                       "mine": {"label": "我的", "profile": "balanced", "curve": [[0, 2500]]}}, f)
        try:
            p = load_presets(f.name)
            self.assertEqual(p["quiet"]["profile"], "low-power")       # 未覆盖的字段保留
            self.assertEqual(p["quiet"]["curve"][0], [0, 2100])
            self.assertEqual(resolve(p, "我的"), "mine")
        finally:
            os.unlink(f.name)

    def test_user_override_rejects_unsafe_curve(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"quiet": {"curve": [[0, 1]]}}, f)
        self._expect_invalid(f.name)

    def test_user_override_rejects_bad_ec_below(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"quiet": {"ec_below": 95}}, f)
        self._expect_invalid(f.name)

    def _expect_invalid(self, path):
        try:
            with self.assertRaises(ValueError):
                load_presets(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
