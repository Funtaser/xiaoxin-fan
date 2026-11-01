# SPDX-License-Identifier: GPL-3.0-or-later
import json
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from xiaoxin_fan.control import (Controller, Curve, CPU_HOT_HOLD, EC_IDLE_MIN_HOLD, EC_IDLE_TAKE_HOLD,  # noqa: E402
                                 INVALID_GRACE, UP_RATE,
                                 POWER_HARD_W_S, POWER_MIN_W, POWER_SEN_LIMIT, POWER_SEN_TARGET, POWER_START_W,
                                 PowerGuard, SOURCE_SETTLE, SourceSwitch)
from xiaoxin_fan.hw import FAN2_OFFSET, check_model, fan_targets, read_dmi  # noqa: E402
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

    def test_short_burst_rises_slowly(self):
        c = Controller(CURVE)
        run(c, [50] * 10)
        out = run(c, [76] * 3, t0=10)
        rises = [b.target - a.target for a, b in zip([c.step({"cpu": 50}, 9.5)] + out, out)]
        self.assertTrue(all(r <= UP_RATE for r in rises), rises)

    def test_stops_rising_when_burst_ends(self):
        c = Controller(CURVE)
        run(c, [50] * 10)
        out = run(c, [76, 76, 55, 52, 50, 50], t0=10)
        after = [d.target for d in out[2:]]
        self.assertTrue(all(b <= a for a, b in zip(after, after[1:])), after)
        self.assertLessEqual(max(after), out[1].target)

    def test_hot_rises_without_limit(self):
        c = Controller(CURVE)
        run(c, [50] * 10)
        out = run(c, [95] * 8, t0=10)
        self.assertEqual(out[-1].target, 5000)                    # 平滑温度过 80°C 后直接到曲线值

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

    def test_board_sensor_guard_moves_above_power_target(self):
        def last(temps):
            return run(Controller(CURVE, power_guarded=True), [temps] * 3)[-1]
        self.assertEqual(last({"cpu": 50, "sen1": 80}).reason, "curve")     # 功耗保护稳在 79–80°C：风扇不介入
        self.assertEqual((last({"cpu": 50, "sen1": 81}).target, last({"cpu": 50, "sen1": 81}).reason),
                         (4500, "guard:sen1"))
        self.assertEqual(last({"cpu": 50, "sen2": 83}).target, 6000)
        self.assertEqual(last({"cpu": 50, "nvme0": 81}).target, 6000)       # NVMe 保护线不变

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


class FanTargetsTest(unittest.TestCase):
    def test_release_both(self):
        self.assertEqual(fan_targets(0), (0, 0))

    def test_second_fan_slightly_slower_never_below_min(self):
        self.assertEqual(fan_targets(3000), (3000, 3000 - FAN2_OFFSET))
        self.assertEqual(fan_targets(6000), (6000, 6000 - FAN2_OFFSET))
        self.assertEqual(fan_targets(2100), (2100, 2000))
        self.assertEqual(fan_targets(2000), (2000, 2000))

    def test_never_stops_a_fan(self):
        for rpm in [0] + list(range(2000, 6001, 100)):
            for v in fan_targets(rpm):
                self.assertTrue(v == 0 or 2000 <= v <= 6000, (rpm, v))
        for bad in (1, 100, 1999, 6100):
            with self.assertRaises(ValueError):
                fan_targets(bad)


class EcIdleTest(unittest.TestCase):
    def test_hands_back_when_cool_and_takes_over_when_warm(self):
        c = Controller(CURVE, ec_below=55)
        cool = run(c, [45] * 5)
        self.assertEqual((cool[-1].target, cool[-1].reason), (0, "ec-idle"))
        warm = run(c, [70] * 20, t0=5)
        self.assertEqual(warm[-1].reason, "curve")
        self.assertGreater(warm[-1].target, 0)

    def test_hysteresis_band_keeps_state(self):
        c = Controller(CURVE, ec_below=55)
        run(c, [60] * 30)                                                   # 接管
        self.assertEqual(run(c, [52] * 100, t0=30)[-1].reason, "curve")     # 52 > 50：仍接管
        self.assertEqual(run(c, [45] * 60, t0=130)[-1].target, 0)           # 45 < 50 且已保持 60 s：交出
        self.assertEqual(run(c, [54] * 60, t0=190)[-1].target, 0)           # 54 < 55：仍交出

    def test_spike_does_not_take_over(self):
        c = Controller(CURVE, ec_below=50)
        run(c, [42] * 10)
        out = run(c, [42, 42, 56, 42, 42, 55, 42, 42], t0=10)              # 短时尖峰
        self.assertTrue(all(d.reason == "ec-idle" for d in out))

    def test_sustained_load_takes_over(self):
        c = Controller(CURVE, ec_below=50)
        run(c, [42] * 10)
        self.assertEqual(c.step({"cpu": 42}, 10).reason, "ec-idle")
        out = run(c, [80] * int(EC_IDLE_TAKE_HOLD + 3), t0=11)
        self.assertEqual(out[-1].reason, "curve")
        self.assertGreater(out[-1].target, 0)

    def test_short_burst_stays_with_ec(self):
        for ec_below in (50, 55, 60):
            for n in (2, 3, 5):
                c = Controller(CURVE, ec_below=ec_below)
                run(c, [ec_below - 8] * 30)
                out = run(c, [ec_below - 8, 85] + [87] * n + [ec_below] + [ec_below - 8] * 60, t0=30)
                self.assertTrue(all(d.reason == "ec-idle" for d in out), (ec_below, n))

    def test_very_hot_takes_over_immediately(self):
        c = Controller(CURVE, ec_below=50)
        run(c, [42] * 10)
        self.assertEqual(c.step({"cpu": 93}, 10).reason, "curve")

    def test_min_hold_prevents_flapping(self):
        c = Controller(CURVE, ec_below=50)
        self.assertEqual(run(c, [42] * 10)[-1].reason, "ec-idle")          # 交给 EC
        run(c, [56] * 30, t0=10)                                            # 接管
        taken = c._taken_at
        self.assertIsNotNone(taken)
        n = int(taken + EC_IDLE_MIN_HOLD - 40)
        out = run(c, [40] * n, t0=40)                                       # 很快降温，但未满保持时间
        self.assertTrue(all(d.reason == "curve" for d in out))
        self.assertEqual(run(c, [40] * 10, t0=40 + n)[-1].reason, "ec-idle")  # 满 60 s 后交出

    def test_guard_overrides_ec_idle(self):
        d = run(Controller(CURVE, ec_below=55), [{"cpu": 40, "nvme0": 82}] * 3)[-1]
        self.assertEqual((d.target, d.reason), (6000, "guard:nvme0"))


class SenPlant:
    """SEN1 热模型（极客满载闭环数据拟合）：死区 θ 秒、时间常数 τ 的一阶响应 + 缓慢热积累，读数取整。"""

    def __init__(self, base=30.3, gain=0.739, tau=5.0, dead=2, drift=1.89):
        self.base, self.gain, self.a, self.dead, self.drift = base, gain, 1 - math.exp(-1 / tau), dead, drift
        self.x, self.hist, self.t = 14.0, [14.0] * (dead + 1), 0

    def step(self, watts):
        self.hist.append(watts)
        self.x += self.a * (self.hist[-1 - self.dead] - self.x)
        self.t += 1
        return self.base + self.gain * self.x + self.drift * self.t / 60


def closed_loop(plant, seconds=300):
    """满载闭环：不限制时功耗 80 W（PL2）。返回逐秒 (SEN 读数, 功耗上限, 实际功耗)。"""
    g, sen, watts, rows = PowerGuard(), plant.step(14.0), 14.0, []
    for t in range(seconds):
        reading = math.floor(sen) + 0.05
        cap = g.step({"sen1": reading}, float(t), watts)
        watts = min(80.0, cap if cap is not None else 80.0)
        sen = plant.step(watts)
        rows.append((reading, cap, watts))
    return rows


class PowerGuardTest(unittest.TestCase):
    def feed(self, g, sens, t0=0, pkg_w=65.0):
        return [g.step({"cpu": 90, "sen1": s, "sen3": None}, t0 + i, pkg_w) for i, s in enumerate(sens)]

    def test_idle_below_target_no_cap(self):
        self.assertEqual(self.feed(PowerGuard(), [70] * 10)[-1], None)

    def test_engages_at_target_from_actual_power(self):
        caps = self.feed(PowerGuard(), [POWER_SEN_TARGET], pkg_w=60.0)
        self.assertEqual(caps, [60])                 # 起点 = 实际功耗，不从 70 W 慢慢往下试

    def test_fast_rise_engages_early(self):
        caps = self.feed(PowerGuard(), [62, 65, 68, 71, 74], pkg_w=68.0)
        self.assertEqual(caps[:4], [None] * 4)       # 低于 72°C 不做预测
        self.assertIsNotNone(caps[4])                # 74°C、每秒 +3°C：预计越过临界线
        self.assertLess(caps[4], 68)                 # 起点已扣除预计超出量

    def test_sensor_jump_does_not_slam_to_floor(self):
        caps = self.feed(PowerGuard(), [63, 67, 71, 79], pkg_w=68.0)   # 实测出现过一秒 +8°C
        self.assertGreaterEqual(caps[-1], 68 - 13)

    def test_slow_rise_does_not_engage_early(self):
        caps = self.feed(PowerGuard(), [74, 74.5, 75, 75.5, 76, 76.5, 77, 77.5])
        self.assertTrue(all(c is None for c in caps), caps)

    def test_full_load_holds_target_without_crossing_limit(self):
        rows = closed_loop(SenPlant())
        self.assertLess(max(r[0] for r in rows), POWER_SEN_LIMIT + 1)     # 读数为整数：最多读到 80
        tail = rows[-100:]
        self.assertTrue(all(abs(r[0] - POWER_SEN_TARGET) <= 1.1 for r in tail), tail)   # 读数 = 整数 + 0.05
        ups = sum(1 for a, b in zip(tail, tail[1:]) if b[2] > a[2])
        self.assertLessEqual(ups, 10)                # 热积累使功耗缓降，但不来回摆动

    def test_stable_with_model_error(self):
        for plant in (SenPlant(drift=0), SenPlant(base=21.0, gain=0.96, dead=3, drift=0),
                      SenPlant(base=48.0, gain=0.52, drift=0), SenPlant(base=34.0, tau=8.0, dead=3, drift=0)):
            rows = closed_loop(plant)
            self.assertLess(max(r[0] for r in rows), 86)          # 旧的阶梯式调节在同样条件下到 86°C
            tail = rows[-100:]
            self.assertTrue(all(abs(r[0] - POWER_SEN_TARGET) <= 1.1 for r in tail), tail)
            self.assertLessEqual(max(r[2] for r in tail) - min(r[2] for r in tail), 3.0)   # 不振荡

    def test_hard_limit_drops_fast(self):
        g = PowerGuard()
        caps = self.feed(g, [79, 86, 86, 86])
        self.assertTrue(all(a - b >= POWER_HARD_W_S for a, b in zip(caps[1:], caps[2:])), caps)

    def test_floor(self):
        self.assertEqual(self.feed(PowerGuard(), [95] * 60)[-1], POWER_MIN_W)

    def test_no_release_near_target(self):
        g = PowerGuard()
        self.feed(g, [80] * 3, pkg_w=60.0)
        caps = self.feed(g, [78] * 120, t0=3, pkg_w=60.0)       # 上限一直在起作用，温度略低于目标
        self.assertTrue(all(c is not None for c in caps), caps)  # 不撤销：撤销后功耗会冲到 80 W

    def test_releases_fully_when_cool(self):
        g = PowerGuard()
        self.feed(g, [85] * 4)
        rel = self.feed(g, [70] * 120, t0=4, pkg_w=15.0)       # 负载结束
        self.assertEqual(rel[-1], None)
        self.assertTrue(all(b is None or a is None or b >= a for a, b in zip(rel, rel[1:])))

    def test_no_windup_when_cap_not_binding(self):
        g = PowerGuard()
        self.feed(g, [80] * 3, pkg_w=65.0)
        cap = g.cap
        held = self.feed(g, [78] * 30, t0=3, pkg_w=cap - 10)   # EC 把功耗压得更低：上限没在起作用
        self.assertTrue(all(c <= round(cap) for c in held), held)

    def test_unknown_power_starts_from_default(self):
        cap = PowerGuard().step({"sen1": POWER_SEN_TARGET}, 0)
        self.assertIsNotNone(cap)
        self.assertLess(cap, POWER_START_W)

    def test_missing_sensor_keeps_state(self):
        g = PowerGuard()
        self.feed(g, [85] * 3)
        cap = g.cap
        self.assertEqual(g.step({"cpu": 90}, 10), round(cap))

    def test_output_always_safe(self):
        rnd = random.Random(1)
        g = PowerGuard()
        for t in range(2000):
            sen = rnd.choice([None, rnd.uniform(30, 100)])
            cap = g.step({"sen1": sen}, t * rnd.uniform(0.0, 1.5), rnd.choice([None, rnd.uniform(5, 90)]))
            self.assertTrue(cap is None or POWER_MIN_W <= cap < POWER_START_W, cap)


class SourceSwitchTest(unittest.TestCase):
    def feed(self, sw, seq, current, t0=0.0):
        """按 1 秒间隔喂入电源序列，返回每步结果。"""
        return [sw.step(src, t0 + i, current) for i, src in enumerate(seq)]

    def test_first_reading_only_records(self):
        sw = SourceSwitch()
        self.assertIsNone(sw.step("battery", 0, "geek"))
        self.assertEqual(sw.source, "battery")

    def test_switch_after_settle_with_memory(self):
        sw = SourceSwitch({"ac": "geek", "battery": "quiet"})
        out = self.feed(sw, ["ac"] + ["battery"] * (int(SOURCE_SETTLE) + 2), "geek")
        self.assertEqual([o for o in out if o], ["quiet"])
        self.assertEqual(sw.source, "battery")

    def test_bounce_does_not_switch(self):
        sw = SourceSwitch({"battery": "quiet"})
        out = self.feed(sw, ["ac", "battery", "ac", "battery", "ac", "ac"], "geek")
        self.assertEqual([o for o in out if o], [])
        self.assertEqual(sw.source, "ac")

    def test_battery_default(self):
        self.assertEqual(SourceSwitch().preferred("battery", "geek"), "balanced")
        self.assertEqual(SourceSwitch().preferred("battery", "quiet"), "quiet")
        self.assertEqual(SourceSwitch().preferred("ac", "quiet"), "quiet")

    def test_unknown_source_ignored(self):
        sw = SourceSwitch({"battery": "quiet"})
        self.assertEqual([o for o in self.feed(sw, [None] * 6, "geek") if o], [])
        sw.remember(None, "geek")
        self.assertEqual(sw.remembered, {"battery": "quiet"})


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
