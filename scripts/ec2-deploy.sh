#!/usr/bin/env bash
# Runs on the EC2 instance during every deploy.
# EC2's IAM role must allow secretsmanager:GetSecretValue on dify/dev/*.
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ca-central-1}"
APP_SECRET="dify/dev/app-config"
COMPOSE_DIR="/opt/dify/docker"

echo "[deploy] Fetching app config from Secrets Manager..."

# Converts JSON secret → .env format.
# Values are double-quoted; internal " and newlines are escaped.
aws secretsmanager get-secret-value \
  --region "$REGION" \
  --secret-id "$APP_SECRET" \
  --query SecretString \
  --output text | \
python3 -c "
import sys, json
for k, v in json.load(sys.stdin).items():
    v = str(v).replace('\"', '\\\\\"').replace('\n', '\\\\n')
    print(f'{k}=\"{v}\"')
" > "$COMPOSE_DIR/.env"

chmod 600 "$COMPOSE_DIR/.env"

# Bind nginx to 127.0.0.1:3000 only - host nginx handles SSL and proxies here.
# These override the defaults (80:80 and 443:443) without touching docker-compose.yaml.
echo 'EXPOSE_NGINX_PORT=127.0.0.1:3000' >> "$COMPOSE_DIR/.env"
echo 'EXPOSE_NGINX_SSL_PORT=127.0.0.1:3001' >> "$COMPOSE_DIR/.env"
echo 'NGINX_HTTPS_ENABLED=false' >> "$COMPOSE_DIR/.env"

echo "[deploy] Pulling images..."
cd "$COMPOSE_DIR"
docker compose pull --quiet

echo "[deploy] Starting services..."
docker compose up -d --remove-orphans

echo "[deploy] Completed at $(date -u)"
