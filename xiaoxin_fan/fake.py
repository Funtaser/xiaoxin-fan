# SPDX-License-Identifier: GPL-3.0-or-later
"""模拟硬件：无需 root、不碰真实 EC，用于在会话总线上做端到端测试。"""

import math
import time

from .hw import PROFILES, fan_targets

EC_CAPS = {"low-power": 2500, "balanced": 3400, "performance": 3900}


class FakeHardware:
    def __init__(self):
        self.profile = "performance"
        self.extras = []
        self.target = (0, 0)
        self.rpm = [2300.0, 2200.0]
        self._t0 = self._last = time.monotonic()
        self.power_cap = None
        self.source = "ac"

    def _cpu(self):
        # 60 秒周期的负载起伏；风扇越快温度越低
        load = 0.5 + 0.5 * math.sin((time.monotonic() - self._t0) / 60 * 2 * math.pi)
        return 45 + 50 * load - (self.rpm[0] - 2300) / 300

    def _advance(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        cap = 4300 if "gkm" in self.extras else EC_CAPS[self.profile]
        for i in range(2):
            goal = self.target[i] or (cap if self._cpu() > 75 else 2300) - 200 * i
            step = 500 * dt if goal > self.rpm[i] else 33 * dt
            self.rpm[i] += max(-step, min(step, goal - self.rpm[i]))

    def read_rpm(self):
        self._advance()
        return tuple(int(round(r, -2)) for r in self.rpm)

    def set_rpm(self, rpm):
        self.target = fan_targets(rpm)

    def release(self):
        self.set_rpm(0)

    def get_profile(self):
        return self.profile

    def set_profile(self, profile):
        if profile not in PROFILES:
            raise ValueError(profile)
        self.profile = profile

    def set_extras(self, extras):
        self.extras = list(extras)

    def fcmo(self):
        if "gkm" in self.extras:
            return 8
        return {"low-power": 2, "balanced": 0, "performance": 1}[self.profile]

    def read_temps(self):
        return {"cpu": round(self._cpu(), 1), "sen1": 41.0, "sen2": 42.0, "sen3": None, "nvme0": 33.0}

    def power_source(self):
        return self.source

    def battery_percent(self):
        return 80

    def set_power_cap(self, watts):
        self.power_cap = watts

    def power_limits(self):
        return {"msr": self.power_cap or 200.0, "mmio": 70.0}

    def package_watts(self):
        w = 10 + (self._cpu() - 45) * 1.1
        return round(min(w, self.power_cap) if self.power_cap else w, 1)
