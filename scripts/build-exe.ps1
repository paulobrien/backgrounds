[CmdletBinding()]
param(
    [switch] $BundleFfmpeg,
    [string] $FfmpegPath
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$entry = Join-Path $repoRoot 'app\backgrounds.py'
$distDir = Join-Path $repoRoot 'dist'
$workDir = Join-Path $repoRoot 'build\pyinstaller'
$iconPath = Join-Path $workDir 'BackgroundsCamera.ico'

& py -3 -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is not installed. Install it with: py -3 -m pip install --user pyinstaller'
}

New-Item -ItemType Directory -Path $workDir -Force | Out-Null
& py -3 -c "import sys; sys.path.insert(0, sys.argv[1]); from backgrounds import create_app_icon; create_app_icon().save(sys.argv[2], sizes=[(16,16), (32,32), (48,48), (256,256)])" (Join-Path $repoRoot 'app') $iconPath
if ($LASTEXITCODE -ne 0) {
    throw 'Could not create the application icon. Install the application dependencies with: py -3 -m pip install -r requirements.txt'
}

$args = @(
    '-3', '-m', 'PyInstaller',
    '--noconfirm',
    '--onefile',
    '--windowed',
    '--name', 'BackgroundsCamera',
    '--icon', $iconPath,
    '--hidden-import', 'pystray._win32',
    '--distpath', $distDir,
    '--workpath', $workDir,
    '--specpath', $workDir
)

if ($BundleFfmpeg -and -not $FfmpegPath) {
    $cmd = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw 'Could not find ffmpeg.exe on PATH. Pass -FfmpegPath <path-to-ffmpeg.exe>.'
    }
    $FfmpegPath = $cmd.Source
}

if ($FfmpegPath) {
    if (-not (Test-Path -LiteralPath $FfmpegPath -PathType Leaf)) {
        throw "ffmpeg.exe not found: $FfmpegPath"
    }
    $args += @('--add-binary', "$FfmpegPath;.")
}

$args += $entry

& py @args
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

Write-Host "Built standalone executable: $(Join-Path $distDir 'BackgroundsCamera.exe')"
