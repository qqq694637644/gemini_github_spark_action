param(
    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl
)

$ErrorActionPreference = "Stop"
$base = [Uri]$PublicBaseUrl.TrimEnd('/')
$origin = "$($base.Scheme)://$($base.Authority)"
$prefix = $base.AbsolutePath.TrimEnd('/')
$healthUrl = "$PublicBaseUrl/healthz"
$resourceMetadataUrl = "$origin/.well-known/oauth-protected-resource$prefix/mcp"
$authorizationMetadataUrl = "$origin/.well-known/oauth-authorization-server$prefix"
$mcpUrl = "$PublicBaseUrl/mcp"

function Invoke-WebRequestCompat {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,

        [string]$Method = "GET"
    )

    try {
        return Invoke-WebRequest -Uri $Uri -Method $Method -UseBasicParsing
    } catch {
        $response = $_.Exception.Response
        if (-not $response) {
            throw
        }

        $content = ""
        $stream = $response.GetResponseStream()
        if ($stream) {
            $reader = New-Object System.IO.StreamReader($stream)
            try {
                $content = $reader.ReadToEnd()
            } finally {
                $reader.Dispose()
                $stream.Dispose()
            }
        }

        return [PSCustomObject]@{
            StatusCode = [int]$response.StatusCode
            Headers = $response.Headers
            Content = $content
        }
    }
}

function Assert-Status([string]$Name, $Response, [int[]]$Expected) {
    if ($Expected -notcontains [int]$Response.StatusCode) {
        throw "$Name returned HTTP $($Response.StatusCode); expected $($Expected -join ', ')."
    }
    Write-Host "${Name}: HTTP $($Response.StatusCode)" -ForegroundColor Green
}

$health = Invoke-WebRequestCompat -Uri $healthUrl
Assert-Status "health" $health @(200)
$healthJson = $health.Content | ConvertFrom-Json
if ($healthJson.auth_mode -ne "builtin_oauth") {
    throw "Gateway auth_mode is '$($healthJson.auth_mode)', expected 'builtin_oauth'."
}

$resourceMetadata = Invoke-WebRequestCompat -Uri $resourceMetadataUrl
Assert-Status "protected resource metadata" $resourceMetadata @(200)
$resourceJson = $resourceMetadata.Content | ConvertFrom-Json
if (-not $resourceJson.authorization_servers) {
    throw "Protected resource metadata has no authorization_servers."
}

$authorizationMetadata = Invoke-WebRequestCompat -Uri $authorizationMetadataUrl
Assert-Status "authorization server metadata" $authorizationMetadata @(200)
$authorizationJson = $authorizationMetadata.Content | ConvertFrom-Json
if (-not $authorizationJson.authorization_endpoint -or -not $authorizationJson.token_endpoint) {
    throw "Authorization server metadata is missing authorization_endpoint or token_endpoint."
}

$challenge = Invoke-WebRequestCompat -Uri $mcpUrl -Method "HEAD"
Assert-Status "MCP OAuth challenge" $challenge @(401)
$authenticate = [string]$challenge.Headers["WWW-Authenticate"]
if ($authenticate -notmatch "resource_metadata=") {
    throw "MCP 401 challenge does not contain resource_metadata: $authenticate"
}

Write-Host ""
Write-Host "OAuth discovery is ready for Gemini." -ForegroundColor Cyan
Write-Host "MCP URL: $mcpUrl"
Write-Host "Authorization endpoint: $($authorizationJson.authorization_endpoint)"
Write-Host "Token endpoint: $($authorizationJson.token_endpoint)"
