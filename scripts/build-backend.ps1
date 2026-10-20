param(
    [switch]$SkipClean
)

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $root "backend"
$python = Join-Path $backend ".venv\Scripts\python.exe"
$spec = Join-Path $backend "ai-study-helper-backend.spec"
$dist = Join-Path $backend "dist"
$work = Join-Path $backend "build\pyinstaller"
$resourceRoot = Join-Path $root "desktop\src-tauri\resources"
$resourceTarget = Join-Path $resourceRoot "backend"
$built = Join-Path $dist "ai-study-helper-backend"

function Remove-BuildPath {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside repository: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found. Run the README setup commands first."
}

if (-not $SkipClean) {
    Remove-BuildPath $dist
    Remove-BuildPath $work
}
Remove-BuildPath $resourceTarget

New-Item -ItemType Directory -Force -Path $dist, $work, $resourceRoot | Out-Null

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $dist `
    --workpath $work `
    $spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $built)) {
    throw "PyInstaller output not found at $built"
}

Copy-Item -LiteralPath $built -Destination $resourceTarget -Recurse

$files = Get-ChildItem -LiteralPath $resourceTarget -Recurse -File
$sizeMb = [Math]::Round((($files | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "Backend packaged: $resourceTarget"
Write-Host "Files: $($files.Count); Size: $sizeMb MB"
