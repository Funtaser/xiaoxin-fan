# SPDX-License-Identifier: GPL-3.0-or-later
<#
  fan-probe.ps1  —  联想小新风扇/散热行为采集（只读）
  在 Windows 上以管理员身份运行：
    powershell -ExecutionPolicy Bypass -File .\fan-probe.ps1 -Tag 野兽
  参数：
    -Tag      本次测试的标签（建议填当前联想模式：安静 / 均衡 / 野兽）
    -IdleSec  空闲采样秒数（默认 60）
    -LoadSec  满载秒数（默认 180）
    -CoolSec  冷却秒数（默认 90）
  结果保存在脚本所在目录的 result-<时间>-<Tag> 文件夹中。
  本脚本不修改任何设置，只调用名称以 Get / Is 开头且无输入参数的 WMI 方法。
#>
param(
  [string]$Tag = "未标注",
  [int]$IdleSec = 60,
  [int]$LoadSec = 180,
  [int]$CoolSec = 90,
  [int]$Interval = 2
)

$ErrorActionPreference = 'Continue'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "请右键 PowerShell，选择【以管理员身份运行】后再执行本脚本。" -ForegroundColor Red
  exit 1
}

if ($Tag -eq "未标注") {
  Write-Host "测试前请：插上电源，按 Fn+Q 或在联想电脑管家里切好散热模式，关闭其他程序。" -ForegroundColor Yellow
  $t = Read-Host "当前是哪个模式？（安静 / 均衡 / 野兽，直接回车 = 未标注）"
  if ($t) { $Tag = $t.Trim() }
}

$Out = Join-Path $PSScriptRoot ("result-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $Tag)
New-Item -ItemType Directory -Path $Out -Force | Out-Null
Start-Transcript -Path (Join-Path $Out "transcript.txt") | Out-Null
function Save($name, $obj) { $obj | Out-File -FilePath (Join-Path $Out $name) -Encoding utf8 -Width 400 }

Write-Host "== [1/5] 系统与电源信息 ==" -ForegroundColor Cyan
Save "system.txt" (@(
  Get-CimInstance Win32_ComputerSystem | Format-List Manufacturer, Model, SystemFamily, TotalPhysicalMemory
  Get-CimInstance Win32_BIOS           | Format-List SMBIOSBIOSVersion, ReleaseDate
  Get-CimInstance Win32_Processor      | Format-List Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed
  Get-CimInstance Win32_Battery        | Format-List BatteryStatus, EstimatedChargeRemaining
  Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus -ErrorAction SilentlyContinue | Format-List PowerOnline, Charging, Discharging
) | Out-String)
Save "powercfg.txt" (@(
  powercfg /getactivescheme
  powercfg /query SCHEME_CURRENT SUB_PROCESSOR
  "---- Win11 电源模式覆盖 (Overlay) ----"
  Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes" -ErrorAction SilentlyContinue | Format-List *
) | Out-String)

Write-Host "== [2/5] 联想软件 / Intel DTT 驱动 ==" -ForegroundColor Cyan
$pat = 'Lenovo|PCManager|LITS|ImController|Vantage|DTT|Dynamic Tuning|esif|Intel.*(Innovation|Platform)|ipf|dptf'
Save "software.txt" (@(
  "---- 服务 ----"
  Get-Service | Where-Object { $_.Name -match $pat -or $_.DisplayName -match $pat } | Format-Table Name, DisplayName, Status, StartType -AutoSize
  "---- 进程 ----"
  Get-Process | Where-Object { $_.Name -match $pat -or $_.Path -match $pat } | Format-Table Name, Id, Path -AutoSize
  "---- 相关设备与驱动 ----"
  Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'INTC10|INT340|VPC2004|PNP0C14|IETM|LEN' -or $_.FriendlyName -match $pat } |
    ForEach-Object {
      $ver = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_DriverVersion -ErrorAction SilentlyContinue).Data
      [pscustomobject]@{ Name = $_.FriendlyName; InstanceId = $_.InstanceId; Status = $_.Status; Driver = $ver }
    } | Format-Table -AutoSize
) | Out-String)

Write-Host "== [3/5] 联想 WMI 接口定义与只读查询 ==" -ForegroundColor Cyan
$classes = Get-CimClass -Namespace root\wmi | Where-Object { $_.CimClassName -match 'LENOVO|Lenovo|GAMEZONE|GameZone|FAN|Fan|DYTC|ITS|SMART|Thermal|THERMAL' }
$defs = foreach ($c in $classes) {
  "################ $($c.CimClassName) ################"
  "GUID: " + (($c.CimClassQualifiers | Where-Object Name -eq 'guid').Value)
  "-- 属性 --"
  $c.CimClassProperties | ForEach-Object { "  {0} : {1}" -f $_.Name, $_.CimType }
  "-- 方法 --"
  foreach ($m in $c.CimClassMethods) {
    $params = ($m.Parameters | ForEach-Object {
      $dir = if ($_.Qualifiers.Name -contains 'in') { 'in' } elseif ($_.Qualifiers.Name -contains 'out') { 'out' } else { '?' }
      "$dir $($_.CimType) $($_.Name)"
    }) -join ', '
    "  $($m.Name)($params)"
  }
  ""
}
Save "wmi-classes.txt" ($defs | Out-String)

# 记录实例属性，并调用无输入参数的 Get*/Is* 方法
$getters = @()
$calls = foreach ($c in $classes) {
  $insts = @(Get-CimInstance -Namespace root\wmi -ClassName $c.CimClassName -ErrorAction SilentlyContinue)
  if ($insts.Count -eq 0) { continue }
  "################ $($c.CimClassName)（$($insts.Count) 个实例）################"
  $insts | Format-List * | Out-String
  foreach ($m in $c.CimClassMethods) {
    if ($m.Name -notmatch '^(Get|Is)') { continue }
    $inParams = @($m.Parameters | Where-Object { $_.Qualifiers.Name -contains 'in' })
    if ($inParams.Count -gt 0) { "  [跳过 $($m.Name)：需要输入参数 $($inParams.Name -join ',')]"; continue }
    try {
      $r = Invoke-CimMethod -InputObject $insts[0] -MethodName $m.Name -ErrorAction Stop
      $vals = ($r.PSObject.Properties | Where-Object { $_.Name -notmatch '^(PSComputerName|ReturnValue|CimClass|CimInstanceProperties|CimSystemProperties)$' } |
               ForEach-Object { "$($_.Name)=$($_.Value)" }) -join '; '
      "  $($m.Name)() -> ReturnValue=$($r.ReturnValue); $vals"
      $getters += [pscustomobject]@{ Class = $c.CimClassName; Method = $m.Name; Instance = $insts[0] }
    } catch { "  $($m.Name)() -> 失败: $($_.Exception.Message)" }
  }
  ""
}
Save "wmi-readings.txt" ($calls | Out-String)
Write-Host ("   找到 {0} 个相关 WMI 类，{1} 个可调用的只读方法" -f $classes.Count, $getters.Count)

# 采样中周期调用的方法：名称里带风扇/温度/模式字样的 getter
$liveGetters = @($getters | Where-Object { $_.Method -match 'Fan|Temp|Speed|Rpm|RPM|Mode|Thermal|Power' })

Write-Host "== [4/5] 负载过程采样（空闲 $IdleSec s → 满载 $LoadSec s → 冷却 $CoolSec s）==" -ForegroundColor Cyan
$perfClasses = Get-CimClass -ClassName 'Win32_PerfFormattedData_*' |
  Where-Object { $_.CimClassName -match 'ProcessorInformation|EnergyMeter|PowerMeter|ThermalZoneInformation' } |
  Select-Object -ExpandProperty CimClassName
$csv = Join-Path $Out "samples.csv"
"time,elapsed_s,phase,source,instance,metric,value" | Out-File $csv -Encoding utf8

function Sample($phase, $t0) {
  $now = Get-Date
  $el = [math]::Round(($now - $t0).TotalSeconds, 1)
  $ts = $now.ToString("HH:mm:ss")
  $lines = New-Object System.Collections.Generic.List[string]
  foreach ($pc in $perfClasses) {
    foreach ($i in (Get-CimInstance -ClassName $pc -ErrorAction SilentlyContinue)) {
      if ($pc -match 'ProcessorInformation' -and $i.Name -ne '_Total') { continue }
      foreach ($p in $i.CimInstanceProperties) {
        if ($p.Name -match '^(Caption|Description|Name|Frequency_|Timestamp_)' -or $null -eq $p.Value) { continue }
        if ($p.CimType -notmatch 'Int|Real') { continue }
        $lines.Add("$ts,$el,$phase,$pc,$($i.Name),$($p.Name),$($p.Value)")
      }
    }
  }
  foreach ($z in (Get-CimInstance -Namespace root\wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue)) {
    $lines.Add("$ts,$el,$phase,MSAcpi_ThermalZone,$($z.InstanceName -replace ',', ';'),CelsiusFromTenthK,$([math]::Round($z.CurrentTemperature / 10 - 273.15, 1))")
  }
  foreach ($g in $liveGetters) {
    try {
      $r = Invoke-CimMethod -InputObject $g.Instance -MethodName $g.Method -ErrorAction Stop
      foreach ($p in $r.PSObject.Properties) {
        if ($p.Name -match '^(PSComputerName|CimClass|CimInstanceProperties|CimSystemProperties)$') { continue }
        $lines.Add("$ts,$el,$phase,$($g.Class),$($g.Method),$($p.Name),$($p.Value)")
      }
    } catch {}
  }
  $lines | Out-File $csv -Append -Encoding utf8
}

$t0 = Get-Date
$jobs = @()
try {
  Write-Host "   空闲阶段..."
  $end = (Get-Date).AddSeconds($IdleSec); while ((Get-Date) -lt $end) { Sample "idle" $t0; Start-Sleep -Seconds $Interval }

  Write-Host "   满载阶段（$env:NUMBER_OF_PROCESSORS 线程）... 请留意风扇声音"
  1..[int]$env:NUMBER_OF_PROCESSORS | ForEach-Object {
    $jobs += Start-Job -ScriptBlock { $x = 0.0; while ($true) { $x = [math]::Sqrt($x + 12345.678) } }
  }
  $end = (Get-Date).AddSeconds($LoadSec); while ((Get-Date) -lt $end) { Sample "load" $t0; Start-Sleep -Seconds $Interval }
} finally {
  $jobs | Stop-Job -ErrorAction SilentlyContinue; $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
}
Write-Host "   冷却阶段..."
$end = (Get-Date).AddSeconds($CoolSec); while ((Get-Date) -lt $end) { Sample "cool" $t0; Start-Sleep -Seconds $Interval }

Write-Host "== [5/5] 7-Zip 基准（与 Linux 对比，可选）==" -ForegroundColor Cyan
$sevenZip = @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($sevenZip) {
  Start-Sleep -Seconds 30
  $st = (& $sevenZip b -mmt1 | Select-String '^Tot:').Line
  Start-Sleep -Seconds 30
  $mt = (& $sevenZip b | Select-String '^Tot:').Line
  Save "7zip.txt" ("单核: $st`r`n多核: $mt")
  Write-Host "   单核: $st"; Write-Host "   多核: $mt"
} else {
  Save "7zip.txt" "未安装 7-Zip，跳过"
  Write-Host "   未找到 7-Zip，跳过（可从 7-zip.org 安装后重跑）"
}

Stop-Transcript | Out-Null
Write-Host ""
Write-Host "完成！结果在: $Out" -ForegroundColor Green
