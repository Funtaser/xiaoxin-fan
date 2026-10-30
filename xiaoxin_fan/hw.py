# SPDX-License-Identifier: GPL-3.0-or-later
"""硬件访问层：本程序中唯一调用 acpi_call 的地方，只开放白名单内的操作。

已在 XiaoXinPro 16 IMH9 (83D4, BIOS MECN69WW) 上验证：
  风扇转速  读 \\_SB.WM10.WMAE(0, 0x11, ID)          → rpm
           写 \\_SB.WM10.WMAE(0, 0x12, buf[ID,rpm])  → 0 表示 EC 确认
           rpm=0 交还 EC 自动；rpm=1 会让风扇停转（禁止）
  EC 模式  DYTC 隐藏功能（STP/GKM 等），platform_profile 由 ideapad_laptop 提供
"""

import glob
import os
import struct
import time

ACPI_CALL = "/proc/acpi/call"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
RAPL = "/sys/class/powercap/intel-rapl:0"          # MSR 域；EC 只改写 MMIO 域，不碰这里
RAPL_ENERGY = f"{RAPL}/energy_uj"
POWER_STATE = "/var/lib/xiaoxin-fan/power-original.json"   # 下调前的原始 PL1/PL2，崩溃后据此恢复

WMAE = r"\_SB.WM10.WMAE"
DYTC = r"\_SB.PC00.LPCB.EC0.VPC0.DYTC"
FCMO = r"\_SB.PC00.LPCB.EC0.FCMO"

FAN_IDS = (0x04030001, 0x04030002)
MIN_MANUAL_RPM = 2000
MAX_RPM = 6000

# DYTC SET 功能号（见 DSDT 中 Method (DYTC)）
DYTC_FUNCS = {"stp": 4, "apm": 5, "aqm": 6, "iep": 7, "ibs": 8}

PROFILES = ("low-power", "balanced", "performance")

# 已验证的机型 → 已验证的 BIOS 版本。其它机型上同名 ACPI 方法可能含义完全不同，默认拒绝运行。
SUPPORTED_MODELS = {
    ("LENOVO", "83D4"): {"name": "XiaoXin Pro 16 IMH9 (2024)", "bios": {"MECN69WW"}},
}
DMI_DIR = "/sys/class/dmi/id"


class HardwareError(Exception):
    pass


def validate_rpm(rpm):
    """0（交还 EC）或 [2000, 6000] 内取整到 100；其它值一律拒绝（1 会让风扇停转）。"""
    rpm = int(rpm)
    if rpm == 0:
        return 0
    if not MIN_MANUAL_RPM <= rpm <= MAX_RPM:
        raise ValueError(f"转速 {rpm} 超出允许范围 {MIN_MANUAL_RPM}–{MAX_RPM}")
    return round(rpm / 100) * 100


def _acpi(expr):
    try:
        with open(ACPI_CALL, "w") as f:
            f.write(expr)
        with open(ACPI_CALL) as f:
            out = f.read().strip("\0").strip()
    except OSError as e:
        raise HardwareError(f"acpi_call 不可用: {e}") from e
    if not out.startswith("0x"):
        raise HardwareError(f"{expr} -> {out}")
    return int(out, 16)


def read_dmi(dmi_dir=DMI_DIR):
    def get(key):
        try:
            with open(f"{dmi_dir}/{key}") as f:
                return f.read().strip()
        except OSError:
            return ""
    return {"vendor": get("sys_vendor"), "product": get("product_name"),
            "family": get("product_family"), "bios": get("bios_version")}


def check_model(dmi, allow_untested=False):
    """返回 (是否允许运行, 说明)。"""
    entry = SUPPORTED_MODELS.get((dmi["vendor"], dmi["product"]))
    desc = f"{dmi['vendor']} {dmi['product']} ({dmi['family']}), BIOS {dmi['bios']}"
    if entry is None:
        if allow_untested:
            return True, f"未验证的机型 {desc}：已按配置 allow_untested_model 强制运行，风险自负"
        return False, (f"未验证的机型 {desc}。本程序直接调用 BIOS/EC 方法，在其它机型上可能产生未知后果。"
                       f"如确认同型号硬件，请在 /etc/xiaoxin-fan/config.json 中设置 "
                       f'{{"allow_untested_model": true}}，并欢迎提交兼容性报告。')
    if dmi["bios"] not in entry["bios"]:
        return True, f"{entry['name']}：BIOS {dmi['bios']} 未经验证（已验证: {', '.join(sorted(entry['bios']))}）"
    return True, f"{entry['name']}，BIOS {dmi['bios']}（已验证）"


class Hardware:
    """真实硬件。需要 root 和 acpi_call 模块。"""

    def __init__(self, allow_untested=False):
        ok, self.model_note = check_model(read_dmi(), allow_untested)
        if not ok:
            raise HardwareError(self.model_note)
        if not os.path.exists(ACPI_CALL):
            raise HardwareError("未加载 acpi_call 模块（sudo modprobe acpi_call）")
        self.sensors = SensorSet.discover()
        self._energy = None

    # ---- 风扇 ----
    def read_rpm(self):
        return tuple(_acpi(f"{WMAE} 0 0x11 {fid:#x}") for fid in FAN_IDS)

    def set_rpm(self, rpm):
        """rpm=0 交还 EC；否则设为 [2000, 6000] 内、取整到 100 的目标转速。"""
        rpm = validate_rpm(rpm)
        ok = True
        for fid in FAN_IDS:
            buf = struct.pack("<II", fid, rpm).hex()
            ok &= _acpi(f"{WMAE} 0 0x12 b{buf}") == 0
        if not ok:
            raise HardwareError(f"EC 未确认风扇设定 {rpm}")

    def release(self):
        self.set_rpm(0)

    # ---- EC 模式 ----
    def get_profile(self):
        with open(PLATFORM_PROFILE) as f:
            return f.read().strip()

    def set_profile(self, profile):
        if profile not in PROFILES:
            raise ValueError(profile)
        with open(PLATFORM_PROFILE, "w") as f:
            f.write(profile)

    def set_extras(self, extras):
        """关闭全部 DYTC 附加功能，再开启 extras 中列出的（stp/apm/aqm/iep/ibs/gkm）。

        GKM 必须最先关：BIOS 关 GKM 时会无条件写 FCMO=1（性能档的功耗与风扇表）。
        之后每次 DYTC SET 都会按 platform_profile 和各功能标志重新计算 FCMO，从而回到正确的档位。
        """
        _acpi(f"{DYTC} {0x00B | 1 << 12 | 0xF << 16:#x}")             # GKM 关
        for name, func in DYTC_FUNCS.items():
            _acpi(f"{DYTC} {0x001 | func << 12 | 0xF << 16:#x}")
        for name in extras:
            if name == "gkm":
                _acpi(f"{DYTC} {0x00B | 1 << 12 | 0xF << 16 | 1 << 20:#x}")
            elif name in DYTC_FUNCS:
                _acpi(f"{DYTC} {0x001 | DYTC_FUNCS[name] << 12 | 0xF << 16 | 1 << 20:#x}")
            else:
                raise ValueError(f"未知 EC 附加模式: {name}")

    def fcmo(self):
        return _acpi(FCMO)

    # ---- 功耗上限（MSR PL1/PL2）----
    def set_power_cap(self, watts):
        """watts=None 恢复原始上限；否则把 PL1、PL2 都设为 watts。"""
        import json
        if watts is None:
            restore_power_limits()
            return
        if not os.path.exists(POWER_STATE):
            orig = [int(open(f"{RAPL}/constraint_{i}_power_limit_uw").read()) for i in (0, 1)]
            os.makedirs(os.path.dirname(POWER_STATE), exist_ok=True)
            with open(POWER_STATE, "w") as f:
                json.dump({"pl1_uw": orig[0], "pl2_uw": orig[1]}, f)
        uw = int(watts * 1_000_000)
        try:
            for i in (0, 1):
                with open(f"{RAPL}/constraint_{i}_power_limit_uw", "w") as f:
                    f.write(str(uw))
        except OSError as e:
            raise HardwareError(f"无法设置功耗上限（可能被 BIOS 锁定）: {e}") from e

    # ---- 读数 ----
    def power_source(self):
        return read_power_source()

    def battery_percent(self):
        return read_battery_percent()

    def read_temps(self):
        return self.sensors.read()

    def package_watts(self):
        """两次调用之间的平均封装功耗（W）；首次调用返回 None。"""
        try:
            with open(RAPL_ENERGY) as f:
                e = int(f.read())
        except OSError:
            return None
        now = time.monotonic()
        prev, self._energy = self._energy, (now, e)
        if prev is None or e < prev[1] or now <= prev[0]:
            return None
        return (e - prev[1]) / (now - prev[0]) / 1e6


class SensorSet:
    """cpu（x86_pkg_temp）、sen1/sen2（主板）、nvme*（硬盘）。读数无效时为 None。"""

    def __init__(self, paths):
        self.paths = paths

    @classmethod
    def discover(cls):
        paths = {}
        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            try:
                kind = open(f"{zone}/type").read().strip()
            except OSError:
                continue
            if kind == "x86_pkg_temp":
                paths["cpu"] = f"{zone}/temp"
            elif kind in ("SEN1", "SEN2", "SEN3", "SEN4"):
                paths[kind.lower()] = f"{zone}/temp"
        n = 0
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                if open(f"{hw}/name").read().strip() == "nvme":
                    paths[f"nvme{n}"] = f"{hw}/temp1_input"
                    n += 1
            except OSError:
                continue
        if "cpu" not in paths:
            raise HardwareError("找不到 CPU 温度传感器 x86_pkg_temp")
        return cls(paths)

    def read(self):
        out = {}
        for name, path in self.paths.items():
            try:
                t = int(open(path).read()) / 1000
            except (OSError, ValueError):
                t = None
            out[name] = t if t is not None and 5 <= t <= 150 else None   # SEN3/SEN4 恒为 0.05°C
        return out


CONFIG_FILE = "/etc/xiaoxin-fan/config.json"


def read_power_source(base="/sys/class/power_supply"):
    """"ac" / "battery"；没有 Mains 类电源时返回 None。"""
    found = False
    for d in glob.glob(f"{base}/*"):
        try:
            with open(f"{d}/type") as f:
                if f.read().strip() != "Mains":
                    continue
            found = True
            with open(f"{d}/online") as f:
                if f.read().strip() == "1":
                    return "ac"
        except OSError:
            continue
    return "battery" if found else None


def read_battery_percent(base="/sys/class/power_supply"):
    """电池电量（%），读不到时返回 None。"""
    for d in glob.glob(f"{base}/BAT*"):
        try:
            with open(f"{d}/capacity") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def load_config(path=CONFIG_FILE):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def restore_power_limits():
    """若曾下调过功耗上限，按保存的原始值恢复。返回是否做了恢复。"""
    import json
    try:
        with open(POWER_STATE) as f:
            orig = json.load(f)
    except (OSError, ValueError):
        return False
    for i, key in ((0, "pl1_uw"), (1, "pl2_uw")):
        with open(f"{RAPL}/constraint_{i}_power_limit_uw", "w") as f:
            f.write(str(int(orig[key])))
    os.remove(POWER_STATE)
    return True


def release_now():
    """systemd ExecStopPost 兜底：交还 EC、关闭附加模式、恢复功耗上限。"""
    try:
        if restore_power_limits():
            print("功耗上限已恢复")
    except Exception as e:  # noqa: BLE001 —— 兜底路径，尽力而为
        print(f"恢复功耗上限失败: {e}")
    try:
        hw = Hardware(allow_untested=load_config().get("allow_untested_model", False))
        hw.release()
        hw.set_extras([])
        print("风扇已交还 EC 自动控制")
    except Exception as e:  # noqa: BLE001
        print(f"交还 EC 失败: {e}")
