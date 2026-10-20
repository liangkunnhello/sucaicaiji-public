# Create a desktop shortcut pointing to the silent launcher .vbs
# Chinese names are built from Unicode code points so the file's on-disk
# encoding can never corrupt them (Windows PowerShell reads .ps1 as GBK).

$ErrorActionPreference = 'Stop'

$appDir = Split-Path -Parent $PSScriptRoot

# 启动-无窗口.vbs
$vbsName = [string]::Concat([char]0x542F,[char]0x52A8,'-',[char]0x65E0,[char]0x7A97,[char]0x53E3,'.vbs')
$target = Join-Path $appDir $vbsName

$icon = Join-Path $appDir 'node_modules\electron\dist\electron.exe'

$ws = New-Object -ComObject WScript.Shell
$desktop = $ws.SpecialFolders('Desktop')

# 素材采集分类工具.lnk
$lnkName = [string]::Concat(
  [char]0x7D20,[char]0x6750,[char]0x91C7,[char]0x96C6,[char]0x5206,[char]0x7C7B,
  [char]0x5DE5,[char]0x5177,'.lnk')
$lnkPath = Join-Path $desktop $lnkName

$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $target
$lnk.WorkingDirectory = $appDir
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Description = 'Material Capture and Classification Tool'
$lnk.Save()

Write-Output ("OK: " + $lnkPath)
