from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import json
import subprocess
import docker
import uuid
import asyncio
from datetime import datetime

# Pydantic models for request/response
class DeploymentRequest(BaseModel):
    git_url: Optional[str] = None
    uploaded_code_path: Optional[str] = None
    environment_variables: Optional[dict] = {}

class AppStatus(BaseModel):
    app_id: str
    name: str
    status: str  # running, stopped, building, error
    url: str
    created_at: str

# Initialize Docker client and FastAPI app
docker_client = docker.from_env()
app = FastAPI(title="Mini Cloud Platform")

# Mock database for development
apps_db = {}

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/deploy", response_model=dict)
async def deploy_app(deployment: DeploymentRequest, background_tasks: BackgroundTasks):
    """Deploy a new application from Git or uploaded code"""
    app_id = str(uuid.uuid4())[:8]
    app_name = f"app-{app_id}"
    subdomain = f"{app_id}.platform.local"  # Replace with your domain

    # Store app metadata
    apps_db[app_id] = {
        "id": app_id,
        "name": app_name,
        "status": "building",
        "url": f"https://{subdomain}",
        "created_at": datetime.now().isoformat(),
        "git_url": deployment.git_url
    }

    # Start build process in background
    background_tasks.add_task(build_and_deploy_app, app_id, deployment)
    
    return {"app_id": app_id, "status": "building", "url": f"https://{subdomain}"}

def build_and_deploy_app(app_id: str, deployment: DeploymentRequest):
    """Background task to build and deploy the application container"""
    try:
        app_meta = apps_db[app_id]
        app_meta["status"] = "building"
        
        # For Git-based deployment
        if deployment.git_url:
            image_tag = f"user-app:{app_id}"
            
            # Build Docker image from Git repository
            docker_client.images.build(
                path=deployment.git_url,  # This would need to be a local path in a real scenario
                tag=image_tag,
                rm=True
            )
            
            # Run the container
            container = docker_client.containers.run(
                image_tag,
                detach=True,
                name=f"app-{app_id}",
                network="cloud-platform",  # Use a custom Docker network
                environment=deployment.environment_variables,
                labels={
                    "traefik.enable": "true",
                    f"traefik.http.routers.app-{app_id}.rule": f"Host(`{app_meta['url']}`)",
                    f"traefik.http.routers.app-{app_id}.entrypoints": "web"
                }
            )
            
            app_meta["container_id"] = container.id
            app_meta["status"] = "running"
            
    except Exception as e:
        app_meta["status"] = "error"
        app_meta["error"] = str(e)

@app.post("/api/apps/{app_id}/start")
async def start_app(app_id: str):
    """Start a stopped application"""
    if app_id not in apps_db:
        raise HTTPException(status_code=404, detail="App not found")
    
    try:
        container = docker_client.containers.get(f"app-{app_id}")
        container.start()
        apps_db[app_id]["status"] = "running"
        return {"status": "success", "message": "App started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/apps/{app_id}/stop")
async def stop_app(app_id: str):
    """Stop a running application"""
    if app_id not in apps_db:
        raise HTTPException(status_code=404, detail="App not found")
    
    try:
        container = docker_client.containers.get(f"app-{app_id}")
        container.stop()
        apps_db[app_id]["status"] = "stopped"
        return {"status": "success", "message": "App stopped"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/apps/{app_id}/logs")
async def get_app_logs(app_id: str):
    """Get application logs"""
    if app_id not in apps_db:
        raise HTTPException(status_code=404, detail="App not found")
    
    try:
        container = docker_client.containers.get(f"app-{app_id}")
        logs = container.logs().decode('utf-8')
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/apps", response_model=List[AppStatus])
async def list_apps():
    """List all deployed applications"""
    return [AppStatus(**app) for app in apps_db.values()]

# Serve a simple dashboard
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return """
    <html>
        <head>
            <title>Mini Cloud Platform Dashboard</title>
            <script src="https://unpkg.com/htmx.org@1.9.10"></script>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; }
                .app { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; }
                .running { border-left: 5px solid green; }
                .stopped { border-left: 5px solid red; }
                .building { border-left: 5px solid orange; }
            </style>
        </head>
        <body>
            <h1>Cloud Platform Dashboard</h1>
            
            <h2>Deploy New App</h2>
            <form hx-post="/api/deploy" hx-target="#result">
                <input type="text" name="git_url" placeholder="Git Repository URL" style="width: 300px;">
                <button type="submit">Deploy</button>
            </form>
            <div id="result"></div>
            
            <h2>Active Applications</h2>
            <div hx-get="/api/apps" hx-trigger="load every 5s" id="app-list">
                Loading...
            </div>
            
            <script>
                function startApp(appId) {
                    htmx.ajax('POST', `/api/apps/${appId}/start`, { target: `#app-${appId}` });
                }
                
                function stopApp(appId) {
                    htmx.ajax('POST', `/api/apps/${appId}/stop`, { target: `#app-${appId}` });
                }
            </script>
        </body>
    </html>
    