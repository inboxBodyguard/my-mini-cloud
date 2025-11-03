#!/bin/bash
set -e  # Stop on any error

echo "🚀 Starting setup for My Mini Cloud..."

# ===== Update system =====
echo "📦 Updating system packages..."
sudo apt update -y && sudo apt upgrade -y

# ===== Install Docker and Docker Compose =====
echo "🐳 Installing Docker and Docker Compose..."
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Enable Docker to start on boot
sudo systemctl enable docker
sudo systemctl start docker

# Install Docker Compose (plugin-compatible binary)
DOCKER_COMPOSE_VERSION="2.24.5"
sudo curl -L "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# ===== Docker group setup =====
echo "👤 Adding current user to Docker group..."
sudo usermod -aG docker $USER
newgrp docker <<EONG
echo "✅ User added to Docker group."
EONG

# ===== Prepare directories =====
echo "📂 Creating required directories..."
mkdir -p letsencrypt data dashboard backups
touch letsencrypt/acme.json
chmod 600 letsencrypt/acme.json

# ===== Deploy containers =====
echo "🚢 Deploying containers using docker-compose..."
docker-compose down || true
docker-compose pull
docker-compose up -d

# ===== System services =====
echo "🧠 Enabling Docker auto-restart on reboot..."
sudo systemctl enable docker.service
sudo systemctl enable containerd.service

echo ""
echo "✅ Setup complete!"
echo "📦 Stack running: docker ps"
echo "🔍 Logs: docker-compose logs -f"
echo "🌐 Visit your dashboard at: https://platform.your-domain.com"