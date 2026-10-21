$ErrorActionPreference = "Stop"

$packageRoot = $PSScriptRoot
$sourceRoot = Join-Path $packageRoot "ai-relay"
$buildRoot = Join-Path $packageRoot ".build"
$workPath = Join-Path $buildRoot "work"
$specPath = Join-Path $buildRoot "spec"
$buildPython = if ($env:AI_RELAY_BUILD_PYTHON) { $env:AI_RELAY_BUILD_PYTHON } else { "python" }

& $buildPython -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "AI多Agent流水线" `
    --icon (Join-Path $sourceRoot "ai-relay.ico") `
    --workpath $workPath `
    --distpath $packageRoot `
    --specpath $specPath `
    (Join-Path $sourceRoot "launcher.py")
