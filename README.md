# Mini Cloud Platform 🚀

A lightweight, self-hosted PaaS (Platform as a Service) that lets you deploy web applications from Git repositories with automatic SSL, reverse proxy, and container orchestration.

## 🎯 What It Does

Mini Cloud Platform transforms any VPS into a Heroku-like deployment system. Push your code to Git, provide the URL, and get a live application with HTTPS in minutes.

**Key Features:**

- 🔄 Deploy from Git repositories (Node.js, Python, static sites)
- 🔒 Automatic SSL certificates via Let’s Encrypt
- 🌐 Reverse proxy with Traefik
- 📊 PostgreSQL database included
- 🐳 Docker containerization for each app
- 📈 Resource monitoring and metrics
- 🔐 Multi-user support with authentication
- 💾 Automatic backups
- 🔄 Auto-updates with Watchtower

## 🏗️ Architecture

```
┌─────────────┐
│   Traefik   │ ← Reverse Proxy + SSL (Port 80/443)
└──────┬──────┘
       │
┌──────┴──────────────────────────┐
│                                  │
│  ┌──────────┐    ┌───────────┐  │
│  │ Platform │    │ PostgreSQL│  │
│  │   API    │    │           │  │
│  └────┬─────┘    └───────────┘  │
│       │                          │
│  ┌────┴─────────────────┐       │
│  │   Deployed Apps      │       │
│  │  (Docker Containers) │       │
│  └──────────────────────┘       │
│                                  │
└──────────────────────────────────┘
    Docker Network: mini-cloud-network
```

## 🚀 Quick Start

### Prerequisites

- Linux VPS (Ubuntu 20.04+ recommended)
- Docker & Docker Compose installed
- Domain name pointing to your server
- 2GB+ RAM, 20GB+ disk space

### Installation

1. **Clone the repository:**

```bash
git clone https://github.com/yourusername/mini-cloud-platform.git
cd mini-cloud-platform
```

1. **Configure environment:**

```bash
# Edit docker-compose.yml
nano docker-compose.yml
```

Update these values:

- `PLATFORM_DOMAIN`: Your domain (e.g., `example.com`)
- `SECRET_KEY`: Generate with `openssl rand -hex 32`
- `POSTGRES_PASSWORD`: Strong database password
- Email in Traefik’s ACME configuration

1. **Start the platform:**

```bash
docker-compose up -d
```

1. **Verify installation:**

```bash
docker-compose ps
docker-compose logs -f platform
```

1. **Access the dashboard:**

```
https://platform.your-domain.com
```

## 📱 Usage

### Deploy Your First App

#### Via API:

```bash
curl -X POST https://platform.your-domain.com/api/deploy \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "git_url": "https://github.com/username/repo.git",
    "environment_variables": {
      "NODE_ENV": "production"
    }
  }'
```

#### Via Dashboard:

1. Navigate to `https://platform.your-domain.com/dashboard`
1. Click “Deploy New App”
1. Enter Git repository URL
1. Add environment variables (optional)
1. Click “Deploy”

Your app will be available at: `https://my-app.your-domain.com`

### Supported Frameworks

The platform auto-detects and deploys:

- **Node.js** (Express, Next.js, React, etc.)
- **Python** (Flask, Django, FastAPI)
- **Static Sites** (HTML/CSS/JS)

If no `Dockerfile` exists, one is automatically generated.

## 🔧 API Endpoints

### Authentication

```bash
# Register
POST /api/auth/register
{
  "email": "user@example.com",
  "password": "secure_password"
}

# Login
POST /api/auth/login
```

### App Management

```bash
# List apps
GET /api/apps

# Get app details
GET /api/apps/{app_id}

# Deploy app
POST /api/deploy

# Start/Stop/Restart
POST /api/apps/{app_id}/start
POST /api/apps/{app_id}/stop
POST /api/apps/{app_id}/restart

# Delete app
DELETE /api/apps/{app_id}

# View logs
GET /api/apps/{app_id}/logs

# Get stats
GET /api/apps/{app_id}/stats
```

### Platform Stats

```bash
# Overall platform statistics
GET /api/stats

# Prometheus metrics
GET /metrics
```

## 🗂️ Project Structure

```
mini-cloud-platform/
├── main.py                 # FastAPI application
├── server.js               # Example Node.js app
├── database.py             # SQLAlchemy models
├── docker-compose.yml      # Docker orchestration
├── dockerfile              # Platform container
├── requirements.txt        # Python dependencies
├── dashboard/              # Web UI
├── data/                   # Persistent data
├── backups/                # Database backups
└── letsencrypt/            # SSL certificates
```

## 🔐 Security Features

- **Container Isolation**: Each app runs in its own Docker container
- **Resource Limits**: CPU and memory constraints per app
- **No New Privileges**: Security opt prevents privilege escalation
- **Automatic SSL**: Let’s Encrypt certificates with auto-renewal
- **User Authentication**: Token-based auth system
- **Database Encryption**: Secure password hashing

## 📊 Monitoring

### View Platform Metrics

```bash
curl https://platform.your-domain.com/metrics
```

### Check App Logs

```bash
# Via API
curl https://platform.your-domain.com/api/apps/{app_id}/logs

# Via Docker
docker logs app-{app_id}
```

### Resource Usage

```bash
# Get app statistics
curl https://platform.your-domain.com/api/apps/{app_id}/stats
```

## 💾 Backup & Recovery

### Automatic Backups

Backups run automatically via `cron-backup.sh`. Configure in your crontab:

```bash
# Edit crontab
crontab -e

# Add daily backup at 2 AM
0 2 * * * /path/to/mini-cloud-platform/cron-backup.sh
```

### Manual Backup

```bash
# Trigger backup via API
curl -X POST https://platform.your-domain.com/api/admin/backup

# Or run backup script
./backup.py
```

### List Backups

```bash
curl https://platform.your-domain.com/api/admin/backups
```

### Restore from Backup

```bash
# Stop platform
docker-compose down

# Extract backup
tar -xzf backups/backup_20231122_020000.tar.gz -C /

# Restart platform
docker-compose up -d
```

## 🔄 Updates

The platform uses **Watchtower** for automatic container updates:

- Checks for updates every 5 minutes
- Automatically pulls new images
- Restarts containers with updated versions
- Cleans up old images

To manually update:

```bash
docker-compose pull
docker-compose up -d
```

## ⚙️ Configuration

### Environment Variables

|Variable         |Description          |Default                      |
|-----------------|---------------------|-----------------------------|
|`PLATFORM_DOMAIN`|Your domain name     |`localhost`                  |
|`SECRET_KEY`     |JWT secret key       |Required                     |
|`DATABASE_URL`   |PostgreSQL connection|Auto-configured              |
|`DOCKER_HOST`    |Docker socket path   |`unix:///var/run/docker.sock`|

### Resource Limits (per app)

Edit `main.py` to adjust:

- Memory: 512MB limit, 256MB reserved
- CPU: 0.5 cores
- Max restart attempts: 3

## 🐛 Troubleshooting

### App Won’t Deploy

```bash
# Check platform logs
docker-compose logs -f platform

# Check app logs
docker logs app-{app_id}

# Verify network
docker network inspect mini-cloud-network
```

### SSL Certificate Issues

```bash
# Check Traefik logs
docker-compose logs -f traefik

# Verify DNS records
dig platform.your-domain.com
```

### Database Connection Errors

```bash
# Check PostgreSQL status
docker-compose logs postgres

# Test connection
docker exec -it mini-cloud-platform-postgres-1 psql -U admin -d cloudplatform
```

### Port Conflicts

```bash
# Check what's using port 80/443
sudo netstat -tlnp | grep ':80\|:443'

# Stop conflicting services
sudo systemctl stop apache2  # or nginx
```

## 🤝 Contributing

1. Fork the repository
1. Create a feature branch (`git checkout -b feature/amazing-feature`)
1. Commit your changes (`git commit -m 'Add amazing feature'`)
1. Push to the branch (`git push origin feature/amazing-feature`)
1. Open a Pull Request

## 📝 License

MIT License - see LICENSE file for details

## 🆘 Support

- **Issues**: [GitHub Issues](https://github.com/imboxBodyguard/mini-cloud-platform/issues)
- **Docs**: [Full Documentation](https://docs.your-domain.com)
- **Discord**: [Community Server](https://discord.gg/yourserver)

## 🎯 Roadmap

- [ ] GitHub Actions integration
- [ ] Horizontal scaling support
- [ ] Built-in Redis/MongoDB services
- [ ] Custom domain support per app
- [ ] CI/CD webhooks
- [ ] One-click rollback
- [ ] App marketplace/templates
- [ ] Multi-region deployment

## 💡 Example Apps

Deploy these example repositories to test your platform:

- Node.js API: `https://github.com/example/node-api`
- Python Flask: `https://github.com/example/flask-app`
- React SPA: `https://github.com/example/react-app`
- Static Site: `https://github.com/example/static-site`

-----

**Built with ❤️ using FastAPI, Docker, and Traefik**

*Transform your VPS into a powerful deployment platform in minutes.*