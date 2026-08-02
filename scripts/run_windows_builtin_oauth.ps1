param(
    [int]$Port = 8005
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Missing .venv. Run .\scripts\setup_windows_builtin_oauth.ps1 first."
}
if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    throw "Missing .env. Run .\scripts\setup_windows_builtin_oauth.ps1 first."
}

& $venvPython -m uvicorn app.main:app `
    --host 127.0.0.1 `
    --port $Port `
    --proxy-headers `
    --forwarded-allow-ips 127.0.0.1
exit $LASTEXITCODE
