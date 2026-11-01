# xiaoxin-fan

联想小新 Pro 16 IMH9（2024，型号 83D4）在 Linux 下的风扇控制：守护进程 + 命令行 + GNOME 快捷设置面板。

[English summary](#english-summary)

> [!WARNING]
> 本程序通过 `acpi_call` 直接调用 BIOS/EC 方法控制风扇。**只在 83D4 / BIOS MECN69WW 上验证过**，
> 其它机型默认拒绝运行。风扇控制失当可能导致过热降频；虽然程序有多重保护（见下文），**使用风险自负**，
> 作者不对任何硬件损坏负责（见 GPL-3.0 第 15、16 条）。

## 能做什么

- **读写双风扇转速**——Linux 内核驱动报"不支持"，但 BIOS 实际提供了接口（见 [逆向分析](docs/RESEARCH.md)）；两个风扇错开 200 rpm（与 EC 相同），避免同转速的拍频
- **6 个预设**：EC 功耗档位 + 自定义风扇曲线，含 Linux 驱动未开放的 STP（10–15W）与 GKM（Windows「极客模式」）
- **低温交给 EC**：CPU 低于阈值时由 EC 接管，风扇可完全停转，和原厂一样安静
- **突发不吵**：开应用、编译开头这类几秒的突发不会把风扇从停转吵起来（接管条件须持续 10 s，≥90°C 立即接管）；
  温度不高时每秒最多升 200 rpm，突发结束即停止升速
- **GNOME 快捷设置面板**：一键切换、实时显示温度/转速/功耗
- **接管系统电源模式**：与 power-profiles-daemon 双向同步（续航/静音→节能，均衡→平衡，性能/极客→性能）；
  守护进程在线时面板会隐藏 GNOME 自带的「性能模式」（Power Mode）开关并占用它的位置，避免两个入口重复
- **插拔电源自动切换**：插电 / 电池各记住上次选的预设（面板、fanctl 或 Fn+Q），拔插后自动切换（电池未设置过时，性能/极客降为均衡；低电量时系统自动省电不会被记住）
- **Fn+Q**：按 静音 → 均衡 → 性能 循环；Fn+Q 与插拔电源引起的切换会在屏幕中央弹出提示

## 预设

| 预设 | EC 档位（功耗上限） | 风扇 |
|---|---|---|
| 原厂 | 不变 | 完全交给 EC，与未安装时相同（**首次安装的默认值**） |
| 续航 | low-power + STP（10–15W） | <60°C 交给 EC，之后 2000→3500 |
| 静音 | low-power（20–35W） | <60°C 交给 EC，75°C 前 ≤2500 |
| 均衡 | balanced（50–70W） | <55°C 交给 EC，最高 4500 |
| 性能 | performance（70W） | <50°C 交给 EC，中低负载接近原厂、高负载平滑升速，最高 4300 |
| 极客 | performance + GKM | 全程接管，空闲 2300，最高 6000 |

原厂 EC 的逻辑是"约 75°C 时一步跳到档位上限（性能档 3900，极客 4300）"，硬件满速约 6100。
实测数据见 [MEASUREMENTS.md](docs/MEASUREMENTS.md)。

自定义：在 `/etc/xiaoxin-fan/presets.json` 中覆盖字段或新增预设，然后 `sudo systemctl restart xiaoxin-fand`：

```json
{ "quiet": { "curve": [[0, 2000], [70, 2200], [85, 3500]], "ec_below": 60 } }
```

## 安全机制

- 只接受 0（交还 EC）或 2000–6000 rpm；**BIOS 中写 1 会让风扇停转**，代码层面拒绝。
- 机型校验：非 83D4 拒绝运行；未验证的 BIOS 版本会在日志中警告。
- 风扇保护（参考 BIOS 散热表）：主板 SEN ≥72°C → ≥4500，≥76°C → 6000（功耗保护工作时改为 ≥81 / ≥83°C，
  SEN1 由功耗保护稳在 79°C，满载不必一律 6000）；NVMe ≥75°C → ≥5000，≥80°C → 6000；
  CPU 持续 20 s ≥98°C → ≥4500。
- **功耗保护**：主板传感器 SEN1 紧贴 CPU/供电，Linux 满载时原厂状态下会长期停在 **91°C**，超过 BIOS 临界线 80°C，
  且风扇压不住它、Linux 也不会因此降频或关机。本程序仿照 Windows 上 Intel DTT 的做法：SEN 接近 80°C 时用 PI 控制
  调节 CPU 功耗上限（MSR PL1/PL2，最低 35W），按升温速度提前介入，把 SEN1 稳在 79°C，回落后自动恢复。
  实测满载 SEN1 稳定在 79–80°C（峰值 81°C），频率仅比不限制低约 2.6%。
  「原厂」预设不介入。
- CPU 传感器连续 5 s 读不到、或控制周期出现任何异常 → 交还 EC。
- 退出 / 崩溃（systemd `ExecStopPost`）/ 休眠前（logind 延迟锁）→ 交还 EC 并恢复原始功耗上限；唤醒后重新应用预设。

## 安装

依赖：Ubuntu 24.04+ / 同类发行版、systemd、Python 3.10+、`python3-gi`、`acpi-call-dkms`；面板需要 GNOME Shell 50。

```bash
sudo apt install acpi-call-dkms python3-gi
git clone https://github.com/Funtaser/xiaoxin-fan.git && cd xiaoxin-fan
sudo bash install.sh            # 安装/更新守护进程与 fanctl（会先跑单元测试）
bash gnome-extension/install.sh # 安装 GNOME 面板（注销重新登录后生效）
```

卸载：`sudo bash uninstall.sh`（风扇交还 EC）。

## 使用

```bash
fanctl                 # 当前状态
fanctl list            # 预设与曲线
fanctl set 性能        # 切换（sudo 组用户无需 sudo）
fanctl watch           # 实时
fanctl set 原厂        # 一键回到出厂行为
```

## 项目结构

| 路径 | 内容 |
|---|---|
| `xiaoxin_fan/` | 守护进程、控制逻辑、硬件访问层（唯一调用 `acpi_call` 之处，白名单） |
| `gnome-extension/` | GNOME 快捷设置面板 |
| `tests/` | 控制逻辑、预设、机型校验单元测试 |
| `tools/` | 研究用探测脚本（读转速、手动设定测试、除尘记录、负载对比、双风扇听感对比等），Windows 采集脚本；`record-trace.py` 逐秒记录守护进程状态（无需 sudo，适合在真实编译时开着） |
| `docs/` | [逆向分析](docs/RESEARCH.md)、[实测数据](docs/MEASUREMENTS.md)、[其它机型兼容性检查](docs/COMPATIBILITY.md) |

## 开发

```bash
python3 -m unittest discover -s tests            # 单元测试
bin/xiaoxin-fand --fake & bin/fanctl --session   # 模拟硬件端到端测试（无需 root、不碰 EC）
```

## 参与

- **其它机型**：请先按 [COMPATIBILITY.md](docs/COMPATIBILITY.md) 做只读检查，再提交兼容性报告 issue。
- 问题和改进欢迎提 issue / PR。改动控制逻辑请附单元测试。

## 修订历史

实测数据见 [MEASUREMENTS.md](docs/MEASUREMENTS.md)。

### v0.4.0（2026-11-01）

- **功耗保护改为 PI 控制**：目标 SEN1 79°C，按升温速度提前介入。极客满载 SEN1 峰值 85→81°C，稳态从 74–80°C 的锯齿变为 79–80°C
- **短时突发不惊动风扇**：交给 EC 时接管条件须持续 10 s（≥90°C 或保护线触发时立即接管）；温度不高时每秒最多升 200 rpm，突发结束即停止升速
- **极客预设空闲转速**从约 3000 降到 2300 rpm
- **双风扇错开 200 rpm**（与 EC 相同），避免同转速的拍频
- **风扇保护线调整**：功耗保护可用时 SEN 保护线改为 ≥81 / ≥83°C（不可用时仍为 72 / 76°C），性能、均衡中段曲线贴近原厂
- ⚠️ **行为变化**：「性能」预设满载风扇从 6000 降到 4300 rpm，频率约低 5%（3198→3046 MHz）；要最快请用「极客」
- Fn+Q 切换会被记住，并弹出屏幕提示
- 修复开机不启动（服务改挂 `graphical.target`）
- 状态新增 MSR / MMIO PL1 读数；新增 `tools/record-trace.py`、`tools/fan-offset-listen.sh`

### v0.3.0（2026-10-30）

- 修复「静音」「均衡」实际运行在 70/70 W 的问题（BIOS 关闭 GKM 时的副作用）
- 插拔电源自动切换，插电 / 电池各记住一个预设
- 低温交给 EC 的判断加防抖，避免来回跳
- 「续航」预设改为 low-power + STP
- 面板：读数格加小曲线，接管系统「性能模式」开关

### v0.2.0（2026-10-30）

- 新增功耗保护：SEN1 超过 80°C 时下调 CPU 功耗上限，回落后恢复

### v0.1.0（2026-10-30）

- 首个版本：守护进程、`fanctl`、GNOME 面板、6 个预设

## 许可证

[GPL-3.0-or-later](LICENSE)

---

## English summary

Fan control for the **Lenovo XiaoXin Pro 16 IMH9 (2024, machine type 83D4)** on Linux: a root daemon, a CLI (`fanctl`)
and a GNOME Shell quick-settings toggle.

- The kernel's `lenovo-wmi-other` driver reports fan control as unsupported on this model, but the BIOS method
  `\_SB.WM10.WMAE` (`LENOVO_OTHER_METHOD`, feature IDs `0x04030001/2`) reads **and sets** both fans' RPM.
  Writing `0` hands control back to the EC; **writing `1` stops the fan** and is rejected by this program.
- Hidden DYTC modes (STP 10–15 W, GKM = Windows "Geek mode") are exposed as presets alongside custom fan curves.
- Safety: model/BIOS gate, RPM whitelist (0 or 2000–6000), sensor-based floors, hand-back to the EC on exit,
  crash and suspend.
- **Only verified on 83D4 / BIOS MECN69WW; other models are refused by default.** See
  [docs/RESEARCH.md](docs/RESEARCH.md) (in Chinese) for the reverse-engineering notes and
  [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for read-only checks on other machines.

License: GPL-3.0-or-later. No warranty — you run this at your own risk.
