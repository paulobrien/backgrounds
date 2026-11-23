[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$installDirectory = Join-Path $env:LOCALAPPDATA 'BackgroundsCamera'
$targetDll = Join-Path $installDirectory 'BackgroundsCamera.dll'
$classPath = 'HKCU:\Software\Classes\CLSID\{27F551F8-5C2A-4A53-AFBF-942DA7F5E451}\InprocServer32'
$classItem = Get-Item -LiteralPath $classPath -ErrorAction SilentlyContinue
$registeredPath = if ($classItem) { $classItem.GetValue('') } else { $null }
if ($registeredPath -and -not [string]::Equals($registeredPath, $targetDll, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Camera is registered from another path: $registeredPath. This script will not unregister that DLL."
}
if ($registeredPath) {
    if (-not (Test-Path -LiteralPath $targetDll -PathType Leaf)) { throw "Registered DLL is missing: $targetDll. Restore the DLL before uninstalling." }
    $process = Start-Process -FilePath "$env:SystemRoot\System32\regsvr32.exe" -ArgumentList @('/s', '/u', "`"$targetDll`"") -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "Camera unregistration failed (regsvr32 exit code $($process.ExitCode))." }
}
if (Test-Path -LiteralPath $targetDll -PathType Leaf) {
    try { Remove-Item -LiteralPath $targetDll -Force }
    catch { throw "Could not remove the DLL. Close Teams and other apps using the camera, then retry. $($_.Exception.Message)" }
}
if ((Test-Path -LiteralPath $installDirectory -PathType Container) -and @(Get-ChildItem -LiteralPath $installDirectory -Force).Count -eq 0) {
    Remove-Item -LiteralPath $installDirectory
}
Write-Host 'Removed Backgrounds Virtual Camera for the current Windows user.'


