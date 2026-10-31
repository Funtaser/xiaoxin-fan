# 逆向分析记录

机型：联想小新 Pro 16 IMH9（2024，DMI `LENOVO 83D4`），BIOS `MECN69WW`，Intel Core Ultra 5 125H，双风扇。
系统：Ubuntu 26.04，内核 7.2（Liquorix）/ 7.0（generic）。

> 以下 ACPI 路径、功能号和行为只在上述机型/BIOS 上验证过。其它机型即使方法同名，含义也可能不同。

## 结论速览

| 途径 | 结果 |
|---|---|
| Intel DTT 风扇 `TFN1`（`INTC1063`，thermal `cooling_device`） | ❌ 空壳 |
| `ideapad_laptop` 的 `fan_mode` | ❌ 非 0 值只会触发除尘 |
| `lenovo_wmi_gamezone` / `LENOVO_GAMEZONE_DATA` | ❌ 风扇相关方法均未实现 |
| `LENOVO_FAN_METHOD`（`Fan_Get_Table`/`Fan_Set_Table`） | ❌ BIOS 未提供该 GUID |
| **`LENOVO_OTHER_METHOD`（`\_SB.WM10.WMAE`）功能 `0x0403000x`** | ✅ **可读写双风扇转速** |
| **DYTC 隐藏模式（STP / GKM 等）** | ✅ 可切换 EC 功耗/风扇档位 |

## 走不通的路

### Intel DTT 风扇 TFN1

`/sys/class/thermal/cooling_device*` 中类型为 `TFN1` 的设备（`max_state=50`，支持 fine-grain control），
`_FPS` 声明了 0–100% 共 12 档。但 SSDT 中：

```
Method (_FSL, 1) { If (CondRefOf (\_SB.PC00.LPCB.H_EC.DPTF.FNSL)) { ... } }
Method (_FST, 0) { If (CondRefOf (\_SB.PC00.LPCB.H_EC.DPTF.GFNS)) { ... } }
```

本机的 EC 设备是 `EC0` 而不是 Intel 参考设计的 `H_EC`，这两个方法都不存在：写入被静默忽略，读回转速为 `0xFFFFFFFF`。
把 INT3400 的策略切到 Active（`3A95C389-…`）也无效。Windows 上 DTT 驱动同样无法通过它控制风扇。

### ideapad_laptop fan_mode

`/sys/bus/platform/devices/VPC2004:00/fan_mode` 经 `VPCW` 直接透传给 EC（命令 `0x2B`）。驱动文档的
0/1/2/4（超静音/标准/除尘/高效散热）在本机不成立：

| 写入 | 读回 | 行为 |
|---|---|---|
| 0 | 133（`0x85`） | 正常 EC 自动 |
| 1 / 2 / 4 | 3 | **除尘**：刹车至 ~500 rpm → 1.5 s 内冲到 ~6100 rpm → 保持，循环；EC 不会自动退出 |

### GameZone WMI

`LENOVO_GAMEZONE_DATA`（`887B54E3-…`，`ACPI\PNP0C14\GMZN_0`）在 Windows 上可见完整 MOF（`GetFanCount`、
`GetSmartFanMode`、`GetCPUTemp` 等），但实际调用几乎全部返回“无效的对象”，仅 `IsSupportGpuOC`、
`IsSupportIGPUMode`、`GetIGPUModeStatus` 有效。`LENOVO_FAN_METHOD`（`92549549-…`）在 BIOS `_WDG` 中不存在。
Linux 的 `lenovo_wmi_gamezone` 报 `platform_profile probe failed`，`lenovo_wmi_other` 报
`fan reporting/tuning is unsupported on this device`。

## 走通的路 1：风扇转速（LENOVO_OTHER_METHOD）

`\_SB.WM10`（`_HID PNP0C14`，`_UID "GMZN"`）的 `_WDG` 中，GUID `DC2A8805-3A8C-41BA-A6F7-092E0089CD3B`
（`LENOVO_OTHER_METHOD`）对应对象 ID `AE` → ACPI 方法 **`\_SB.WM10.WMAE(instance, method, arg)`**：

| method | WMI 名称 | arg |
|---|---|---|
| `0x11` | `GetFeatureValue(IDs)` | 整数功能 ID |
| `0x12` | `SetFeatureValue(IDs, value)` | 8 字节缓冲区：DWORD 功能 ID + DWORD 值（小端） |

风扇功能 ID：`0x04030001`（风扇 1）、`0x04030002`（风扇 2）。`LENOVO_CAPABILITY_DATA_00` 中两者 capability = 7（可读可写）。

DSDT 实现（经 EC 邮箱 `MBEY`）：

| 操作 | EC 命令 | 含义 |
|---|---|---|
| Get | `MBEY(0xEF, 0x63, fan)` × 100 | 当前转速 rpm |
| Set 值 = 0 | `MBEY(0xEF, 0x63, 3)` | **交还 EC 自动控制** |
| Set 值 = 1 | `MBEY(0xEF, 0x61/0x62, 0)` | ⚠️ **目标 0 rpm = 停转** |
| Set 值 = N | `MBEY(0xEF, 0x61/0x62, N/100)` | 目标 N rpm |

Set 返回 0 表示 EC 应答 `0xAC`（成功）。用 `acpi_call`：

```bash
echo '\_SB.WM10.WMAE 0 0x11 0x04030001' > /proc/acpi/call; cat /proc/acpi/call      # 读风扇 1
echo '\_SB.WM10.WMAE 0 0x12 b0100030488130000' > /proc/acpi/call                    # 风扇 1 → 5000 rpm
echo '\_SB.WM10.WMAE 0 0x12 b0100030400000000' > /proc/acpi/call                    # 风扇 1 交还 EC
```

实测行为：

- 手动设定**向上、向下都生效**（2 线程负载下 EC 本想 3900，设 3000 即稳定在 3000）——接管后 EC 不再兜底。
- 切换 `platform_profile` 不会覆盖手动设定。
- 交还 EC 后转速按 EC 节奏缓降（约 100 rpm / 3 s）。
- 硬件满速约 6100 rpm；EC 低温（约 46°C 以下）会让风扇停转。

## 走通的路 2：DYTC 隐藏模式

`\_SB.PC00.LPCB.EC0.VPC0.DYTC(cmd)`，与 `ideapad_laptop`/联想电脑管家相同的入口。

- GET：`cmd = 0x02`，返回 `CICF<<8 | CICM<<12 | 标志位<<16 | 1`
- SET：`cmd = 0x001 | func<<12 | mode<<16 | valid<<20`（非 MMC 功能的 mode 须为 `0xF`）
- GKM：单独命令 `0x00B | 1<<12 | 0xF<<16 | valid<<20`

| 功能 | func | 写入 EC `FCMO` | MMIO PL1/PL2 | 风扇上限（2 线程负载） |
|---|---|---|---|---|
| 标准（balanced） | — | 0 | 50 / 70 W | 3400 |
| MMC 性能（performance） | 0xB, mode 2 | 1 | 70 / 70 W | 3900 |
| MMC 低功耗（low-power） | 0xB, mode 3 | 2 | 20 / 35 W | 2500 |
| APM | 5 | 3 | 70 / 70 W | 3600 |
| AQM | 6 | 4 | 20 / 35 W | 2500 |
| STP | 4 | 5 | **10 / 15 W** | 2500 |
| IEP | 7 | 6 | 70 / 70 W | 3800 |
| IBS | 8 | 7 | 20 / 35 W | 2500 |
| **GKM**（Windows「极客模式」） | 命令 0x0B | **8** | 70 / 70 W | **4300** |

`FCMO` 可经 `acpi_call` 读取 `\_SB.PC00.LPCB.EC0.FCMO`。BIOS 中计算顺序为 AQM→APM→IBS→IEP→MMC→STP，后者覆盖前者
（实测 low-power + STP 同样得到 FCMO=5、PL 10/15 W，因此「续航」预设用 low-power，系统菜单显示为“节能”）；
切换 MMC 会重算 FCMO（GKM 被覆盖），所以叠加模式前应先设好 `platform_profile`。

**关闭 GKM 的副作用**：命令 0x0B 在 valid=0 时不重算，而是无条件写 `ODV8=1, FCMO=1`，即性能档的功耗与风扇表。
如果关 GKM 是最后一步，low-power / balanced 会停在 70/70 W（v0.2.0 的「静音」「均衡」预设即有此问题）。
正确顺序：先关 GKM，再发其它 DYTC SET（任意一次都会按 MMC 与各功能标志重算 FCMO）。

**AQM / APM / IEP / IBS 的含义**：BIOS 中只有变量名（`VAQM` 等），没有说明。按缩写与实测推测分别对应
联想「智能模式」自动切换时的子模式（自动安静 / 自动性能 / 智能高性能 / 智能省电），此为推测，未经官方确认。
除 FCMO 外，它们各自置位一个输出标志（`ODVH` / `ODVG` / `ODVJ` / `ODVK`），供 Windows 上的 Intel DTT 选择散热策略；
Linux 不运行 DTT，因此**在 Linux 下实际生效的只有 EC 的功耗上限与风扇上限**。满载对比（MEASUREMENTS 第 8 节）：
AQM、IBS 与 low-power 完全相同；IEP 与 performance 相同（满载时风扇同样到 3900）；APM 即风扇上限降到 3600 的
performance，更热，EC 会把 PL1 多压低约 5 W。因此本程序只采用了有独特效果的 STP 与 GKM。

**EC 会动态下调 PL1**：轻负载时读到的 performance 档为 70/70 W，满载变热后 EC 会自行把 MMIO PL1 降到 60 W
（风扇上限较低的 APM 降到 55 W）。

**Fn+Q**：在 Linux 下按 low-power → balanced → performance 循环，`platform_profile` 同步变化，守护进程随之切换预设。

## EC 自动风扇逻辑（实测）

- **阶梯 + 封顶**：CPU 约 75°C 时 20 s 内拉到当前档位上限，之后不再升高。
- 只有上限随档位变化（见上表）；EC 从未使用 4300 以上转速（除尘时除外）。
- 满载时 CPU 无论哪个档位都会顶到 100°C 温度墙。

## 温度传感器（BIOS 散热表）

| 区域 | 说明 | 阈值 |
|---|---|---|
| `x86_pkg_temp` | CPU | 温度墙 100°C |
| `SEN1` | 紧贴 CPU/供电，随功耗快速上升（4 线程 20 s 内 38→71°C） | 主动 40/50/60，被动 65，过热 75，**临界 80** |
| `SEN2` | 机身 | 同上 |
| `SEN3` / `SEN4` | 恒为 0.05°C，无效 | — |
| NVMe ×2 | — | 警告 83.85，临界 87.85 |

风扇对 SEN1 影响有限：同一负载下 6000 rpm 仅比 3900 rpm 低约 2–4°C；原厂 EC 允许 SEN1 到 73°C+ 仍只开 3900。

**满载时 SEN1 越过临界线**：插电 18 线程满载、原厂 EC 控制下，功耗 ~70W 时 SEN1 长期停在 **91°C**（风扇 6000 rpm
时 87°C）。这些温区由 `int3403_thermal` 注册，策略为 `user_space` 且内核不轮询，越过 80°C 临界点也不会触发关机；
Linux 上也没有组件会因此降频。Windows 上 Intel DTT 会依据这些传感器下调功耗——这很可能是 Windows 极客模式只跑
~64.5W、而 Linux 跑满 70W 的原因。

本程序的功耗保护（`PowerGuard`）据此下调 **MSR** 功耗上限（`/sys/class/powercap/intel-rapl:0`，原始值 PL1 200W /
PL2 115W；EC 切换档位时只改写 MMIO 域 `intel-rapl-mmio:0`，不会覆盖 MSR，实际生效取两者较小值）。BIOS 未锁定该寄存器。

## Windows 侧

联想电脑管家 5.1 通过 **Lenovo Notebook ITS Driver**（`ACPI\IDEA2008`，服务 `LITSSVC`）切换模式，同时装有 Intel IPF/DTT
（`dptftcs`、`ipfsvc`）。WMI 枚举与只读调用的完整采集脚本见 `tools/windows/`。
