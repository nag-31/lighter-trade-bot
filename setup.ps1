Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -OutFile "python-installer.exe"
.\python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
Start-Sleep -Seconds 30

$env:PATH = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

Invoke-WebRequest -Uri "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe" -OutFile "git-installer.exe"
.\git-installer.exe /VERYSILENT
Start-Sleep -Seconds 15

$env:PATH = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

git clone https://github.com/nag-31/lighter-trade-bot.git
cd lighter-trade-bot
pip install -r requirements.txt
