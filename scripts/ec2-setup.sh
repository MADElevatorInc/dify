#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04 EC2 instance.
#
# Prerequisites before running:
#   1. EC2 launched with an IAM role that has:
#        secretsmanager:GetSecretValue on dify/dev/*
#   2. Security group inbound: TCP 22, 80, 443 from 0.0.0.0/0
#   3. Elastic IP associated and Route 53 A record created:
#        madify.madelevator.com → <elastic IP>
#      (DNS must resolve before certbot step or it will fail)
set -euo pipefail

DOMAIN="madify.madelevator.com"
EMAIL="kuldeep.saini@madelevator.com"
APP_DIR="/opt/dify"

# ── Packages ─────────────────────────────────────────────────────────
sudo apt-get update -q
sudo apt-get install -y nginx certbot python3-certbot-nginx docker.io docker-compose-plugin python3 rsync

sudo systemctl enable --now docker nginx
sudo usermod -aG docker ubuntu

# ── App directory ─────────────────────────────────────────────────────
sudo mkdir -p "$APP_DIR"/{docker,scripts}
sudo chown -R ubuntu:ubuntu "$APP_DIR"

# ── SSL certificate via Let's Encrypt (free) ──────────────────────────
# Uses nginx plugin so certbot auto-configures renewal.
sudo certbot --nginx -d "$DOMAIN" \
  --email "$EMAIL" --agree-tos --non-interactive --redirect

# ── Host nginx: SSL terminator → Docker Compose on port 3000 ─────────
# Dify's docker nginx is bound to 127.0.0.1:3000 (see docker-compose note below).
sudo tee /etc/nginx/conf.d/madify.conf > /dev/null << NGINX
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 15M;
    proxy_read_timeout   300s;

    location / {
        proxy_pass         http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection keep-alive;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
    }
}
NGINX

sudo nginx -t && sudo systemctl reload nginx

# ── docker-compose.yaml note ──────────────────────────────────────────
# In docker/docker-compose.yaml, change the nginx service ports from:
#     ports:
#       - "80:80"
#       - "443:443"
# to:
#     ports:
#       - "127.0.0.1:3000:80"
# This keeps Dify's nginx local-only; the host nginx above handles SSL.
# ──────────────────────────────────────────────────────────────────────

echo ""
echo "EC2 setup complete. Remaining steps:"
echo "  1. Update docker/docker-compose.yaml nginx ports (see comment above)"
echo "  2. Create Secrets Manager secrets (see aws-secrets-template.json)"
echo "  3. Set GitHub repo variables: AWS_DEPLOY_ROLE_ARN, AWS_REGION=ca-central-1"
echo "  4. Push to deploy/dev — first deploy will pull images and start services"
echo "  5. Visit https://${DOMAIN}"
