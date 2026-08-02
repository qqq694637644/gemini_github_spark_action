param(
    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$RedirectUri,

    [Parameter(Mandatory = $true)]
    [string]$GitHubUsername,

    [string]$AllowedRepos = "",

    [string]$ClientId = "gemini-spark-personal",

    [switch]$HardenAcl,

    [switch]$Force
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required and was not found in PATH."
}
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 'Python 3.11 or newer is required')"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $venvPython -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$arguments = @(
    "scripts/setup_windows_builtin_oauth.py",
    "--public-base-url", $PublicBaseUrl,
    "--redirect-uri", $RedirectUri,
    "--github-username", $GitHubUsername,
    "--client-id", $ClientId
)
if (-not [string]::IsNullOrWhiteSpace($AllowedRepos)) {
    $arguments += @("--allowed-repos", $AllowedRepos)
}
if ($Force) {
    $arguments += "--force"
}

& $venvPython @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($HardenAcl) {
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $dataPath = Join-Path $repoRoot "data"
    $envPath = Join-Path $repoRoot ".env"

    & icacls.exe $dataPath /inheritance:r /grant:r `
        "*$($currentSid):(OI)(CI)F" `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply ACLs to $dataPath" }

    & icacls.exe $envPath /inheritance:r /grant:r `
        "*$($currentSid):F" `
        "*S-1-5-18:F" `
        "*S-1-5-32-544:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply ACLs to $envPath" }

    $keyPath = Join-Path $dataPath "oauth-signing-key.pem"
    $stream = [System.IO.File]::OpenRead($keyPath)
    $stream.Dispose()
}

Write-Host ""
Write-Host "Next command:" -ForegroundColor Cyan
Write-Host ".\scripts\run_windows_builtin_oauth.ps1 -Port 8005"
