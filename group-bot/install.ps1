$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
if (-not (Test-Path "$PSScriptRoot\runtime\python.exe")) {
    Write-Host 'Downloading portable Python 3.12.10 from python.org...'
    Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile "$PSScriptRoot\python-runtime.zip"
    Expand-Archive "$PSScriptRoot\python-runtime.zip" -DestinationPath "$PSScriptRoot\runtime" -Force
    Remove-Item "$PSScriptRoot\python-runtime.zip"
}
$pathConfig = "$PSScriptRoot\runtime\python312._pth"
if (-not ((Get-Content $pathConfig) -contains '..')) {
    Add-Content -Path $pathConfig -Value '..'
}
& "$PSScriptRoot\runtime\python.exe" "$PSScriptRoot\bot.py" setup
if ($LASTEXITCODE -ne 0) { throw 'Setup did not finish. Run SETUP.cmd again.' }
# Protect the token against other ordinary local Windows accounts.
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe "$PSScriptRoot" /inheritance:r /grant:r "${account}:(OI)(CI)(F)" '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
& icacls.exe "$PSScriptRoot\settings.json" /inheritance:r /grant:r "${account}:(F)" '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
Write-Host 'Setup complete. Run RUN.cmd for a manual start, or AUTOSTART.cmd for automatic startup.'
