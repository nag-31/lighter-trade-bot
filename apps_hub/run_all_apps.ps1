param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "data\app_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Processes = New-Object System.Collections.Generic.List[System.Diagnostics.Process]

function Test-Port {
    param([Parameter(Mandatory = $true)][int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        $ok = $task.Wait(500)
        $connected = $client.Connected
        $client.Dispose()
        return $ok -and $connected
    } catch { return $false }
}

function Start-App {
    param(
        [string]$Name,
        [string]$WorkingDirectory,
        [string[]]$Arguments,
        [int]$Port,
        [hashtable]$Environment = @{},
        [switch]$RestartExisting,
        [string]$ProcessMatch = ""
    )
    if (Test-Port -Port $Port) {
        if ($RestartExisting -and $ProcessMatch) {
            $matching = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object {
                Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
            } | Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -match $ProcessMatch }
            if ($matching) {
                foreach ($existing in $matching) {
                    Write-Host "Refreshing $Name (old pid $($existing.ProcessId)) ..."
                    Stop-Process -Id $existing.ProcessId -Force -ErrorAction Stop
                }
                $deadline = [DateTime]::UtcNow.AddSeconds(5)
                while ((Test-Port -Port $Port) -and [DateTime]::UtcNow -lt $deadline) {
                    Start-Sleep -Milliseconds 100
                }
            }
        }
        if (Test-Port -Port $Port) {
            Write-Host "Ready: $Name on port $Port (already running; not replaced)"
            return
        }
    }
    $stdout = Join-Path $LogDir "$Name.log"
    $stderr = Join-Path $LogDir "$Name.err.log"
    Write-Host "Starting $Name ..."
    $previousEnvironment = @{}
    try {
        foreach ($key in $Environment.Keys) {
            $previousEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
            [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key], "Process")
        }
        $proc = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    } finally {
        foreach ($key in $Environment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $previousEnvironment[$key], "Process")
        }
    }
    $Processes.Add($proc)
    Write-Host "  pid $($proc.Id), log $stdout"
}

function Stop-All {
    Write-Host ""
    Write-Host "Stopping apps started by this launcher ..."
    foreach ($proc in $Processes) { if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } }
}

try {
    Start-App -Name "command_center" -WorkingDirectory $Root -Arguments @("-B", "-m", "command_center.app", "--host", "127.0.0.1", "--port", "8810") -Port 8810 -RestartExisting -ProcessMatch "-m\s+command_center\.app(?:\s|$)"
    Start-App -Name "trade_journal" -WorkingDirectory $Root -Arguments @("-B", "-m", "trade_journal.app", "--host", "127.0.0.1", "--port", "8811") -Port 8811 -RestartExisting -ProcessMatch "-m\s+trade_journal\.app(?:\s|$)"
    Start-App -Name "portfolio" -WorkingDirectory $Root -Arguments @("-B", "-m", "src.portfolio_app", "--host", "127.0.0.1", "--port", "8790") -Port 8790
    Start-App -Name "pnl_analytics" -WorkingDirectory $Root -Arguments @("-B", "-m", "standalone.pnl_analytics_bot.dashboard.server", "--host", "127.0.0.1", "--port", "8787") -Port 8787
    Start-App -Name "apps_hub" -WorkingDirectory $Root -Arguments @("-B", "-m", "apps_hub.access_page", "--host", "127.0.0.1", "--port", "8800") -Port 8800 -RestartExisting -ProcessMatch "-m\s+apps_hub\.access_page(?:\s|$)"
    Start-App -Name "trade_tracker" -WorkingDirectory $Root -Arguments @("-B", "-m", "src.dashboard") -Port 8080
    $FullBotRoot = Join-Path $Root "bots\full_fledged_bot"
    if (Test-Path (Join-Path $FullBotRoot "full_fledged_bot\cli.py")) { Start-App -Name "full_fledged_bot" -WorkingDirectory $FullBotRoot -Arguments @("-B", "-m", "full_fledged_bot.cli", "--config", "config.example.yaml", "serve") -Port 18080 }
    $WorkspaceRoot = Split-Path -Parent $Root
    $HackAlertRoot = Join-Path $WorkspaceRoot "hack-alert-bot"
    if (Test-Path (Join-Path $HackAlertRoot "alertbot\__main__.py")) {
        Start-App -Name "hack_alert_bot" -WorkingDirectory $HackAlertRoot -Arguments @("-B", "-m", "alertbot", "--dashboard") -Port 8788 -RestartExisting -ProcessMatch "-m\s+alertbot(?:\s|$)" -Environment @{
            ALERTBOT_DASHBOARD_HOST = "127.0.0.1"
            ALERTBOT_DASHBOARD_PORT = "8788"
        }
    }
    Write-Host ""
    Write-Host "Signal Research:  http://127.0.0.1:8810/"
    Write-Host "Trade Journal:    http://127.0.0.1:8811/"
    Write-Host "Crypto Scientist App Hubs: http://127.0.0.1:8800/"
    if (Test-Path (Join-Path $HackAlertRoot "alertbot\__main__.py")) { Write-Host "TVL Monitor:      http://127.0.0.1:8788/" }
    Write-Host "Logs: $LogDir"
    Write-Host ""
    Write-Host "Press Enter to stop apps started by this launcher."
    [void][Console]::ReadLine()
} finally { Stop-All }
