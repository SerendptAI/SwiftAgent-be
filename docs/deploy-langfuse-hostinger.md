# Deploying Langfuse on Hostinger VPS

## Step 0 — Check your plan

> [!IMPORTANT]
> Langfuse's full stack (Postgres + ClickHouse + Redis + MinIO + app + worker) needs at minimum 8GB RAM. KVM 1 (4GB) will OOM. You need at least KVM 2 (8GB) — KVM 4 (16GB) is ideal.

| Your Hostinger Plan | Verdict |
|---|---|
| KVM 1 — 1 vCPU / 4GB | ❌ Too small |
| KVM 2 — 2 vCPU / 8GB | ⚠️ Workable (tight) |
| KVM 4 — 4 vCPU / 16GB | ✅ Recommended |
| KVM 8 — 8 vCPU / 32GB | ✅ Comfortable |

## Step 1 — SSH into your VPS

From Hostinger hPanel → VPS → SSH Access, grab your IP and root credentials.

```bash
ssh root@YOUR_VPS_IP
```

## Step 2 — Install Docker & Docker Compose

```bash
# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Verify
docker --version
docker compose version

# (Optional) Allow running docker without sudo
usermod -aG docker $USER
```

## Step 3 — Point your domain to the VPS

In Hostinger hPanel → DNS Zone (or wherever your domain lives):

```text
A    langfuse.yourdomain.com    →    YOUR_VPS_IP
```

Wait for DNS to propagate (~5 min on Hostinger, up to 30 min globally).

## Step 4 — Clone Langfuse & configure

```bash
# Clone the repo
git clone https://github.com/langfuse/langfuse.git
cd langfuse

# Generate secrets
NEXTAUTH_SECRET=$(openssl rand -hex 32)
SALT=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)
DB_PASSWORD=$(openssl rand -hex 16)

echo "NEXTAUTH_SECRET=$NEXTAUTH_SECRET"
echo "SALT=$SALT"
echo "ENCRYPTION_KEY=$ENCRYPTION_KEY"
echo "DB_PASSWORD=$DB_PASSWORD"
```

Create your `.env` file:

```bash
cat > .env << EOF
# App
NEXTAUTH_URL=https://langfuse.yourdomain.com
NEXTAUTH_SECRET=${NEXTAUTH_SECRET}
SALT=${SALT}
ENCRYPTION_KEY=${ENCRYPTION_KEY}

# Database
DATABASE_URL=postgresql://langfuse:${DB_PASSWORD}@db:5432/langfuse
POSTGRES_USER=langfuse
POSTGRES_PASSWORD=${DB_PASSWORD}
POSTGRES_DB=langfuse

# Telemetry (optional, disable for privacy)
TELEMETRY_ENABLED=false

# Port
PORT=3000
EOF
```

## Step 5 — Add Nginx + SSL to the Docker Compose stack

Create an Nginx config:

```bash
mkdir -p nginx/conf.d
cat > nginx/conf.d/langfuse.conf << 'EOF'
server {
    listen 80;
    server_name langfuse.yourdomain.com;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name langfuse.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/langfuse.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/langfuse.yourdomain.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # Increase buffer sizes for large LLM payloads
    client_max_body_size 50M;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://langfuse-web:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF
```

Add Nginx + Certbot to `docker-compose.override.yml` (so you don't modify the main file):

```bash
cat > docker-compose.override.yml << 'EOF'
services:
  nginx:
    image: nginx:alpine
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - ./nginx/certbot/conf:/etc/letsencrypt:ro
      - ./nginx/certbot/www:/var/www/certbot:ro
    depends_on:
      - langfuse-web

  certbot:
    image: certbot/certbot
    volumes:
      - ./nginx/certbot/conf:/etc/letsencrypt
      - ./nginx/certbot/www:/var/www/certbot
EOF
```

## Step 6 — Get SSL certificate

```bash
# 1. Start just Nginx on port 80 first (with a temp config that doesn't require certs)
# Temporarily rename the ssl server block — or start with a plain HTTP-only config
# Simplest: use certbot standalone (stop nginx first if running)
docker run --rm \
  -p 80:80 \
  -v "$(pwd)/nginx/certbot/conf:/etc/letsencrypt" \
  -v "$(pwd)/nginx/certbot/www:/var/www/certbot" \
  certbot/certbot certonly \
  --standalone \
  --email your@email.com \
  --agree-tos \
  --no-eff-email \
  -d langfuse.yourdomain.com
```

## Step 7 — Open firewall ports

```bash
# On Hostinger — also do this in hPanel → VPS → Firewall
ufw allow 22    # SSH
ufw allow 80    # HTTP
ufw allow 443   # HTTPS
ufw enable
```

> [!NOTE]
> In Hostinger hPanel, go to VPS → Firewall and also add rules for ports 80 and 443 there — the hPanel firewall sits above UFW.

## Step 8 — Launch the full stack

```bash
# Start everything
docker compose up -d

# Watch logs until ready (~2-3 mins)
docker compose logs -f langfuse-web
```

When you see `"Ready on http://localhost:3000"` → you're live. Visit `https://langfuse.yourdomain.com` — you'll see the Langfuse sign-up page. The first account created becomes the admin.

## Step 9 — Auto-renew SSL

```bash
# Add a cron job for cert renewal
(crontab -l 2>/dev/null; echo "0 12 * * * docker run --rm -v $(pwd)/nginx/certbot/conf:/etc/letsencrypt -v $(pwd)/nginx/certbot/www:/var/www/certbot certbot/certbot renew --quiet && docker exec langfuse_nginx_1 nginx -s reload") | crontab -
```

## Step 10 — Connect SwiftAgent-be

Create API keys in Langfuse UI → Settings → API Keys, then add to your `SwiftAgent-be` `.env`:

```env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://langfuse.yourdomain.com
```

---

## ⚠️ Hostinger-Specific Gotchas

| Issue | Fix |
|---|---|
| **Port 3000 exposed publicly** | Make sure hPanel firewall only allows 80/443 — block 3000. |
| **VPS reboots lose containers** | Run docker compose up -d on reboot via cron: `@reboot cd /root/langfuse && docker compose up -d` |
| **Low RAM (KVM 2)** | Add a 4GB swapfile: `fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile` |
| **ClickHouse eating all RAM** | Fine-tune clickhouse memory limits in `docker-compose.override.yml`. |
