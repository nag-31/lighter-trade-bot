$ErrorActionPreference = "Stop"

$Repo = "D:\content\crypto scientist\lighter-trade-bot"
$Python = "C:\Python314\python.exe"
$SecretFile = Join-Path $Repo ".env.portfolio-private.local"
$LogDir = Join-Path $Repo "data\app_logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Test-Path -LiteralPath $SecretFile)) {
    $secure = Read-Host "Create the private portfolio password (12+ characters)" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        if ($plain.Length -lt 12) { throw "Password must contain at least 12 characters." }
        $hash = $plain | & $Python -B -c "import sys; sys.path.insert(0, r'D:\content\crypto scientist\lighter-trade-bot'); from src.portfolio_app import hash_password; print(hash_password(sys.stdin.read().rstrip()))"
    }
    finally {
        if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        $plain = $null
    }
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $sessionSecret = [Convert]::ToBase64String($bytes)
    @(
        "PORTFOLIO_PASSWORD_HASH=$hash"
        "PORTFOLIO_SESSION_SECRET=$sessionSecret"
    ) | Set-Content -LiteralPath $SecretFile -Encoding utf8
    Write-Host "Created local private credentials at $SecretFile"
}

$settings = @{}
Get-Content -LiteralPath $SecretFile | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $settings[$matches[1]] = $matches[2] }
}
if (-not $settings.PORTFOLIO_PASSWORD_HASH -or -not $settings.PORTFOLIO_SESSION_SECRET) {
    throw "Private credential file is incomplete: $SecretFile"
}

$processes = @()
try {
    $guestOut = Join-Path $LogDir "portfolio_guest.out.log"
    $guestErr = Join-Path $LogDir "portfolio_guest.err.log"
    $processes += Start-Process -FilePath $Python -ArgumentList @(
        '-B', '-m', 'src.portfolio_app', '--host', '127.0.0.1',
        '--port', '8790', '--storage-mode', 'guest'
    ) -WorkingDirectory $Repo -RedirectStandardOutput $guestOut `
      -RedirectStandardError $guestErr -WindowStyle Hidden -PassThru

    $env:PORTFOLIO_PASSWORD_HASH = $settings.PORTFOLIO_PASSWORD_HASH
    $env:PORTFOLIO_SESSION_SECRET = $settings.PORTFOLIO_SESSION_SECRET
    $privateOut = Join-Path $LogDir "portfolio_private.out.log"
    $privateErr = Join-Path $LogDir "portfolio_private.err.log"
    $processes += Start-Process -FilePath $Python -ArgumentList @(
        '-B', '-m', 'src.portfolio_app', '--host', '127.0.0.1',
        '--port', '8791', '--storage-mode', 'private',
        '--db-path', ('"' + (Join-Path $Repo 'data\portfolio.db') + '"')
    ) -WorkingDirectory $Repo -RedirectStandardOutput $privateOut `
      -RedirectStandardError $privateErr -WindowStyle Hidden -PassThru
}
finally {
    Remove-Item Env:\PORTFOLIO_PASSWORD_HASH -ErrorAction SilentlyContinue
    Remove-Item Env:\PORTFOLIO_SESSION_SECRET -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2
Write-Host ""
Write-Host "Guest portfolio:   http://127.0.0.1:8790/"
Write-Host "Private portfolio: http://127.0.0.1:8791/"
Write-Host ""
Write-Host "Press Enter to stop both portfolio apps."
[void](Read-Host)

foreach ($process in $processes) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id }
}
