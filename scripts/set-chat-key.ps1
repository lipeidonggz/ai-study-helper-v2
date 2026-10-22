$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found. Run the README setup commands first."
}

$secureKey = Read-Host "DeepSeek API Key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:ASH_CHAT_KEY_TEMP = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    & $python -c "import os; from app.storage.credentials import CHAT_API_KEY_USERNAME, WindowsCredentialStore; key = os.environ.pop('ASH_CHAT_KEY_TEMP', ''); WindowsCredentialStore().set(CHAT_API_KEY_USERNAME, key)"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to store the chat API key."
    }
    Write-Host "DeepSeek chat API key stored in Windows Credential Manager."
}
finally {
    Remove-Item Env:ASH_CHAT_KEY_TEMP -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
