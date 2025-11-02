# main.py
import os
import uuid
import json
import asyncio
import subprocess
from typing import Dict, List, Optional
from datetime import datetime

from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import docker
from docker.models.containers import Container

# Configuration
CONFIG = {
    "domain": os.getenv("PLATFORM_DOMAIN", "localhost"),
    "docker_network": "mini-cloud-network",
    "data_volume": "mini-cloud-data",
    "port_range_start": 10000
}

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

# In-memory storage (replace with database in production)
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

@app.on_event("startup")
async def startup():
    """Initialize platform on startup"""
    print("🚀 Starting Mini Cloud Platform...")
    
    # Ensure Docker network exists
    try:
        docker_client.networks.get(CONFIG["docker_network"])
    except docker.errors.NotFound:
        docker_client.networks.create(
            CONFIG["docker_network"], 
            driver="bridge",
            attachable=True
        )
        print(f"✅ Created Docker network: {CONFIG['docker_network']}")

@app.get("/")
async def root():
    return {"message": "Mini Cloud Platform API", "version": "1.0.0"}

@app.post("/api/deploy", response_model=dict)
async def deploy_app(
    deployment: DeploymentRequest, 
    background_tasks: BackgroundTasks
):
    """Deploy a new application"""
    app_id = str(uuid.uuid4())[:8]
    port = deployment.port or (CONFIG["port_range_start"] + len(apps_db))
    
    # Generate subdomain
    if CONFIG["domain"] == "localhost":
        url = f"http://localhost:{port}"
    else:
        subdomain = deployment.name.lower().replace(" ", "-")
        url = f"https://{subdomain}.{CONFIG['domain']}"

    # Create app record
    app_data = {
        "id": app_id,
        "name": deployment.name,
        "status": "building",
        "url": url,
        "port": port,
        "git_url": deployment.git_url,
        "environment_variables": deployment.environment_variables,
        "created_at": datetime.now().isoformat(),
        "container_id": None
    }
    
    apps_db[app_id] = app_data
    
    # Start deployment in background
    background_tasks.add_task(deploy_app_background, app_id, deployment)
    
    return {
        "app_id": app_id, 
        "status": "building", 
        "url": url,
        "message": "Deployment started"
    }

async def deploy_app_background(app_id: str, deployment: DeploymentRequest):
    """Background task to handle app deployment"""
    try:
        app_data = apps_db[app_id]
        print(f"🛠️ Building app: {app_data['name']} ({app_id})")
        
        # For Git deployment
        if deployment.git_url:
            await build_from_git(app_id, deployment)
        else:
            raise HTTPException(400, "Only Git deployment supported in this version")
            
    except Exception as e:
        apps_db[app_id]["status"] = "error"
        apps_db[app_id]["error"] = str(e)
        print(f"❌ Deployment failed for {app_id}: {e}")

async def build_from_git(app_id: str, deployment: DeploymentRequest):
    """Build and deploy from Git repository"""
    app_data = apps_db[app_id]
    
    try:
        # Create build directory
        build_path = f"/tmp/builds/{app_id}"
        os.makedirs(build_path, exist_ok=True)
        
        # Clone repository
        print(f"📥 Cloning {deployment.git_url}...")
        result = subprocess.run(
            ["git", "clone", deployment.git_url, build_path],
            capture_output=True, text=True, timeout=300
        )
        
        if result.returncode != 0:
            raise Exception(f"Git clone failed: {result.stderr}")
        
        # Check for Dockerfile
        dockerfile_path = os.path.join(build_path, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            # Generate simple Dockerfile for Node.js/Python apps
            await generate_dockerfile(build_path)
        
        # Build Docker image
        image_tag = f"mini-cloud-app-{app_id}:latest"
        print(f"🐳 Building Docker image: {image_tag}")
        
        image, logs = docker_client.images.build(
            path=build_path,
            tag=image_tag,
            rm=True
        )
        
        # Run container
        container_name = f"app-{app_id}"
        environment_vars = {
            **app_data["environment_variables"],
            "PORT": str(app_data["port"])
        }
        
        container: Container = docker_client.containers.run(
            image_tag,
            detach=True,
            name=container_name,
            network=CONFIG["docker_network"],
            ports={f"{app_data['port']}/tcp": app_data['port']},
            environment=environment_vars,
            labels={
                "mini-cloud.app-id": app_id,
                "mini-cloud.app-name": app_data["name"]
            }
        )
        
        # Update app status
        apps_db[app_id]["status"] = "running"
        apps_db[app_id]["container_id"] = container.id
        
        print(f"✅ Successfully deployed {app_data['name']} at {app_data['url']}")
        
    except Exception as e:
        apps_db[app_id]["status"] = "error"
        apps_db[app_id]["error"] = str(e)
        raise

async def generate_dockerfile(build_path: str):
    """Generate a Dockerfile for common app types"""
    # Check for package.json (Node.js) or requirements.txt (Python)
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
async def list_apps():
    """Get all deployed applications"""
    return [AppStatus(**app) for app in apps_db.values()]

@app.get("/api/apps/{app_id}", response_model=AppStatus)
async def get_app(app_id: str):
    """Get specific app details"""
    if app_id not in apps_db:
        raise HTTPException(404, "App not found")
    return AppStatus(**apps_db[app_id])

@app.post("/api/apps/{app_id}/start")
async def start_app(app_id: str):
    """Start a stopped application"""
    if app_id not in apps_db:
        raise HTTPException(404, "App not found")
    
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    
    try:
        container = docker_client.containers.get(app_data["container_id"])
        container.start()
        apps_db[app_id]["status"] = "running"
        return {"status": "success", "message": "App started"}
    except Exception as e:
        raise HTTPException(500, f"Failed to start app: {str(e)}")

@app.post("/api/apps/{app_id}/stop")
async def stop_app(app_id: str):
    """Stop a running application"""
    if app_id not in apps_db:
        raise HTTPException(404, "App not found")
    
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    
    try:
        container = docker_client.containers.get(app_data["container_id"])
        container.stop()
        apps_db[app_id]["status"] = "stopped"
        return {"status": "success", "message": "App stopped"}
    except Exception as e:
        raise HTTPException(500, f"Failed to stop app: {str(e)}")

@app.post("/api/apps/{app_id}/restart")
async def restart_app(app_id: str):
    """Restart an application"""
    if app_id not in apps_db:
        raise HTTPException(404, "App not found")
    
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    
    try:
        container = docker_client.containers.get(app_data["container_id"])
        container.restart()
        apps_db[app_id]["status"] = "running"
        return {"status": "success", "message": "App restarted"}
    except Exception as e:
        raise HTTPException(500, f"Failed to restart app: {str(e)}")

@app.delete("/api/apps/{app_id}")
async def delete_app(app_id: str):
    """Delete an application"""
    if app_id not in apps_db:
        raise HTTPException(404, "App not found")
    
    app_data = apps_db[app_id]
    
    try:
        # Stop and remove container
        if app_data["container_id"]:
            container = docker_client.containers.get(app_data["container_id"])
            container.stop()
            container.remove()
        
        # Remove image
        image_tag = f"mini-cloud-app-{app_id}:latest"
        try:
            docker_client.images.remove(image_tag)
        except:
            pass  # Image might not exist
        
        # Remove from database
        del apps_db[app_id]
        
        return {"status": "success", "message": "App deleted"}
    except Exception as e:
        raise HTTPException(500, f"Failed to delete app: {str(e)}")

@app.get("/api/apps/{app_id}/logs")
async def get_app_logs(app_id: str, lines: int = 100):
    """Get application logs"""
    if app_id not in apps_db:
        raise HTTPException(404, "App not found")
    
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400, "App not deployed properly")
    
    try:
        container = docker_client.containers.get(app_data["container_id"])
        logs = container.logs(tail=lines).decode('utf-8')
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(500, f"Failed to get logs: {str(e)}")

@app.get("/api/stats")
async def get_platform_stats():
    """Get platform statistics"""
    total_apps = len(apps_db)
    running_apps = len([app for app in apps_db.values() if app["status"] == "running"])
    
    return {
        "total_apps": total_apps,
        "running_apps": running_apps,
        "stopped_apps": total_apps - running_apps,
        "domain": CONFIG["domain"],
        "uptime": "0"  # You'd track this in production
    }

# Serve static files for dashboard
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")

@app.get("/dashboard")
async def serve_dashboard():
    return FileResponse('dashboard/index.html')

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)