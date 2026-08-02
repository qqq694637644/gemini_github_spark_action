param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$dataPath = Join-Path $repoRoot "data"
$envPath = Join-Path $repoRoot ".env"
$keyPath = Join-Path $dataPath "oauth-signing-key.pem"
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

if (-not (Test-Path $dataPath)) {
    throw "Missing data directory: $dataPath"
}

& takeown.exe /F $dataPath /R /D Y | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "takeown failed for $dataPath. Re-run this PowerShell window as Administrator."
}

& icacls.exe $dataPath /inheritance:r /grant:r `
    "*$($currentSid):(OI)(CI)F" `
    "*S-1-5-18:(OI)(CI)F" `
    "*S-1-5-32-544:(OI)(CI)F" `
    /T /C | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "icacls failed for $dataPath"
}

if (Test-Path $envPath) {
    & takeown.exe /F $envPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "takeown failed for $envPath."
    }
    & icacls.exe $envPath /inheritance:r /grant:r `
        "*$($currentSid):F" `
        "*S-1-5-18:F" `
        "*S-1-5-32-544:F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed for $envPath"
    }
}

if (-not (Test-Path $keyPath)) {
    throw "Missing OAuth signing key: $keyPath"
}

$stream = [System.IO.File]::OpenRead($keyPath)
try {
    Write-Host "OAuth signing key is readable. Bytes: $($stream.Length)" -ForegroundColor Green
} finally {
    $stream.Dispose()
}

Write-Host "Windows OAuth ACL repair completed." -ForegroundColor Cyan
