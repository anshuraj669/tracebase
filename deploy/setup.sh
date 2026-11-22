#!/usr/bin/env bash
# One-time setup of Tracebase on a fresh Ubuntu 22.04 / 24.04 server (AWS Lightsail, EC2, any VPS).
# Run it on the server, from the copied project folder:
#
#   sudo ./deploy/setup.sh --domain tracebase-demo.duckdns.org            # your own repos (put them in /srv/tracebase/repos)
#   sudo ./deploy/setup.sh --domain tracebase-demo.duckdns.org --demo     # public demo with the demo repos + logs
#   sudo ./deploy/setup.sh --domain auto --demo                           # no domain yet: <public-ip>.sslip.io
#
# Safe to run again (keeps the existing deploy/.env and admin token).
set -euo pipefail

DOMAIN="" DEMO=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --demo) DEMO=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
ENV="$HERE/.env"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Installing Docker"
if ! command -v docker >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git python3 openssl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker >/dev/null
[[ -n "${SUDO_USER:-}" ]] && usermod -aG docker "$SUDO_USER" || true

say "Swap (helps the image build on 1-2 GB servers)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

say "Folders"
mkdir -p /srv/tracebase/repos /srv/tracebase/logs

if [[ "$DOMAIN" == "auto" ]]; then
  IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
  DOMAIN="${IP//./-}.sslip.io"
  echo "no domain: using $DOMAIN"
fi

say "Settings ($ENV)"
if [[ ! -f "$ENV" ]]; then
  [[ -n "$DOMAIN" ]] || { echo "first run needs --domain <name> (or --domain auto)" >&2; exit 1; }
  cp "$HERE/.env.example" "$ENV"
  sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|; s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$(openssl rand -hex 32)|" "$ENV"
  chmod 600 "$ENV"
  echo "created, with a new admin token"
elif [[ -n "$DOMAIN" ]]; then
  sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" "$ENV"
fi

if [[ $DEMO -eq 1 ]]; then
  say "Demo data (repos + logs)"
  python3 "$ROOT/demo/seed.py" >/dev/null || true      # no-op if it already exists
  sed -i "s|^REPOS_DIR=.*|REPOS_DIR=$ROOT/demo/volume1/repos|; s|^LOGS_DIR=.*|LOGS_DIR=$ROOT/demo/volume1/logs|" "$ENV"
fi

say "Firewall"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi
echo "also open ports 80 and 443 in the AWS console (Lightsail: Networking tab · EC2: security group)"

say "Building and starting (first build takes a few minutes)"
cd "$HERE"
docker compose -f docker-compose.prod.yml --env-file "$ENV" up -d --build

say "Repo sync every 15 minutes (git pull of everything in REPOS_DIR)"
install -m 0755 "$HERE/pull-repos.sh" /usr/local/bin/tracebase-pull-repos
REPOS="$(grep '^REPOS_DIR=' "$ENV" | cut -d= -f2-)"
echo "*/15 * * * * root /usr/local/bin/tracebase-pull-repos $REPOS >> /var/log/tracebase-pull.log 2>&1" > /etc/cron.d/tracebase-pull

DOMAIN="$(grep '^DOMAIN=' "$ENV" | cut -d= -f2-)"
cat <<EOF

$(printf '\033[1;32m')Tracebase is starting at https://$DOMAIN$(printf '\033[0m')
  (the HTTPS certificate is fetched on the first visit; DNS must already point at this server)

Admin token (keep it secret):   sudo grep ADMIN_TOKEN $ENV
Add a person (from your laptop, with the tracebase CLI):
  TRACEBASE_URL=https://$DOMAIN TRACEBASE_TOKEN=<admin token> tracebase team add priya --repos "client-a/*"
  (or with curl from anywhere: see deploy/README.md)
Logs:     cd $HERE && docker compose -f docker-compose.prod.yml logs -f
Update:   copy the new code here, then: sudo ./deploy/setup.sh
EOF
