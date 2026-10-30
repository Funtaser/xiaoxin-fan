---
name: 兼容性报告 / Compatibility report
about: 报告其它机型或 BIOS 版本的只读检查结果
labels: compatibility
---

> 请先阅读 docs/COMPATIBILITY.md。**在维护者确认前不要强制运行写入操作。**

### 机型信息

```
（cat /sys/class/dmi/id/{sys_vendor,product_name,product_family,bios_version} 的输出）
```

### BIOS 接口检查

- [ ] DSDT 中存在 `Method (WMAE`
- [ ] 存在 `0x04030001`，读取为 `MBEY (0xEF, 0x63, One)`
- [ ] 写入为 `MBEY (0xEF, 0x61, …)` / `MBEY (0xEF, 0x62, …)`
- [ ] 存在 `Method (DYTC`

```
（相关 DSDT 片段）
```

### 只读转速测试

```
（GetFeatureValue 0x04030001 / 0x04030002 的返回值，空闲与负载下各一次）
```

### 其它

发行版 / 内核 / GNOME 版本：
