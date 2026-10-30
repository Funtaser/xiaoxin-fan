# SPDX-License-Identifier: GPL-3.0-or-later
"""风扇控制逻辑（纯计算，不访问硬件，便于测试）。

每秒调用一次 Controller.step(温度字典, 当前时刻)，返回本周期的目标转速：
  0      → 交还 EC 自动控制
  其它   → 手动目标 rpm（2000–6000，取整到 100）

处理顺序：温度平滑 → 曲线插值（降温方向带回差）→ 安全下限 → 降速限速。
"""

from dataclasses import dataclass, field

from .hw import MAX_RPM, MIN_MANUAL_RPM

EMA_ALPHA = 0.35          # CPU 温度指数平滑系数（1 Hz 采样）
HYSTERESIS = 3.0          # 降温时需多降这么多度，转速才开始下降
DOWN_RATE = 100           # 每秒最多降低的 rpm（EC 自身约 33 rpm/s）
INVALID_GRACE = 5.0       # 传感器连续无效超过此秒数 → 交还 EC
CPU_HOT = 98.0            # CPU 持续高于此温度……
CPU_HOT_HOLD = 20.0       # ……超过此秒数，则风扇不低于 CPU_HOT_FLOOR
CPU_HOT_FLOOR = 4500
EC_IDLE_HYST = 2.0        # 低温交给 EC 的回差：低于 ec_below-2 交出，回到 ec_below 再接管

# (传感器前缀, 温度阈值, 转速下限)
# SEN1 紧贴 CPU/供电，随功耗快速升温；原厂 EC 在 4 线程负载下让它到 73°C+ 仍只开 3900，
# BIOS 散热表：SEN 过热 75 / 临界 80（Linux 不轮询该温区，越过临界也不会关机）。风扇对 SEN1 影响有限
# （6000 比 3900 仅低约 2–4°C），所以风扇只在原厂工作区间之上介入，真正压住 SEN1 靠下方的 PowerGuard。NVMe 警告 83.85。
GUARDS = (
    ("sen", 76.0, MAX_RPM),
    ("sen", 72.0, 4500),
    ("nvme", 80.0, MAX_RPM),
    ("nvme", 75.0, 5000),
)


def quantize(rpm):
    return int(round(rpm / 100) * 100)


@dataclass
class Curve:
    points: list   # [(温度°C, rpm), ...]，温度递增

    def __post_init__(self):
        self.points = sorted((float(t), int(r)) for t, r in self.points)
        if not self.points:
            raise ValueError("风扇曲线至少需要一个点")
        for _, r in self.points:
            if not MIN_MANUAL_RPM <= r <= MAX_RPM:
                raise ValueError(f"曲线转速 {r} 超出 {MIN_MANUAL_RPM}–{MAX_RPM}")

    def at(self, temp):
        pts = self.points
        if temp <= pts[0][0]:
            return pts[0][1]
        for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
            if temp <= t1:
                return r0 + (r1 - r0) * (temp - t0) / (t1 - t0)
        return pts[-1][1]


@dataclass
class Decision:
    target: int                  # 0 = 交还 EC
    reason: str                  # curve / ec-idle / guard:<传感器> / sensor-fault / stock
    cpu_smoothed: float | None = None


@dataclass
class Controller:
    curve: Curve | None          # None = 原厂模式（全程交给 EC）
    ec_below: float | None = None  # 平滑 CPU 温度低于此值时交给 EC（EC 低温时可让风扇停转）
    _ec_idle: bool = False
    _ema: float | None = None
    _last: int = 0
    _last_time: float | None = None
    _invalid_since: float | None = None
    _hot_since: float | None = None
    _eval_temp: float | None = field(default=None, repr=False)

    def reset(self):
        self._ema = self._last_time = self._invalid_since = self._hot_since = self._eval_temp = None
        self._last = 0
        self._ec_idle = False

    def step(self, temps, now):
        if self.curve is None:
            self._last = 0
            return Decision(0, "stock")

        cpu = temps.get("cpu")
        if cpu is None:
            self._invalid_since = self._invalid_since if self._invalid_since is not None else now
            if now - self._invalid_since >= INVALID_GRACE or self._ema is None:
                self.reset()
                return Decision(0, "sensor-fault")
            cpu = self._ema               # 短暂无效：沿用上次平滑值
        else:
            self._invalid_since = None

        self._ema = cpu if self._ema is None else self._ema + EMA_ALPHA * (cpu - self._ema)

        # 曲线 + 回差：升温立即跟随；降温要多降 HYSTERESIS 度才跟随
        if self._eval_temp is None or self._ema >= self._eval_temp:
            self._eval_temp = self._ema
        elif self._ema <= self._eval_temp - HYSTERESIS:
            self._eval_temp = self._ema + HYSTERESIS
        target = self.curve.at(self._eval_temp)
        reason = "curve"

        # 安全下限
        for prefix, limit, floor in GUARDS:
            hot = [n for n, t in temps.items() if n.startswith(prefix) and t is not None and t >= limit]
            if hot and floor > target:
                target, reason = floor, f"guard:{hot[0]}"
        if cpu >= CPU_HOT:
            self._hot_since = self._hot_since if self._hot_since is not None else now
            if now - self._hot_since >= CPU_HOT_HOLD and CPU_HOT_FLOOR > target:
                target, reason = CPU_HOT_FLOOR, "guard:cpu"
        else:
            self._hot_since = None

        # 低温交给 EC；任何安全保护生效时都不交出
        if self.ec_below is not None and reason == "curve":
            if self._ema < self.ec_below - EC_IDLE_HYST:
                self._ec_idle = True
            elif self._ema >= self.ec_below:
                self._ec_idle = False
            if self._ec_idle:
                self._last, self._last_time = 0, now
                return Decision(0, "ec-idle", round(self._ema, 1))
        else:
            self._ec_idle = False

        # 降速限速（升速不限，安全优先）
        if self._last and self._last_time is not None and target < self._last:
            dt = min(max(now - self._last_time, 0.0), 2.0)       # 卡顿后恢复时不“攒额度”
            target = max(target, self._last - DOWN_RATE * dt)

        target = min(MAX_RPM, max(MIN_MANUAL_RPM, quantize(target)))
        self._last, self._last_time = target, now
        return Decision(target, reason, round(self._ema, 1))


# ---------------------------------------------------------------------------
# 功耗保护：SEN1 紧贴 CPU/供电，风扇压不住它（满载 6000 rpm 仍到 87°C，原厂 3900 rpm 时 91°C）。
# 仿照 Windows 上 Intel DTT 的做法，SEN 持续超过 BIOS 临界线时逐步降低 CPU 功耗上限。
POWER_SEN_LIMIT = 80.0     # BIOS 散热表中 SEN 的临界温度
POWER_RELEASE_HYST = 3.0   # 回落到 limit-3 以下才开始放开
POWER_START_W = 70.0       # 介入时的起点（performance 档 EC 设定的 PL1）
POWER_MIN_W = 35.0
POWER_DOWN_W_S = 2.0       # ≥ limit 时每秒下调
POWER_DOWN_FAST_W_S = 5.0  # ≥ limit+5 时每秒下调
POWER_UP_W_S = 0.5         # ≤ limit-3 时每秒放开


@dataclass
class PowerGuard:
    limit: float = POWER_SEN_LIMIT
    cap: float | None = None          # None = 不限制（保持原厂功耗上限）
    _last_time: float | None = None

    def reset(self):
        self.cap = self._last_time = None

    def step(self, temps, now):
        """返回本周期的功耗上限（整数瓦）或 None。"""
        sens = [t for n, t in temps.items() if n.startswith("sen") and t is not None]
        dt = 0.0 if self._last_time is None else min(max(now - self._last_time, 0.0), 2.0)
        self._last_time = now
        if not sens:                       # 读不到：保持现状
            return None if self.cap is None else round(self.cap)
        sen = max(sens)
        if sen >= self.limit:
            rate = POWER_DOWN_FAST_W_S if sen >= self.limit + 5 else POWER_DOWN_W_S
            self.cap = max(POWER_MIN_W, (self.cap if self.cap is not None else POWER_START_W) - rate * max(dt, 1.0))
        elif self.cap is not None and sen <= self.limit - POWER_RELEASE_HYST:
            self.cap += POWER_UP_W_S * dt
            if self.cap >= POWER_START_W:
                self.cap = None
        return None if self.cap is None else round(self.cap)
