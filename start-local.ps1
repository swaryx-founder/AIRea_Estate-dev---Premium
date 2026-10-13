# Start AI API + LiveKit agent + open dashboard (Windows local)
# Usage:  .\start-local.ps1
# Optional: .\start-local.ps1 -Port 8001 -AgentName outbound-caller-test-local

param(
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8000,
    [string]$AgentName = "outbound-caller-test-local"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:LOAD_DOTENV = "true"
$env:AI_AGENT_NAME = $AgentName
$env:LIVEKIT_AGENT_NAME = $AgentName
$env:AGENT_NAME = $AgentName

Write-Host "Starting API on http://${HostAddress}:$Port ..." -ForegroundColor Cyan
Start-Process -FilePath "python" -ArgumentList @(
    "-m", "uvicorn", "server:app",
    "--host", "$HostAddress",
    "--port", "$Port",
    "--reload"
) -WorkingDirectory $PSScriptRoot -WindowStyle Normal

Start-Sleep -Seconds 3

Write-Host "Starting agent ($AgentName) ..." -ForegroundColor Cyan
Start-Process -FilePath "python" -ArgumentList @("agent.py", "start") `
    -WorkingDirectory $PSScriptRoot -WindowStyle Normal

Start-Sleep -Seconds 4

$lanIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL' -and $_.IPAddress -notlike '169.254*' } | Select-Object -First 1 -ExpandProperty IPAddress)
$localUrl = "http://127.0.0.1:$Port/"
$lanUrl = if ($lanIp) { "http://${lanIp}:$Port/" } else { $localUrl }

Write-Host "Opening dashboard: $localUrl" -ForegroundColor Green
Start-Process $localUrl

Write-Host ""
Write-Host "Done. Keep both Python windows open while calling." -ForegroundColor Green
Write-Host "  Local URL:   $localUrl"
if ($lanIp) {
    Write-Host "  Network URL: $lanUrl (Share with friends on same Wi-Fi)" -ForegroundColor Cyan
}
Write-Host "  Agent:       $AgentName"
