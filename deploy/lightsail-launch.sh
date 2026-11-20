#!/bin/bash
# Lightsail launch script for Signalpipe. Paste this into "Add launch script" when creating an
# Ubuntu 24.04 instance (2 GB plan: the LinkedIn agent runs a browser). It installs Docker, fetches the
# app, generates a password and starts the app, the LinkedIn agent and Caddy (automatic HTTPS).
# Takes about ten minutes after the instance boots.
#
# Optional: set your own domain before pasting (point its A record at the instance's static IP).
DOMAIN=""
REPO="https://github.com/nathenialalleyne/signalpipe"

set -euo pipefail
exec > /var/log/signalpipe-setup.log 2>&1
export DEBIAN_FRONTEND=noninteractive

# Swap gives the build and the agent's browser room on a small instance.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update -y
apt-get install -y ca-certificates curl git openssl
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -d /opt/signalpipe/.git ]; then
  git clone --depth 1 "$REPO" /opt/signalpipe
fi
cd /opt/signalpipe

if [ ! -f .env ]; then
  IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
  # Without your own domain, use a hostname that resolves to this IP so HTTPS can be issued.
  [ -n "$DOMAIN" ] || DOMAIN="${IP//./-}.sslip.io"
  umask 077
  cat > .env <<EOF
APP_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')
SECRET_KEY=$(openssl rand -hex 32)
API_TOKEN=$(openssl rand -hex 24)
DOMAIN=$DOMAIN
EOF
fi

docker compose up -d --build

. ./.env
cat > /root/signalpipe-info.txt <<EOF
Signalpipe is running.
Address:  https://$DOMAIN
Password: $APP_PASSWORD
LinkedIn: sign in once in Settings > "Open the agent's screen" (the agent then runs by itself)
Settings: /opt/signalpipe/.env  (add API keys here, then run: cd /opt/signalpipe && docker compose up -d)
Update:   cd /opt/signalpipe && git pull && docker compose up -d --build
EOF
chmod 600 /root/signalpipe-info.txt
echo "setup complete"
