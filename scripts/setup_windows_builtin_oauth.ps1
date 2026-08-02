param(
    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$RedirectUri,

    [Parameter(Mandatory = $true)]
    [string]$GitHubUsername,

    [Parameter(Mandatory = $true)]
    [string]$AllowedRepos,

    [string]$ClientId = "gemini-spark-personal",

    [switch]$Force
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "PowerShell 7 or newer is required. Start this script with pwsh."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required and was not found in PATH."
}
if (-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
    throw "PowerShell 7 executable 'pwsh' was not found in PATH."
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
    "--allowed-repos", $AllowedRepos,
    "--client-id", $ClientId
)
if ($Force) {
    $arguments += "--force"
}

& $venvPython @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

foreach ($sensitivePath in @((Join-Path $repoRoot "data"), (Join-Path $repoRoot ".env"))) {
    try {
        & icacls.exe $sensitivePath /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls exited with code $LASTEXITCODE" }
    } catch {
        Write-Warning "Could not tighten ACL for $sensitivePath automatically: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Next command:" -ForegroundColor Cyan
Write-Host ".\scripts\run_windows_builtin_oauth.ps1 -Port 8005"
