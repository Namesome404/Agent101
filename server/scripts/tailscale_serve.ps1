# Tailscale HTTPS for Muse (iPhone mic/camera via browser)
# Usage:  .\tailscale_serve.ps1
#         .\tailscale_serve.ps1 -Reset

param(
    [switch]$Reset,
    [int]$MusePort = 8002
)

$ErrorActionPreference = "Stop"
$Ts = "${env:ProgramFiles}\Tailscale\tailscale.exe"
if (-not (Test-Path $Ts)) {
    Write-Error "Tailscale not found. Install from https://tailscale.com/download/windows"
}

function Get-TailnetHost {
    $json = & $Ts status --json | ConvertFrom-Json
    $dns = [string]$json.Self.DNSName
    return $dns.TrimEnd('.')
}

if ($Reset) {
    & $Ts serve reset
    Write-Host "tailscale serve reset done."
    exit 0
}

Write-Host "=== Tailscale status ===" -ForegroundColor Cyan
& $Ts status

$TailnetHost = Get-TailnetHost
if (-not $TailnetHost) {
    Write-Error "Cannot read tailnet DNS name. Check tailscale up and MagicDNS."
}

Write-Host ""
Write-Host "Tailnet host: $TailnetHost" -ForegroundColor Green
Write-Host ""
Write-Host "REQUIRED: Enable HTTPS in Tailscale admin:" -ForegroundColor Yellow
Write-Host "  https://login.tailscale.com/admin/dns"
Write-Host "  - Enable MagicDNS"
Write-Host "  - Enable HTTPS Certificates"
Write-Host "  Wait 1-2 minutes after saving, then run this script again."
Write-Host ""

$certOk = $true
try {
    $null = & $Ts cert $TailnetHost 2>&1
    if ($LASTEXITCODE -ne 0) { $certOk = $false }
} catch {
    $certOk = $false
}
if (-not $certOk) {
    Write-Host "HTTPS certificates not ready. Enable MagicDNS + HTTPS Certificates in admin, then retry." -ForegroundColor Red
    Write-Host "Docs: xiaozhi-esp32-server/docs/tailscale-iphone-mic.md"
    exit 1
}

Write-Host "=== tailscale serve -> Muse port $MusePort ===" -ForegroundColor Cyan
Write-Host "Muse proxies /xiaozhi/v1 and /mcp/vision to core."
& $Ts serve reset 2>$null
& $Ts serve --bg $MusePort
Start-Sleep -Seconds 2
& $Ts serve status

Write-Host ""
Write-Host "=== iPhone ===" -ForegroundColor Green
Write-Host "  1) Tailscale app on, same account"
Write-Host "  2) Safari: https://$TailnetHost/"
Write-Host "  3) Open agent terminal, allow mic/camera"
Write-Host "  4) Bind 6-digit code in Muse console"
Write-Host ""
Write-Host "ESP32: still use LAN http://<LAN-IP>:8003/xiaozhi/ota/ (plain ws)"
