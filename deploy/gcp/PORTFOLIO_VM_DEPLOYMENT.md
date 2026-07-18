# Portfolio VM Deployment

This deployment runs two independent services from the same codebase:

- `portfolio.8-231-102-153.sslip.io`: stateless guest mode on port 8790.
- `private-portfolio.8-231-102-153.sslip.io`: password-protected private mode on port 8791.

The guest service stores wallets and history only in each visitor's browser. The
private service uses `/home/ADMIN/apps/lighter-trade-bot/data/portfolio.db`.
Neither service runs a scheduled portfolio refresh. A refresh starts only when
the page is loaded with data at least 24 hours old or the Refresh button is used.

## Local Windows links

Run the dedicated launcher in PowerShell:

```powershell
& "D:\content\crypto scientist\lighter-trade-bot\scripts\run_portfolio_apps.ps1"
```

On first run it asks for the private password and creates the ignored local file
`D:\content\crypto scientist\lighter-trade-bot\.env.portfolio-private.local`.

## VM installation

Run these commands after connecting to the VM:

```bash
cd /home/ADMIN/apps/lighter-trade-bot
/usr/bin/git pull --ff-only origin master
/home/ADMIN/apps/lighter-trade-bot/.venv/bin/python -m pip install -r /home/ADMIN/apps/lighter-trade-bot/requirements.txt

read -s -p "Private portfolio password: " PORTFOLIO_PASSWORD
printf '\n'
PORTFOLIO_PASSWORD_HASH="$(printf '%s' "$PORTFOLIO_PASSWORD" | /home/ADMIN/apps/lighter-trade-bot/.venv/bin/python -B -c 'import sys; from src.portfolio_app import hash_password; print(hash_password(sys.stdin.read()))')"
unset PORTFOLIO_PASSWORD
PORTFOLIO_SESSION_SECRET="$(/usr/bin/openssl rand -base64 48)"
/usr/bin/printf '%s\n' \
  "PORTFOLIO_PASSWORD_HASH=$PORTFOLIO_PASSWORD_HASH" \
  "PORTFOLIO_SESSION_SECRET=$PORTFOLIO_SESSION_SECRET" \
  "PORTFOLIO_ALLOWED_ORIGINS=https://private-portfolio.8-231-102-153.sslip.io" \
  > /home/ADMIN/apps/lighter-trade-bot/.env.portfolio-private
/usr/bin/chmod 600 /home/ADMIN/apps/lighter-trade-bot/.env.portfolio-private

/usr/bin/sudo /usr/bin/cp /home/ADMIN/apps/lighter-trade-bot/deploy/gcp/portfolio.service /etc/systemd/system/portfolio.service
/usr/bin/sudo /usr/bin/cp /home/ADMIN/apps/lighter-trade-bot/deploy/gcp/portfolio-private.service /etc/systemd/system/portfolio-private.service
/usr/bin/sudo /usr/bin/cp /home/ADMIN/apps/lighter-trade-bot/deploy/gcp/crypto-apps-nginx.conf /etc/nginx/sites-available/crypto-apps
/usr/bin/sudo /usr/bin/ln -sfn /etc/nginx/sites-available/crypto-apps /etc/nginx/sites-enabled/crypto-apps
/usr/bin/sudo /usr/sbin/nginx -t
/usr/bin/sudo /usr/bin/systemctl daemon-reload
/usr/bin/sudo /usr/bin/systemctl enable --now portfolio.service portfolio-private.service
/usr/bin/sudo /usr/bin/systemctl reload nginx
```

## HTTPS

The private password must not be sent over plain HTTP. Issue or renew both
certificates with Certbot before using the private site:

```bash
/usr/bin/sudo /usr/bin/certbot --nginx \
  -d portfolio.8-231-102-153.sslip.io \
  -d private-portfolio.8-231-102-153.sslip.io \
  --redirect
```

Verify the services:

```bash
/usr/bin/systemctl status portfolio.service portfolio-private.service --no-pager
/usr/bin/curl --fail --silent https://portfolio.8-231-102-153.sslip.io/api/config
/usr/bin/curl --head https://private-portfolio.8-231-102-153.sslip.io/
```

The private response should redirect to `/login`. Database files, environment
files, logs, and local wallet-validation evidence are excluded from Git.
