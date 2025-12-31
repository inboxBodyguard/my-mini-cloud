#!/bin/bash
# aws-setup.sh - Deploy Mini Cloud to AWS

set -e

echo "🚀 Deploying Mini Cloud Platform to AWS..."

# ===== Configuration =====
REGION="us-east-1"
INSTANCE_TYPE="t3.small"
SECURITY_GROUP="mini-cloud-sg"
KEY_NAME="mini-cloud-key"
TAG_NAME="MiniCloudPlatform"

# ===== Create Security Group =====
echo "🔐 Creating security group..."
aws ec2 create-security-group \
  --group-name "$SECURITY_GROUP" \
  --description "Mini Cloud Platform Security Group" \
  --region "$REGION" || true

# Open ports: SSH(22), HTTP(80), HTTPS(443), Dashboard(3000)
PORTS=(22 80 443 3000 8000 8080)
for port in "${PORTS[@]}"; do
  aws ec2 authorize-security-group-ingress \
    --group-name "$SECURITY_GROUP" \
    --protocol tcp \
    --port "$port" \
    --cidr 0.0.0.0/0 \
    --region "$REGION" || true
done

# ===== Create Key Pair =====
echo "🔑 Creating key pair..."
aws ec2 create-key-pair \
  --key-name "$KEY_NAME" \
  --query 'KeyMaterial' \
  --output text > "$KEY_NAME.pem"
chmod 400 "$KEY_NAME.pem"
echo "✅ Key saved to $KEY_NAME.pem"

# ===== Launch EC2 Instance =====
echo "🖥️ Launching EC2 instance..."
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-0c55b159cbfafe1f0 \  # Ubuntu 22.04 LTS
  --count 1 \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-groups "$SECURITY_GROUP" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$TAG_NAME}]" \
  --region "$REGION" \
  --query 'Instances[0].InstanceId' \
  --output text)

echo "⏳ Waiting for instance to start..."
aws ec2 wait instance-running \
  --instance-ids "$INSTANCE_ID" \
  --region "$REGION"

# Get public IP
PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text \
  --region "$REGION")

echo "✅ Instance $INSTANCE_ID running at $PUBLIC_IP"

# ===== Generate Setup Script =====
cat > remote-setup.sh << 'EOF'
#!/bin/bash
set -e

# Update system
apt update -y && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
usermod -aG docker ubuntu

# Install Docker Compose
curl -L "https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Clone your repository
git clone https://github.com/YOUR_USERNAME/my-mini-cloud.git /opt/mini-cloud
cd /opt/mini-cloud

# Create .env file
cat > .env << 'ENVEOF'
PLATFORM_DOMAIN=$PUBLIC_IP.nip.io
DOCKER_HOST=unix:///var/run/docker.sock
DATABASE_URL=postgresql://admin:password@postgres:5432/cloudplatform
SECRET_KEY=$(openssl rand -hex 32)
ENVEOF

# Run setup
chmod +x setup.sh
./setup.sh

# Start the Node.js dashboard server
cd /opt/mini-cloud/dashboard
npm install
npm install dockerode http-proxy-middleware ws

# Create systemd service for dashboard
cat > /etc/systemd/system/mini-cloud-dashboard.service << 'SERVICEEOF'
[Unit]
Description=Mini Cloud Dashboard
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/mini-cloud/dashboard
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=PLATFORM_API_URL=http://localhost:8000
Environment=DOCKER_SOCKET=/var/run/docker.sock
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable mini-cloud-dashboard
systemctl start mini-cloud-dashboard

echo "✅ Setup complete!"
echo "🌐 Dashboard: http://$PUBLIC_IP:3000"
echo "🔧 API: http://$PUBLIC_IP:8000"
EOF

# ===== Copy and Run Setup Script =====
echo "📤 Uploading setup script to instance..."
scp -i "$KEY_NAME.pem" -o StrictHostKeyChecking=no remote-setup.sh ubuntu@$PUBLIC_IP:/home/ubuntu/

echo "🚀 Running setup on remote instance..."
ssh -i "$KEY_NAME.pem" -o StrictHostKeyChecking=no ubuntu@$PUBLIC_IP "bash /home/ubuntu/remote-setup.sh"

# ===== Output Summary =====
echo ""
echo "🎉 DEPLOYMENT COMPLETE!"
echo "======================="
echo "🌐 Dashboard URL: http://$PUBLIC_IP:3000"
echo "🔧 API Endpoint: http://$PUBLIC_IP:8000"
echo "📱 Deploy apps at: http://platform.$PUBLIC_IP.nip.io"
echo ""
echo "🔑 SSH Access: ssh -i $KEY_NAME.pem ubuntu@$PUBLIC_IP"
echo "📊 View logs: ssh -i $KEY_NAME.pem ubuntu@$PUBLIC_IP 'docker-compose -f /opt/mini-cloud/docker-compose.yml logs -f'"
echo ""
echo "💰 Estimated monthly cost: ~$15 (t3.small)"