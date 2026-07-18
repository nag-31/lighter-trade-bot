param([string]$Python = "python")

$HubLauncher = Join-Path (Split-Path -Parent $PSScriptRoot) "apps_hub\run_all_apps.ps1"
& $HubLauncher -Python $Python
