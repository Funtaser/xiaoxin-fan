# 其它机型兼容性检查

本程序直接调用 BIOS/EC 方法。**在未验证的机型上，不要运行任何写入操作**（设定转速、切换 DYTC、除尘），
同名方法在别的 BIOS 里可能改写完全不同的 EC 寄存器。下面的检查**全部只读**。

## 1. 机型信息

```bash
cat /sys/class/dmi/id/{sys_vendor,product_name,product_family,bios_version}
```

## 2. BIOS 是否提供同样的接口

```bash
sudo apt install acpica-tools
mkdir acpi && cd acpi && sudo acpidump -b && iasl -d dsdt.dat >/dev/null
grep -n 'Method (WMAE' dsdt.dsl                       # 风扇读写方法
grep -n '0x04030001' dsdt.dsl                         # 风扇 1 功能 ID
grep -n 'Method (DYTC' dsdt.dsl                       # 模式切换
grep -n -A3 'Name (_UID, "GMZN")' dsdt.dsl            # WMI 设备
```

把 `If ((ToInteger (Arg2) == 0x04030001))` 附近的代码与 [RESEARCH.md](RESEARCH.md) 中的描述对比：
读取应为 `MBEY (0xEF, 0x63, One)`，写入应为 `MBEY (0xEF, 0x61, …)`。**完全一致**才有继续的意义。

## 3. 只读测试转速

确认第 2 步一致后：

```bash
sudo apt install acpi-call-dkms && sudo modprobe acpi_call
echo '\_SB.WM10.WMAE 0 0x11 0x04030001' | sudo tee /proc/acpi/call >/dev/null; sudo cat /proc/acpi/call
```

返回值 ×1 即 rpm（例如 `0x8fc` = 2300）。数值随负载变化即说明读取有效。

## 4. 提交兼容性报告

请开一个 issue（选择「兼容性报告」模板），附上第 1–3 步的输出。
在维护者确认前，请勿用 `allow_untested_model` / `XIAOXIN_ALLOW_UNTESTED` 强制运行。
