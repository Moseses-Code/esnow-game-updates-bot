$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path "$PSScriptRoot\settings.json")) { throw 'Run SETUP.cmd first.' }
# The task runs at boot even before Windows logon. The script directory must be trusted.
$action = New-ScheduledTaskAction -Execute "$PSScriptRoot\runtime\python.exe" -Argument ('"' + "$PSScriptRoot\bot.py" + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ESnowGameUpdates' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'ESnow: game update reports at 08:30 and 20:30 Moscow time.' -Force | Out-Null
Start-ScheduledTask -TaskName 'ESnowGameUpdates'
Write-Host 'Autostart installed and bot started. Check /status in Telegram.'
Read-Host 'Press Enter to close'
