# SPDX-License-Identifier: GPL-3.0-or-later
"""风扇控制逻辑（纯计算，不访问硬件，便于测试）。

每秒调用一次 Controller.step(温度字典, 当前时刻)，返回本周期的目标转速：
  0      → 交还 EC 自动控制
  其它   → 手动目标 rpm（2000–6000，取整到 100）

处理顺序：温度平滑 → 曲线插值（降温方向带回差）→ 安全下限 → 交给 EC / 接管 → 升降速限速。
"""

from dataclasses import dataclass, field

from .hw import MAX_RPM, MIN_MANUAL_RPM

EMA_ALPHA = 0.35          # CPU 温度指数平滑系数（1 Hz 采样）
HYSTERESIS = 3.0          # 降温时需多降这么多度，转速才开始下降
DOWN_RATE = 100           # 每秒最多降低的 rpm（EC 自身约 33 rpm/s）
# 开应用、编译开头这类几秒的突发会让 x86_pkg_temp 一秒内跳 20–30°C；温度不高时慢慢升速，突发结束前
# 只多转几百 rpm，而不是一下冲上去再慢慢降。平滑温度到 UP_FREE 以上、或任何安全保护生效时不限。
UP_RATE = 200             # 曲线模式下每秒最多升高的 rpm
UP_FREE = 80.0
INVALID_GRACE = 5.0       # 传感器连续无效超过此秒数 → 交还 EC
CPU_HOT = 98.0            # CPU 持续高于此温度……
CPU_HOT_HOLD = 20.0       # ……超过此秒数，则风扇不低于 CPU_HOT_FLOOR
CPU_HOT_FLOOR = 4500
# 低温交给 EC：EC 接手后风扇可能停转、温度回升，我们接管后至少 2000 rpm、温度又回落——
# 回差太窄时会在两者之间来回跳。因此用慢速平滑温度判断，加宽回差，并要求接管后至少保持一段时间。
EC_IDLE_HYST = 5.0        # 低于 ec_below-5 才交出，回到 ec_below 再接管
EC_IDLE_ALPHA = 0.1       # 判断用的慢速平滑（约 10 s），单次尖峰几乎不影响
EC_IDLE_MIN_HOLD = 60.0   # 接管后至少保持这么多秒才可能交出
EC_IDLE_FAST = 10.0       # 快速平滑温度超过 ec_below+10（真实负载）时接管，不等慢速平滑
# 接管条件须持续 EC_IDLE_TAKE_HOLD 秒：几秒的突发不应让风扇从停转直接转满一分钟（原厂 EC 约 75°C 持续 20 s
# 才升速，交给 EC 期间就是原厂行为，推迟接管不会比原厂差）。原始温度过高或安全保护生效时立即接管。
EC_IDLE_TAKE_HOLD = 10.0
EC_IDLE_HARD = 90.0

# (传感器前缀, 温度阈值, 转速下限)
# SEN1 紧贴 CPU/供电，随功耗快速升温；原厂 EC 在 4 线程负载下让它到 73°C+ 仍只开 3900，
# BIOS 散热表：SEN 过热 75 / 临界 80（Linux 不轮询该温区，越过临界也不会关机）。风扇对 SEN1 影响有限
# （6000 比 3900 仅低约 2–4°C），真正压住 SEN1 靠下方的 PowerGuard。NVMe 警告 83.85。
GUARDS = (
    ("sen", 76.0, MAX_RPM),
    ("sen", 72.0, 4500),
    ("nvme", 80.0, MAX_RPM),
    ("nvme", 75.0, 5000),
)
# 功耗保护在工作时会把 SEN1 稳在 79°C；风扇保护线若仍在 72/76°C，满载时任何预设都会被推到 6000 rpm
# （实测性能预设曲线上限 4300，满载却是 6000）。因此功耗保护可用时，SEN 保护线移到其目标之上，
# 只在功耗已压到底仍压不住时介入；功耗保护不可用（写入失败、原厂预设）时仍用 GUARDS。
GUARDS_WITH_POWER = (
    ("sen", 83.0, MAX_RPM),
    ("sen", 81.0, 4500),
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
    power_guarded: bool = False    # 功耗保护可用时由守护进程置 True（SEN 保护线改用 GUARDS_WITH_POWER）
    _ec_idle: bool = False
    _idle_ema: float | None = None
    _taken_at: float | None = None
    _warm_since: float | None = None     # 交给 EC 期间，接管条件开始成立的时刻
    _ema: float | None = None
    _last: int = 0
    _last_time: float | None = None
    _invalid_since: float | None = None
    _hot_since: float | None = None
    _eval_temp: float | None = field(default=None, repr=False)

    def reset(self):
        self._ema = self._last_time = self._invalid_since = self._hot_since = self._eval_temp = None
        self._idle_ema = self._taken_at = self._warm_since = None
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

        rising = self._ema is None or cpu > self._ema
        self._ema = cpu if self._ema is None else self._ema + EMA_ALPHA * (cpu - self._ema)
        self._idle_ema = cpu if self._idle_ema is None else self._idle_ema + EC_IDLE_ALPHA * (cpu - self._idle_ema)

        # 曲线 + 回差：升温立即跟随；降温要多降 HYSTERESIS 度才跟随
        if self._eval_temp is None or self._ema >= self._eval_temp:
            self._eval_temp = self._ema
        elif self._ema <= self._eval_temp - HYSTERESIS:
            self._eval_temp = self._ema + HYSTERESIS
        target = self.curve.at(self._eval_temp)
        reason = "curve"

        # 安全下限
        for prefix, limit, floor in GUARDS_WITH_POWER if self.power_guarded else GUARDS:
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
            if self._ec_idle:
                # 慢速平滑在突发结束后还会高几秒，因此同时要求快速平滑仍未回落
                warm = self._ema >= self.ec_below and (
                    self._idle_ema >= self.ec_below or self._ema >= self.ec_below + EC_IDLE_FAST)
                if not warm:
                    self._warm_since = None
                elif self._warm_since is None:
                    self._warm_since = now
                if cpu >= EC_IDLE_HARD or (warm and now - self._warm_since >= EC_IDLE_TAKE_HOLD):
                    self._ec_idle, self._taken_at, self._warm_since = False, now, None
            elif self._idle_ema < self.ec_below - EC_IDLE_HYST and (
                    self._taken_at is None or now - self._taken_at >= EC_IDLE_MIN_HOLD):
                self._ec_idle = True
            if self._ec_idle:
                self._last, self._last_time = 0, now
                return Decision(0, "ec-idle", round(self._ema, 1))
        elif self._ec_idle:                      # 安全保护生效：立即接管
            self._ec_idle, self._taken_at, self._warm_since = False, now, None

        # 升降速限速（升速只在温度不高、无安全保护时限制）
        if self._last and self._last_time is not None:
            dt = min(max(now - self._last_time, 0.0), 2.0)       # 卡顿后恢复时不“攒额度”
            if target < self._last:
                target = max(target, self._last - DOWN_RATE * dt)
            elif reason == "curve" and self._ema < UP_FREE:
                # 突发已结束（平滑温度开始回落）就不再升；回差会让曲线值再停留一会儿
                target = min(target, self._last + UP_RATE * max(dt, 1.0) if rising else self._last)

        target = min(MAX_RPM, max(MIN_MANUAL_RPM, quantize(target)))
        self._last, self._last_time = target, now
        return Decision(target, reason, round(self._ema, 1))


# ---------------------------------------------------------------------------
# 功耗保护：SEN1 紧贴 CPU/供电，风扇压不住它（满载 6000 rpm 仍到 87°C，原厂 3900 rpm 时 91°C）。
# 仿照 Windows 上 Intel DTT 的做法，SEN 接近 BIOS 临界线时调节 CPU 功耗上限，把它稳在目标温度。
#
# 速度型 PI：cap -= KP·Δe + KI·e·dt（e = SEN − 目标）。由极客满载闭环数据拟合：SEN1 对功耗的响应为
# 死区 2 s、时间常数 5 s、0.74°C/W，另有约 +1.9°C/min 的缓慢热积累；不限制时短时功耗约 80 W。
# 旧的阶梯式调节在负载开始时冲到 85°C，之后在 73–80°C 间锯齿摆动。这里按升温速度预测，预计会越过
# 临界线时提前介入，起点直接取“当前功耗 − 预计超出量 / 增益”。参数在模型增益 +30%、死区 3 s 时仍不振荡。
POWER_SEN_LIMIT = 80.0     # BIOS 散热表中 SEN 的临界温度
POWER_SEN_TARGET = 79.0    # PI 目标
POWER_KP = 1.2             # W/°C
POWER_KI = 0.25            # W/(°C·s)
POWER_LEAD = 4.0           # 预测温度 = SEN + LEAD × 每秒升温（取最近 3 s）
POWER_PREDICT_FROM = 72.0  # 低于此温度不做预测介入
POWER_GAIN = 0.8           # °C/W，估算介入起点用
POWER_MAX_OVERSHOOT = 10.0 # 预测超出量上限：SEN1 读数偶有一秒跳 8°C，线性外推不能无限放大
POWER_TRACK_MARGIN = 3.0   # 上限比实际功耗高出此值以上 = 没在起作用：接近目标时不再放开（防积分饱和）
POWER_HARD = 85.0          # SEN ≥ 此温度时……
POWER_HARD_W_S = 5.0       # ……每秒至少降这么多
POWER_START_W = 70.0       # 上限回到此值即撤销限制（performance 档 EC 设定的 PL1）
POWER_RELEASE_BELOW = 3.0  # ……且 SEN 低于目标这么多度
POWER_MIN_W = 35.0


@dataclass
class PowerGuard:
    target: float = POWER_SEN_TARGET
    cap: float | None = None          # None = 不限制（保持原厂功耗上限）
    _last_time: float | None = None
    _e_prev: float | None = None
    _hist: list = field(default_factory=list)   # 最近 4 秒的 SEN，算升温速度

    def reset(self):
        self.cap = self._last_time = self._e_prev = None
        self._hist = []

    def step(self, temps, now, pkg_w=None):
        """pkg_w：上一周期的实际封装功耗（W），读不到为 None。返回本周期的功耗上限（整数瓦）或 None。"""
        sens = [t for n, t in temps.items() if n.startswith("sen") and t is not None]
        dt = 0.0 if self._last_time is None else min(max(now - self._last_time, 0.0), 2.0)
        self._last_time = now
        if not sens:                       # 读不到：保持现状
            return None if self.cap is None else round(self.cap)
        sen = max(sens)
        self._hist = (self._hist + [sen])[-4:]
        e = sen - self.target

        if self.cap is None:
            rate = max(0.0, (self._hist[-1] - self._hist[0]) / max(1, len(self._hist) - 1))
            pred = sen + POWER_LEAD * rate
            if sen < self.target and not (sen >= POWER_PREDICT_FROM and pred >= POWER_SEN_LIMIT):
                self._e_prev = e
                return None
            base = pkg_w if pkg_w is not None else POWER_START_W - 2     # 读不到功耗：起点也要低于原厂上限
            over = min(POWER_MAX_OVERSHOOT, max(0.0, pred - self.target))
            cap = min(POWER_START_W - 2, base - over / POWER_GAIN)
        else:
            de = 0.0 if self._e_prev is None else e - self._e_prev
            cap = self.cap - (POWER_KP * de + POWER_KI * e * dt)
            if cap > self.cap and e > -POWER_RELEASE_BELOW and pkg_w is not None and self.cap >= pkg_w + POWER_TRACK_MARGIN:
                cap = self.cap
            if sen >= POWER_HARD:
                cap = min(cap, self.cap - POWER_HARD_W_S * max(dt, 1.0))
        self._e_prev = e
        self.cap = max(POWER_MIN_W, cap)
        if self.cap >= POWER_START_W - 0.5:
            if e <= -POWER_RELEASE_BELOW:
                self.cap = None
                return None
            self.cap = POWER_START_W - 1     # 不受限时实际可到 80 W（PL2）：接近目标时不撤销，否则一放开就冲过头
        return round(self.cap)


# ---------- 插拔电源自动切换 ----------
SOURCE_SETTLE = 3.0       # 电源状态稳定这么多秒才算插拔（PD 协商、接口松动时会抖动）
# 从没给电池选过预设时：高功耗预设拔电后默认降到「均衡」，其它保持不变
BATTERY_DEFAULTS = {"performance": "balanced", "geek": "balanced"}


class SourceSwitch:
    """插电 / 电池各记住一个预设；电源稳定变化后返回应切换到的预设。

    source 取值 "ac" / "battery"，None 表示无法判断（不切换）。
    """

    def __init__(self, remembered=None, settle=SOURCE_SETTLE):
        self.remembered = {k: v for k, v in (remembered or {}).items() if k in ("ac", "battery") and v}
        self.settle = settle
        self.source = None         # 已确认的电源
        self._pending = None
        self._since = 0.0

    @property
    def settled(self):
        """电源状态稳定（没有正在确认的插拔）。"""
        return self.source is not None and self._pending is None

    def remember(self, source, preset):
        if source in ("ac", "battery"):
            self.remembered[source] = preset

    def preferred(self, source, current):
        """在 source 下应使用的预设（没有记录时按默认规则）。"""
        if source in self.remembered:
            return self.remembered[source]
        if source == "battery":
            return BATTERY_DEFAULTS.get(current, current)
        return current

    def step(self, source, now, current):
        """喂入当前电源；确认发生插拔时返回目标预设，否则返回 None。"""
        if source is None:
            self._pending = None
            return None
        if self.source is None:          # 首次：只记录，不切换
            self.source = source
            return None
        if source == self.source:
            self._pending = None
            return None
        if source != self._pending:
            self._pending, self._since = source, now
            return None
        if now - self._since < self.settle:
            return None
        self.source, self._pending = source, None
        return self.preferred(source, current)
