# Build OutboundAI Docker image locally (requires Docker Desktop)
# Usage: .\build-docker.ps1
#        .\build-docker.ps1 -Tag "outboundai:v1"

param(
    [string]$Tag = "outboundai:latest"
)

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error @"
Docker is not installed or not in PATH.

Install Docker Desktop for Windows:
  https://docs.docker.com/desktop/setup/install/windows-install/

Then restart your terminal and run:
  .\build-docker.ps1
"@
    exit 1
}

Set-Location $PSScriptRoot

Write-Host "Building Docker image: $Tag" -ForegroundColor Cyan
docker build -t $Tag .

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nBuild successful!" -ForegroundColor Green
    Write-Host "Run locally (set env vars first or use Azure in production):"
    Write-Host "  docker run --rm -p 8000:8000 --env-file .env $Tag"
    Write-Host "`nOr with individual env vars from Azure-style config:"
    Write-Host "  docker run --rm -p 8000:8000 -e CONFIG_FROM_ENV_ONLY=true -e PORT=8000 ... $Tag"
}
