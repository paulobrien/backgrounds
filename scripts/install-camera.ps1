[CmdletBinding()]
param(
    [string] $DllPath = (Join-Path $PSScriptRoot '..\build\virtual-camera\Release\BackgroundsCamera.dll')
)
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem) { throw 'This build requires 64-bit Windows.' }
if (-not (Test-Path -LiteralPath $DllPath -PathType Leaf)) { throw "Camera DLL not found: $DllPath. Build it first; see native\virtual-camera\README.md." }
$installDirectory = Join-Path $env:LOCALAPPDATA 'BackgroundsCamera'
$targetDll = Join-Path $installDirectory 'BackgroundsCamera.dll'
New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
try {
    Copy-Item -LiteralPath $DllPath -Destination $targetDll -Force
} catch {
    throw "Could not copy the camera DLL. Close Teams and other apps using the camera, then retry. $($_.Exception.Message)"
}
$process = Start-Process -FilePath "$env:SystemRoot\System32\regsvr32.exe" -ArgumentList @('/s', "`"$targetDll`"") -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) { throw "Camera registration failed (regsvr32 exit code $($process.ExitCode))." }
Write-Host "Installed Backgrounds Virtual Camera for the current Windows user: $targetDll"
Write-Host 'Restart Teams or other camera applications, then select Backgrounds Virtual Camera.'

