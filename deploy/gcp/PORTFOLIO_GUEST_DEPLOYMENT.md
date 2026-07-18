# Portfolio Guest Mode on Cloud Run

Guest mode keeps wallet lists, labels, filters, and historical snapshots in the
visitor's browser using IndexedDB. The Cloud Run service receives addresses only
for the duration of a refresh request and does not open or write the portfolio
SQLite database.

## Privacy boundary

- Other visitors cannot see another visitor's wallets or history.
- The server does not persist wallet addresses in application storage.
- Refresh responses are marked `no-store` and the page uses a no-referrer policy.
- RPC, exchange, and protocol providers still receive queried public addresses.
- Cloud Run request logs should not include request bodies; do not add address
  values to application logs.
- Browser data disappears if site storage is cleared. Use the encrypted backup
  in Settings to preserve or move it. The passphrase is never stored.

## Build and deploy

Run these commands in PowerShell. Replace `YOUR_GCP_PROJECT_ID` once.

```powershell
$ProjectId = "YOUR_GCP_PROJECT_ID"
$Region = "asia-south1"
$Repository = "crypto-apps"
$Gcloud = "C:\Users\ADMIN\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.ps1"
$Repo = "D:\content\crypto scientist\lighter-trade-bot"

& $Gcloud config set project $ProjectId
& $Gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
& $Gcloud artifacts repositories describe $Repository --location $Region 2>$null
if ($LASTEXITCODE -ne 0) {
  & $Gcloud artifacts repositories create $Repository --repository-format docker --location $Region
}

& $Gcloud builds submit $Repo --config "$Repo\cloudbuild.portfolio.yaml" --substitutions "_REGION=$Region,_REPOSITORY=$Repository,_IMAGE=portfolio-guest"

& $Gcloud run deploy portfolio-guest `
  --image "$Region-docker.pkg.dev/$ProjectId/$Repository/portfolio-guest:guest" `
  --region $Region `
  --platform managed `
  --allow-unauthenticated `
  --port 8080 `
  --memory 1Gi `
  --cpu 1 `
  --concurrency 20 `
  --max-instances 3 `
  --timeout 300 `
  --set-env-vars "PORTFOLIO_STORAGE_MODE=guest,PORTFOLIO_GUEST_RATE_LIMIT=12"
```

The final command prints the public service URL. Wallet data remains scoped to
that exact browser origin, so changing domains starts a separate local store.

## Custom domain

After mapping a custom domain, restrict refresh requests to it:

```powershell
$AllowedOrigin = "https://portfolio.example.com"
& "C:\Users\ADMIN\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.ps1" run services update portfolio-guest --region "asia-south1" --update-env-vars "PORTFOLIO_ALLOWED_ORIGINS=$AllowedOrigin"
```

Keep the default Cloud Run URL restricted at the load balancer if the custom
domain must be the only public entry point. Add Cloud Armor rate limiting before
raising the in-process refresh limit for substantial public traffic.

## Local guest-mode check

```powershell
& "C:\Python314\python.exe" -B -m src.portfolio_app --host "127.0.0.1" --port 8791 --storage-mode guest
```

Open `http://127.0.0.1:8791/`. This mode intentionally does not read the local
portfolio database. The existing launcher can continue using local mode.
