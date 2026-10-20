$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
if (Test-Path $cargoBin) {
    $env:PATH = "$cargoBin;$env:PATH"
}

$existingVite = Get-NetTCPConnection -State Listen -LocalPort 1420 -ErrorAction SilentlyContinue
if ($existingVite) {
    throw "Port 1420 is already in use. Stop the existing Vite process first."
}

Push-Location $root
$runtimeDir = Join-Path $root "frontend\.runtime"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$viteOut = Join-Path $runtimeDir "vite.log"
$viteErr = Join-Path $runtimeDir "vite-error.log"
$vite = Start-Process `
    -FilePath "corepack.cmd" `
    -ArgumentList @("pnpm", "--dir", (Join-Path $root "frontend"), "dev") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $viteOut `
    -RedirectStandardError $viteErr `
    -PassThru
$appExit = 0
try {
    $viteReady = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (Get-NetTCPConnection -State Listen -LocalPort 1420 -ErrorAction SilentlyContinue) {
            $viteReady = $true
            break
        }
    }
    if (-not $viteReady) {
        throw "Vite did not start. See frontend/.runtime/vite-error.log."
    }

    cargo run --manifest-path desktop/src-tauri/Cargo.toml
    $appExit = $LASTEXITCODE
}
finally {
    $viteListeners = Get-NetTCPConnection -State Listen -LocalPort 1420 -ErrorAction SilentlyContinue
    foreach ($listener in $viteListeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if (
            $process.CommandLine -like "*ai-study-helper-v2*frontend*vite*" -or
            $process.CommandLine -like "*ai-study-helper-v2*frontend\node_modules*vite*"
        ) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Stop-Process -Id $vite.Id -Force -ErrorAction SilentlyContinue
    Pop-Location
}

exit $appExit
