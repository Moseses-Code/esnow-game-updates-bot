@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -Command ""Stop-ScheduledTask -TaskName ESnowGameUpdates -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName ESnowGameUpdates -Confirm:$false""' -Wait"
pause
