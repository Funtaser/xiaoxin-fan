# SPDX-License-Identifier: GPL-3.0-or-later
"""预设 = EC 档位（决定功耗上限）+ EC 附加模式 + 风扇曲线
       + ec_below：平滑 CPU 温度低于该值时交给 EC（EC 在低温时可让风扇停转，比任何曲线都安静）。

EC 档位实测（2 线程负载）：
  STP           PL 10/15W  风扇上限 2500     low-power  PL 20/35W  上限 2500
  balanced      PL 50/70W  上限 3400         performance PL 70/70W 上限 3900
  performance+GKM          上限 4300         硬件满速约 6100
用户可在 /etc/xiaoxin-fan/presets.json 中覆盖或新增预设（同名字段覆盖）。
"""

import json
import os

from .control import Curve
from .hw import DYTC_FUNCS, PROFILES

USER_PRESETS = "/etc/xiaoxin-fan/presets.json"

DEFAULT_PRESETS = {
    "stock": {
        "label": "原厂", "description": "完全交给 EC，与未安装本程序时相同",
        "profile": None, "extras": [], "curve": None,
    },
    "battery": {
        "ec_below": 60,
        "label": "续航", "description": "功耗 10–15W，最安静，适合用电池看视频/文档",
        "profile": "low-power", "extras": ["stp"],     # STP 在 BIOS 中最后计算，叠加在节能档上同样生效
        "curve": [[0, 2000], [65, 2000], [75, 2600], [85, 3500]],
    },
    "quiet": {
        "ec_below": 60,
        "label": "静音", "description": "功耗 20–35W，75°C 前保持低转速",
        "profile": "low-power", "extras": [],
        "curve": [[0, 2000], [65, 2000], [75, 2500], [85, 3500], [92, 4500]],
    },
    "balanced": {
        "ec_below": 55,
        "label": "均衡", "description": "功耗 50–70W，接近原厂但升降更平滑",
        "profile": "balanced", "extras": [],
        "curve": [[0, 2200], [62, 2300], [72, 2800], [82, 3600], [92, 4500]],
    },
    "performance": {
        "ec_below": 50,
        "label": "性能", "description": "功耗 70W，中低负载接近原厂、高负载平滑升速，最高 4300",
        "profile": "performance", "extras": [],
        # 中等负载受功耗限制，风扇更快只降温不提频：65–75°C 段贴近原厂（EC 约 75°C 前保持低速）
        "curve": [[0, 2300], [60, 2500], [72, 3000], [80, 3900], [88, 4300]],
    },
    "geek": {
        "label": "极客", "description": "性能 + GKM，最高 6000，适合长时间重负载",
        "profile": "performance", "extras": ["gkm"],
        "curve": [[0, 2300], [54, 2300], [65, 4000], [75, 5000], [85, 6000]],   # 空闲 47–52°C：2300（原 3000）
    },
}

# GNOME 右上角电源模式 → 预设（用户在系统菜单里切换时跟随）
PROFILE_TO_PRESET = {"low-power": "quiet", "balanced": "balanced", "performance": "performance"}


def validate(name, p):
    if p.get("profile") is not None and p["profile"] not in PROFILES:
        raise ValueError(f"{name}: 未知 profile {p['profile']}")
    for e in p.get("extras", []):
        if e != "gkm" and e not in DYTC_FUNCS:
            raise ValueError(f"{name}: 未知 extras {e}")
    if p.get("curve") is not None:
        Curve(p["curve"])
    if p.get("ec_below") is not None and not 30 <= float(p["ec_below"]) <= 80:
        raise ValueError(f"{name}: ec_below 须在 30–80°C 之间")
    return p


def load_presets(path=USER_PRESETS):
    presets = {k: dict(v) for k, v in DEFAULT_PRESETS.items()}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for name, override in json.load(f).items():
                presets[name] = {**presets.get(name, {"extras": []}), **override}
    for name, p in presets.items():
        p.setdefault("label", name)
        p.setdefault("description", "")
        p.setdefault("extras", [])
        p.setdefault("ec_below", None)
        validate(name, p)
    return presets


def resolve(presets, name):
    """接受 id（geek）或中文名（极客）。"""
    if name in presets:
        return name
    for k, p in presets.items():
        if p.get("label") == name:
            return k
    raise KeyError(name)
