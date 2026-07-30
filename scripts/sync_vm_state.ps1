[CmdletBinding()]
param(
    [string]$Project = "project-55b8aafe-d086-47bd-8dd",
    [string]$Zone = "asia-south1-a",
    [string]$Vm = "crypto-apps-vm",
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root "data"
$stateDir = Join-Path $root ".vm-state"
$snapshotTool = Join-Path $root ".deploy\snapshot_live_events_db.py"
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$incoming = Join-Path $stateDir "incoming-$stamp"
$backup = Join-Path $root ".local-backups\vm-sync-$stamp"
$gcloudDefault = "C:\Users\ADMIN\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.ps1"
$gcloud = if (Test-Path -LiteralPath $gcloudDefault) {
    $gcloudDefault
} else {
    (Get-Command gcloud -ErrorAction Stop).Source
}

$plan = [ordered]@{
    production = "$Project/$Zone/$Vm"
    source = "/home/ADMIN/apps/lighter-trade-bot/data"
    destination = $dataDir
    backup = $backup
    databases = @("events.db", "command_center.db", "trading_journal.db")
    behavior = "Pull only; never uploads local databases to production"
}
$plan | ConvertTo-Json -Depth 4
if ($PlanOnly) {
    exit 0
}

if (-not (Test-Path -LiteralPath $snapshotTool)) {
    throw "Snapshot helper not found: $snapshotTool"
}
New-Item -ItemType Directory -Force -Path $incoming, $backup | Out-Null

& $gcloud compute scp $snapshotTool "${Vm}:/tmp/snapshot_db.py" `
    --zone $Zone --project $Project

$remoteCommand = @(
    "set -e",
    "app=/home/ADMIN/apps/lighter-trade-bot",
    "python3 /tmp/snapshot_db.py `$app/data/events.db /tmp/events.vm-state.db",
    "python3 /tmp/snapshot_db.py `$app/data/command_center.db /tmp/command-center.vm-state.db",
    "if test -f `$app/data/trading_journal.db; then python3 /tmp/snapshot_db.py `$app/data/trading_journal.db /tmp/trading-journal.vm-state.db; fi",
    "cd `$app",
    "sha256sum command_center/app.py command_center/ingest.py command_center/store.py command_center/lifecycles.py command_center/static/app.js command_center/static/index.html command_center/static/style.css trade_journal/app.py trade_journal/static/app.js trade_journal/static/index.html trade_journal/static/style.css src/dashboard.py apps_hub/access_page.py > /tmp/code.vm-state.sha256"
) -join " && "
& $gcloud compute ssh $Vm --zone $Zone --project $Project `
    "--command=$remoteCommand"

& $gcloud compute scp "${Vm}:/tmp/events.vm-state.db" `
    (Join-Path $incoming "events.db") --zone $Zone --project $Project
& $gcloud compute scp "${Vm}:/tmp/command-center.vm-state.db" `
    (Join-Path $incoming "command_center.db") --zone $Zone --project $Project
& $gcloud compute scp "${Vm}:/tmp/code.vm-state.sha256" `
    (Join-Path $incoming "code.sha256") --zone $Zone --project $Project

$journalRemote = & $gcloud compute ssh $Vm --zone $Zone --project $Project `
    '--command=test -f /tmp/trading-journal.vm-state.db && echo yes || echo no'
if (($journalRemote | Select-Object -Last 1).Trim() -eq "yes") {
    & $gcloud compute scp "${Vm}:/tmp/trading-journal.vm-state.db" `
        (Join-Path $incoming "trading_journal.db") --zone $Zone --project $Project
}

$python = if (Test-Path -LiteralPath "C:\Python312\python.exe") {
    "C:\Python312\python.exe"
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$dbFiles = Get-ChildItem -LiteralPath $incoming -Filter "*.db"
foreach ($file in $dbFiles) {
    $result = & $python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('pragma integrity_check').fetchone()[0])" $file.FullName
    if (($result | Select-Object -Last 1).Trim() -ne "ok") {
        throw "SQLite integrity check failed: $($file.Name)"
    }
}

$localNames = @(
    "events.db", "events.db-wal", "events.db-shm",
    "command_center.db", "command_center.db-wal", "command_center.db-shm",
    "trading_journal.db", "trading_journal.db-wal", "trading_journal.db-shm"
)
foreach ($name in $localNames) {
    $path = Join-Path $dataDir $name
    if (Test-Path -LiteralPath $path) {
        Move-Item -LiteralPath $path -Destination (Join-Path $backup $name)
    }
}
foreach ($file in $dbFiles) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $dataDir $file.Name)
}

$metadata = [ordered]@{
    pulled_at_utc = $stamp
    project = $Project
    zone = $Zone
    vm = $Vm
    mode = "production-to-local"
    backup = $backup
    databases = @(
        $dbFiles | ForEach-Object {
            [ordered]@{
                name = $_.Name
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            }
        }
    )
    code_manifest = @(
        Get-Content -LiteralPath (Join-Path $incoming "code.sha256") |
            ForEach-Object { $_.ToString() }
    )
}
$metadata | ConvertTo-Json -Depth 6 |
    Set-Content -LiteralPath (Join-Path $stateDir "current.json") -Encoding utf8

Write-Host "Local data now matches the VM snapshot."
Write-Host "Provenance: $(Join-Path $stateDir 'current.json')"
Write-Host "Previous local state: $backup"
