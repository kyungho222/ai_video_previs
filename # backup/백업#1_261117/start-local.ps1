param(
    [string]$Python = 'python',
    [ValidateRange(1, 65535)]
    [int]$Port = 9000
)

$ErrorActionPreference = 'Stop'
$previousFrontend = $env:JUTSU_FRONTEND_URL
$previousRedirect = $env:JUTSU_OAUTH_REDIRECT_URL
$previousDirectory = Get-Location
$serverExitCode = 0

try {
    Set-Location -LiteralPath $PSScriptRoot
    $localOrigin = "http://127.0.0.1:$Port"
    $env:JUTSU_FRONTEND_URL = "$localOrigin/previs-console01.html"
    $env:JUTSU_OAUTH_REDIRECT_URL = "$localOrigin/api/jutsu/callback"

    & $Python -B -m uvicorn service_app:create_app --factory --host 127.0.0.1 --port $Port --workers 1
    $serverExitCode = $LASTEXITCODE
}
finally {
    $env:JUTSU_FRONTEND_URL = $previousFrontend
    $env:JUTSU_OAUTH_REDIRECT_URL = $previousRedirect
    Set-Location -LiteralPath $previousDirectory
}

exit $serverExitCode
