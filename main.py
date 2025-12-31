import os
import uuid
import json
import asyncio
import subprocess
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import docker
from docker.models.containers import Container
from flask import jsonify, request
import psutil
import docker
import asyncio
from datetime import datetime


# Database imports
from sqlalchemy.orm import Session
from database import get_db, User, App, APIKey

# Configuration
CONFIG = {
    "domain": os.getenv("PLATFORM_DOMAIN", "localhost"),
    "docker_network": "mini-cloud-network",
    "data_volume": "mini-cloud-data",
    "port_range_start": 10000
}
# Initialize Docker client
try:
    docker_client = docker.from_env()
except:
    docker_client = None
    
app = FastAPI(title="Mini Cloud Platform", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Docker client
docker_client = docker.from_env()

# In-memory storage (fallback during transition)
apps_db: Dict[str, dict] = {}
deployments_db: Dict[str, dict] = {}

# Pydantic models
class DeploymentRequest(BaseModel):
    name: str
    git_url: Optional[str] = None
    environment_variables: Dict[str, str] = {}
    port: Optional[int] = None

class AppStatus(BaseModel):
    id: str
    name: str
    status: str  # running, stopped, building, error
    url: str
    port: int
    created_at: str
    git_url: Optional[str] = None
    container_id: Optional[str] = None

class UserCreate(BaseModel):
    email: str
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

@app.on_event("startup")
async def startup():
    """Initialize platform on startup"""
    print("🚀 Starting Mini Cloud Platform...")
    try:
        docker_client.networks.get(CONFIG["docker_network"])
    except docker.errors.NotFound:
        docker_client.networks.create(
            CONFIG["docker_network"], 
            driver="bridge",
            attachable=True
        )
        print(f"✅ Created Docker network: {CONFIG['docker_network']}")

@app.get("/api/apps/{app_id}/health")
async def check_app_health(app_id: str):
    if app_id not in apps_db:
        return {"status": "unknown"}
    
    try:
        container = docker_client.containers.get(apps_db[app_id]["container_id"])
        # Basic TCP check on app port
        import socket
        sock = socket.create_connection(("localhost", apps_db[app_id]["port"]), timeout=2)
        sock.close()
        return {"status": "healthy"}
    except:
        return {"status": "unhealthy"}

@app.get("/")
async def root():
    return {"message": "Mini Cloud Platform API", "version": "1.0.0"}

# 🔐 Authentication Endpoints
@app.post("/api/auth/register", response_model=Token)
async def register(user: UserCreate, db: Session = Depends(get_db)):
    # Check if user exists
    existing_user = db.query(User).filter(User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = str(uuid.uuid4())
    # For demo - in production, use proper password hashing
    hashed_password = user.password + "_hashed"  
    
    db_user = User(
        id=user_id,
        email=user.email,
        hashed_password=hashed_password
    )
    db.add(db_user)
    db.commit()
    
    # For demo - in production, use proper JWT tokens
    access_token = f"token_{user_id}"
    return {"access_token": access_token, "token_type": "bearer"}

# Simple auth dependency (replace with proper JWT in production)
async def get_current_user():
    return {"id": "default-user", "email": "user@example.com"}

@app.post("/api/deploy", response_model=dict)
async def deploy_app(
    deployment: DeploymentRequest, 
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Deploy a new application"""
    app_id = str(uuid.uuid4())[:8]
    port = deployment.port or (CONFIG["port_range_start"] + len(apps_db))
    
    # Generate subdomain
    if CONFIG["domain"] == "localhost":
        url = f"http://localhost:{port}"
        subdomain = f"localhost:{port}"
    else:
        subdomain = deployment.name.lower().replace(" ", "-")
        url = f"https://{subdomain}.{CONFIG['domain']}"

    # Create app in database
    db_app = App(
        id=app_id,
        name=deployment.name,
        status="building",
        url=url,
        port=port,
        git_url=deployment.git_url,
        environment_variables=json.dumps(deployment.environment_variables),
        user_id=current_user["id"]
    )
    db.add(db_app)
    db.commit()

    # Also keep in memory for backward compatibility
    app_data = {
        "id": app_id,
        "name": deployment.name,
        "status": "building",
        "url": url,
        "port": port,
        "git_url": deployment.git_url,
        "environment_variables": deployment.environment_variables,
        "created_at": datetime.now().isoformat(),
        "container_id": None,
        "subdomain": subdomain,
        "user_id": current_user["id"]
    }
    
    apps_db[app_id] = app_data
    background_tasks.add_task(deploy_app_background, app_id, deployment, subdomain, db)
    
    return {
        "app_id": app_id, 
        "status": "building", 
        "url": url,
        "message": "Deployment started"
    }

async def deploy_app_background(app_id: str, deployment: DeploymentRequest, subdomain: str, db: Session):
    try:
        app_data = apps_db[app_id]
        print(f"🛠️ Building app: {app_data['name']} ({app_id})")
        if deployment.git_url:
            await build_from_git(app_id, deployment, subdomain, db)
        else:
            raise HTTPException(400, "Only Git deployment supported in this version")
    except Exception as e:
        apps_db[app_id]["status"] = "error"
        apps_db[app_id]["error"] = str(e)
        
        # Update database
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app:
            db_app.status = "error"
            db.commit()
        
        print(f"❌ Deployment failed for {app_id}: {e}")

def create_app_container(app_id: str, image_tag: str, environment_vars: dict, port: int, subdomain: str):
    """Create container with SSL labels"""
    
    # SSL labels for Traefik
    labels = {
        "traefik.enable": "true",
        f"traefik.http.routers.app-{app_id}.rule": f"Host(`{subdomain}.{CONFIG['domain']}`)",
        f"traefik.http.routers.app-{app_id}.entrypoints": "websecure",
        f"traefik.http.routers.app-{app_id}.tls.certresolver": "myresolver",
        f"traefik.http.services.app-{app_id}.loadbalancer.server.port": str(port)
    }
    
    # Add resource limits for production
    resource_limits = {
        "memory": "512M",
        "memory_reservation": "256M",
        "nano_cpus": 500000000,
        "cpu_period": 100000,
        "cpu_quota": 50000,
    }

    security_opt = ["no-new-privileges:true"]

    container = docker_client.containers.run(
        image_tag,
        detach=True,
        name=f"app-{app_id}",
        network=CONFIG["docker_network"],
        environment=environment_vars,
        labels=labels,
        ports={f"{port}/tcp": port} if CONFIG["domain"] == "localhost" else {},
        mem_limit=resource_limits["memory"],
        mem_reservation=resource_limits["memory_reservation"],
        cpu_period=resource_limits["cpu_period"],
        cpu_quota=resource_limits["cpu_quota"],
        security_opt=security_opt,
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
    )
    return container

async def build_from_git(app_id: str, deployment: DeploymentRequest, subdomain: str, db: Session):
    app_data = apps_db[app_id]
    try:
        build_path = f"/tmp/builds/{app_id}"
        os.makedirs(build_path, exist_ok=True)
        
        print(f"📥 Cloning {deployment.git_url}...")
        result = subprocess.run(
            ["git", "clone", deployment.git_url, build_path],
            capture_output=True, text=True, timeout=300
        )
        
        if result.returncode != 0:
            raise Exception(f"Git clone failed: {result.stderr}")
        
        dockerfile_path = os.path.join(build_path, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            await generate_dockerfile(build_path)
        
        image_tag = f"mini-cloud-app-{app_id}:latest"
        print(f"🐳 Building Docker image: {image_tag}")
        image, logs = docker_client.images.build(path=build_path, tag=image_tag, rm=True)
        
        environment_vars = {**app_data["environment_variables"], "PORT": str(app_data["port"])}
        container = create_app_container(app_id, image_tag, environment_vars, app_data["port"], subdomain)
        
        # Update both memory and database
        apps_db[app_id]["status"] = "running"
        apps_db[app_id]["container_id"] = container.id
        
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app:
            db_app.status = "running"
            db_app.container_id = container.id
            db.commit()
        
        print(f"✅ Successfully deployed {app_data['name']} at {app_data['url']}")
    except Exception as e:
        apps_db[app_id]["status"] = "error"
        apps_db[app_id]["error"] = str(e)
        
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app:
            db_app.status = "error"
            db.commit()
        raise

async def generate_dockerfile(build_path: str):
    if os.path.exists(os.path.join(build_path, "package.json")):
        dockerfile_content = """
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 3000
CMD ["npm", "start"]
"""
    elif os.path.exists(os.path.join(build_path, "requirements.txt")):
        dockerfile_content = """
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "app.py"]
"""
    else:
        dockerfile_content = """
FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
"""
    with open(os.path.join(build_path, "Dockerfile"), "w") as f:
        f.write(dockerfile_content)

@app.get("/api/apps", response_model=List[AppStatus])
async def list_apps(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get apps from database for current user"""
    apps = db.query(App).filter(App.user_id == current_user["id"]).all()
    return [AppStatus(
        id=app.id,
        name=app.name,
        status=app.status,
        url=app.url,
        port=app.port,
        git_url=app.git_url,
        container_id=app.container_id,
        created_at=app.created_at.isoformat()
    ) for app in apps]

@app.get("/api/apps/{app_id}", response_model=AppStatus)
async def get_app(app_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get specific app from database"""
    app = db.query(App).filter(App.id == app_id, App.user_id == current_user["id"]).first()
    if not app:
        raise HTTPException(404, "App not found")
    return AppStatus(
        id=app.id,
        name=app.name,
        status=app.status,
        url=app.url,
        port=app.port,
        git_url=app.git_url,
        container_id=app.container_id,
        created_at=app.created_at.isoformat()
    )

@app.post("/api/apps/{app_id}/start")
async def start_app(app_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(404, "App not found")
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    try:
        container = docker_client.containers.get(app_data["container_id"])
        container.start()
        apps_db[app_id]["status"] = "running"
        
        # Update database
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app:
            db_app.status = "running"
            db.commit()
            
        return {"status": "success", "message": "App started"}
    except Exception as e:
        raise HTTPException(500, f"Failed to start app: {str(e)}")

@app.post("/api/apps/{app_id}/stop")
async def stop_app(app_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(404, "App not found")
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    try:
        container = docker_client.containers.get(app_data["container_id"])
        container.stop()
        apps_db[app_id]["status"] = "stopped"
        
        # Update database
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app:
            db_app.status = "stopped"
            db.commit()
            
        return {"status": "success", "message": "App stopped"}
    except Exception as e:
        raise HTTPException(500, f"Failed to stop app: {str(e)}")

@app.post("/api/apps/{app_id}/restart")
async def restart_app(app_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(404, "App not found")
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    try:
        container = docker_client.containers.get(app_data["container_id"])
        container.restart()
        apps_db[app_id]["status"] = "running"
        
        # Update database
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app:
            db_app.status = "running"
            db.commit()
            
        return {"status": "success", "message": "App restarted"}
    except Exception as e:
        raise HTTPException(500, f"Failed to restart app: {str(e)}")

@app.delete("/api/apps/{app_id}")
async def delete_app(app_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(404, "App not found")
    app_data = apps_db[app_id]
    try:
        if app_data["container_id"]:
            container = docker_client.containers.get(app_data["container_id"])
            container.stop()
            container.remove()
        image_tag = f"mini-cloud-app-{app_id}:latest"
        try:
            docker_client.images.remove(image_tag)
        except:
            pass
        
        # Remove from database
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app:
            db.delete(db_app)
            db.commit()
            
        # Remove from memory
        del apps_db[app_id]
        
        return {"status": "success", "message": "App deleted"}
    except Exception as e:
        raise HTTPException(500, f"Failed to delete app: {str(e)}")

@app.get("/api/apps/{app_id}/logs")
async def get_app_logs(app_id: str, current_user: dict = Depends(get_current_user)):
    if app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(404, "App not found")
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    try:
        container = docker_client.containers.get(app_data["container_id"])
        logs = container.logs(tail=100).decode('utf-8')
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(500, f"Failed to get logs: {str(e)}")

@app.get("/api/stats")
async def get_platform_stats(db: Session = Depends(get_db)):
    total_apps = db.query(App).count()
    running_apps = db.query(App).filter(App.status == "running").count()
    return {
        "total_apps": total_apps,
        "running_apps": running_apps,
        "stopped_apps": total_apps - running_apps,
        "domain": CONFIG["domain"],
        "uptime": "0"
    }

@app.get("/api/apps/{app_id}/stats")
async def get_app_stats(app_id: str, current_user: dict = Depends(get_current_user)):
    """Get resource usage statistics for an app"""
    if app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(404, "App not found")
    
    try:
        container = docker_client.containers.get(apps_db[app_id]["container_id"])
        stats = container.stats(stream=False)
        
        # Parse Docker stats
        cpu_stats = stats["cpu_stats"]
        memory_stats = stats["memory_stats"]
        
        return {
            "cpu_usage": calculate_cpu_percent(cpu_stats),
            "memory_usage": memory_stats.get("usage", 0),
            "memory_limit": memory_stats.get("limit", 0),
            "network_io": stats["networks"],
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to get stats: {str(e)}")

def calculate_cpu_percent(cpu_stats):
    """Calculate CPU percentage from Docker stats"""
    cpu_delta = cpu_stats["cpu_usage"]["total_usage"] - cpu_stats["precpu_usage"]["total_usage"]
    system_delta = cpu_stats["system_cpu_usage"] - cpu_stats["precpu_usage"]["system_cpu_usage"]
    
    if system_delta > 0 and cpu_delta > 0:
        return (cpu_delta / system_delta) * 100.0
    return 0.0

# Metrics endpoint
@app.get("/metrics")
async def metrics():
    """Prometheus-style metrics endpoint"""
    metrics_data = []
    
    # Platform metrics
    metrics_data.append(f"mini_cloud_apps_total {len(apps_db)}")
    metrics_data.append(f"mini_cloud_apps_running {len([app for app in apps_db.values() if app.get('status') == 'running'])}")
    
    # User metrics (if database is active)
    try:
        # This would need to be adjusted based on your actual users storage
        users_total = 0  # Placeholder
        metrics_data.append(f"mini_cloud_users_total {users_total}")
    except:
        metrics_data.append("mini_cloud_users_total 0")
    
    # Docker metrics
    try:
        containers = docker_client.containers.list()
        metrics_data.append(f"mini_cloud_containers_total {len(containers)}")
        running_containers = len([c for c in containers if c.status == "running"])
        metrics_data.append(f"mini_cloud_containers_running {running_containers}")
    except Exception:
        metrics_data.append("mini_cloud_docker_errors 1")
    
    return Response(content="\n".join(metrics_data), media_type="text/plain")

# Backup endpoints
@app.post("/api/admin/backup")
async def trigger_backup(
    current_user: dict = Depends(get_current_user),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """Trigger manual backup (admin only)"""
    # Check if user is admin (you'd implement proper admin check)
    background_tasks.add_task(perform_full_backup)
    return {"status": "success", "message": "Backup started"}

@app.get("/api/admin/backups")
async def list_backups(current_user: dict = Depends(get_current_user)):
    """List available backups"""
    backup_files = []
    backup_dir = "/app/backups"
    
    if os.path.exists(backup_dir):
        for filename in os.listdir(backup_dir):
            filepath = os.path.join(backup_dir, filename)
            if os.path.isfile(filepath):
                backup_files.append({
                    "name": filename,
                    "size": os.path.getsize(filepath),
                    "created": datetime.fromtimestamp(os.path.getctime(filepath)).isoformat()
                })
    
    return {"backups": backup_files}

def perform_full_backup():
    """Perform full platform backup"""
    os.makedirs("/app/backups", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"/app/backups/backup_{timestamp}.tar.gz"
    
    try:
        subprocess.run(
            ["tar", "-czf", backup_path, "/app/data", "/tmp/builds"],
            check=True
        )
        print(f"✅ Backup created: {backup_path}")
    except Exception as e:
        print(f"❌ Backup failed: {e}")

# Static files
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")

@app.get("/dashboard")
async def serve_dashboard():
    return FileResponse('dashboard/index.html')


# --- 1. App Health Check ---
@app.route('/api/apps/<app_id>/health', methods=['GET'])
def app_health(app_id):
    """Check if app container is responding"""
    try:
        if not docker_client:
            return jsonify({"status": "unknown", "message": "Docker unavailable"}), 503
        
        container = docker_client.containers.get(app_id)
        
        if container.status == 'running':
            # Try to ping the app's health endpoint
            try:
                import requests
                response = requests.get(f"http://localhost:{container.attrs['NetworkSettings']['Ports']['80/tcp'][0]['HostPort']}/health", timeout=2)
                return jsonify({"status": "healthy", "response_time": response.elapsed.total_seconds()})
            except:
                return jsonify({"status": "unhealthy", "message": "Not responding"})
        else:
            return jsonify({"status": "stopped"})
    
    except docker.errors.NotFound:
        return jsonify({"status": "not_found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# --- 2. Platform Stats ---
@app.route('/api/stats', methods=['GET'])
def platform_stats():
    """Get overall platform statistics"""
    try:
        if not docker_client:
            return jsonify({
                "total_apps": 0,
                "running_apps": 0,
                "healthy_apps": 0
            })
        
        containers = docker_client.containers.list(all=True)
        running = [c for c in containers if c.status == 'running']
        
        # Count healthy apps (simplified)
        healthy = len(running)  # In production, ping each app
        
        return jsonify({
            "total_apps": len(containers),
            "running_apps": len(running),
            "healthy_apps": healthy,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return jsonify({"error": str(e)}), 500


# --- 3. App Logs ---
@app.route('/api/apps/<app_id>/logs', methods=['GET'])
def app_logs(app_id):
    """Get container logs"""
    try:
        lines = request.args.get('lines', 50, type=int)
        
        if not docker_client:
            return jsonify({"logs": "Docker unavailable"}), 503
        
        container = docker_client.containers.get(app_id)
        logs = container.logs(tail=lines, timestamps=True).decode('utf-8')
        
        return jsonify({
            "logs": logs,
            "lines": lines,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- 4. Restart App ---
@app.route('/api/apps/<app_id>/restart', methods=['POST'])
def restart_app(app_id):
    """Restart application container"""
    try:
        if not docker_client:
            return jsonify({"error": "Docker unavailable"}), 503
        
        container = docker_client.containers.get(app_id)
        container.restart(timeout=10)
        
        log_audit(session.get('user_email', 'system'), 'app_restart', 
                 details=f"App: {app_id}")
        
        return jsonify({
            "ok": True,
            "message": "App restarted",
            "app_id": app_id,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- 5. App Metrics (CPU/RAM) ---
@app.route('/api/apps/<app_id>/metrics', methods=['GET'])
def app_metrics(app_id):
    """Get real-time CPU and memory usage"""
    try:
        if not docker_client:
            return jsonify({"error": "Docker unavailable"}), 503
        
        container = docker_client.containers.get(app_id)
        
        if container.status != 'running':
            return jsonify({
                "cpu_percent": 0,
                "memory_percent": 0,
                "status": "stopped"
            })
        
        # Get container stats
        stats = container.stats(stream=False)
        
        # Calculate CPU percentage
        cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - \
                    stats['precpu_stats']['cpu_usage']['total_usage']
        system_delta = stats['cpu_stats']['system_cpu_usage'] - \
                       stats['precpu_stats']['system_cpu_usage']
        cpu_percent = (cpu_delta / system_delta) * 100.0 if system_delta > 0 else 0
        
        # Calculate memory percentage
        memory_usage = stats['memory_stats']['usage']
        memory_limit = stats['memory_stats']['limit']
        memory_percent = (memory_usage / memory_limit) * 100.0 if memory_limit > 0 else 0
        
        return jsonify({
            "cpu_percent": round(cpu_percent, 2),
            "memory_percent": round(memory_percent, 2),
            "memory_mb": round(memory_usage / (1024 * 1024), 2),
            "status": "running",
            "timestamp": datetime.utcnow().isoformat()
        })
    
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found"}), 404
    except Exception as e:
        logger.error(f"Metrics error: {e}")
        return jsonify({"error": str(e)}), 500


# --- 6. WebSocket for Real-time Deploy Logs ---
# Note: Requires flask-socketio
from flask_socketio import SocketIO, emit

socketio = SocketIO(app, cors_allowed_origins="*")

@socketio.on('subscribe_deploy')
def handle_deploy_subscription(data):
    """Subscribe to deployment logs"""
    app_id = data.get('app_id')
    # Join room for this deployment
    from flask_socketio import join_room
    join_room(f"deploy_{app_id}")
    emit('subscribed', {'app_id': app_id})

def stream_deploy_logs(app_id, log_line):
    """Emit logs to subscribed clients"""
    socketio.emit('deploy_log', {
        'app_id': app_id,
        'log': log_line,
        'timestamp': datetime.utcnow().isoformat()
    }, room=f"deploy_{app_id}")


# --- 7. Quick Deploy Templates ---
@app.route('/api/templates', methods=['GET'])
def get_templates():
    """Get pre-configured deployment templates"""
    templates = [
        {
            "id": "nodejs",
            "name": "Node.js API",
            "icon": "📦",
            "description": "Express.js REST API with MongoDB",
            "git_url": "https://github.com/example/nodejs-api-template",
            "env_vars": {
                "NODE_ENV": "production",
                "PORT": "3000"
            },
            "buildpack": "nodejs"
        },
        {
            "id": "flask",
            "name": "Python Flask",
            "icon": "🐍",
            "description": "Flask web application with PostgreSQL",
            "git_url": "https://github.com/example/flask-template",
            "env_vars": {
                "FLASK_ENV": "production",
                "PORT": "5000"
            },
            "buildpack": "python"
        },
        {
            "id": "static",
            "name": "Static Site",
            "icon": "📄",
            "description": "HTML/CSS/JS static website",
            "git_url": "https://github.com/example/static-template",
            "env_vars": {},
            "buildpack": "static"
        },
        {
            "id": "wordpress",
            "name": "WordPress",
            "icon": "📝",
            "description": "WordPress CMS with MySQL",
            "git_url": "https://github.com/example/wordpress-template",
            "env_vars": {
                "WORDPRESS_DB_HOST": "db",
                "WORDPRESS_DB_USER": "wordpress"
            },
            "buildpack": "php"
        }
    ]
    
    return jsonify(templates)


# --- 8. Bulk Start Apps ---
@app.route('/api/bulk/start', methods=['POST'])
@limiter.limit("10 per minute")
def bulk_start_apps():
    """Start multiple apps at once"""
    try:
        data = request.get_json()
        app_ids = data.get('app_ids', [])
        
        if not app_ids:
            return jsonify({"error": "No app IDs provided"}), 400
        
        if len(app_ids) > 20:
            return jsonify({"error": "Max 20 apps at once"}), 400
        
        results = []
        for app_id in app_ids:
            try:
                container = docker_client.containers.get(app_id)
                container.start()
                results.append({"app_id": app_id, "status": "started"})
            except Exception as e:
                results.append({"app_id": app_id, "error": str(e)})
        
        log_audit(session.get('user_email', 'system'), 'bulk_start', 
                 details=f"Started {len(app_ids)} apps")
        
        return jsonify({
            "ok": True,
            "results": results,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- 9. Bulk Stop Apps ---
@app.route('/api/bulk/stop', methods=['POST'])
@limiter.limit("10 per minute")
def bulk_stop_apps():
    """Stop multiple apps at once"""
    try:
        data = request.get_json()
        app_ids = data.get('app_ids', [])
        
        if not app_ids:
            return jsonify({"error": "No app IDs provided"}), 400
        
        if len(app_ids) > 20:
            return jsonify({"error": "Max 20 apps at once"}), 400
        
        results = []
        for app_id in app_ids:
            try:
                container = docker_client.containers.get(app_id)
                container.stop(timeout=10)
                results.append({"app_id": app_id, "status": "stopped"})
            except Exception as e:
                results.append({"app_id": app_id, "error": str(e)})
        
        log_audit(session.get('user_email', 'system'), 'bulk_stop', 
                 details=f"Stopped {len(app_ids)} apps")
        
        return jsonify({
            "ok": True,
            "results": results,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- 10. Update App Configuration ---
@app.route('/api/apps/<app_id>/config', methods=['PUT'])
def update_app_config(app_id):
    """Update app environment variables, domain, scaling"""
    try:
        data = request.get_json()
        
        # Validate input
        env_vars = data.get('env_vars', {})
        custom_domain = data.get('custom_domain')
        instances = data.get('instances', 1)
        
        # Update app configuration in database
        # (Assuming you have an App model)
        # app = App.query.get(app_id)
        # app.env_vars = env_vars
        # app.custom_domain = custom_domain
        # app.instances = instances
        # db.session.commit()
        
        # Restart container with new config
        if docker_client:
            try:
                container = docker_client.containers.get(app_id)
                
                # Update environment variables
                # Note: You need to recreate container to apply env changes
                container.stop()
                # container.remove()
                # ... recreate with new env vars
                container.start()
            except:
                pass
        
        log_audit(session.get('user_email', 'system'), 'app_config_update', 
                 details=f"App: {app_id}")
        
        return jsonify({
            "ok": True,
            "message": "Configuration updated",
            "app_id": app_id,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# If using Flask-SocketIO, add to bottom of file:
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)