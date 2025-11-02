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
from fastapi.responses import FileResponse
from pydantic import BaseModel
import docker
from docker.models.containers import Container

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
        ports = {f"{port}/tcp": port} if CONFIG["domain"] == "localhost" else {}
        mem else {},
        mem_limit=resource_limits["_limit=resource_limits["memory"],
        mem_reservation=resource_limits["memory_reservation"],
        cpu_period=resource_limits["cpu_period"],
memory"],
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
        cpu_quota=resource_limits["cpu_quota"],
        security_opt=security_opt,
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
    )
    return container

async def build_from_git(app_id: str, deployment: DeploymentRequest, subdomain: str, db: Session):
    app_data = apps_db[app_id]
    try:
        build_path = f"/tmp/builds/{app_id}"
        os.makedirs(build_path        os.makedirs(build_path, exist_ok=True)
, exist_ok=True)
        
        print(f"📥 Cl        
        print(f"📥 Cloning {deployment.git_url}...oning {deployment.git_url}...")
        result = subprocess")
        result = subprocess.run(
            ["git", "clone.run(
            ["git", "clone", deployment.git_url, build_path],
", deployment.git_url, build_path            capture_output=True, text=True],
            capture_output=True, text=True, timeout=300
        )
, timeout=300
        )
        
        if result.returncode !=        
        if result.returncode != 0:
            raise Exception(f"Git clone failed: {result.st 0:
            raise Exception(f"Git clone failed: {result.stderr}")
        
        dockerfile_pathderr}")
        
        dockerfile_path = os.path.join(build_path, = os.path.join(build_path, "Dockerfile")
        if not "Dockerfile")
        if not os.path.exists(dockerfile_path os.path.exists(dockerfile_path):
):
            await generate_dockerfile            await generate_dockerfile(build_path)
        
        image_tag(build_path)
        
        image_tag = f"mini-cloud-app-{ = f"mini-cloud-app-{app_id}:latest"
        printapp_id}:latest"
       (f"🐳 Building Docker image print(f"🐳 Building Docker image: {image_tag}")
        image: {image_tag}")
        image, logs =, logs = docker_client.images.build(path docker_client.images.build(path=build=build_path, tag=_path, tag=image_tagimage_tag, rm=True)
        
       , rm=True)
        
        environment_v environment_vars = {**app_dataars = {**app_data["environment_variables"], "PORT": str["environment_variables"], "PORT": str(app_data["port"])}
       (app_data["port"])}
        container = create_app_container(app_id container = create_app_container(app_id,, image_tag, environment_vars, app_data["port"], subdomain)
        
        # Update both memory and database
        apps_db[app_id]["status"] = "running"
        apps_db[app_id]["container_id"] = image_tag, environment_vars, app_data["port"], subdomain)
        
        # Update both memory and database
        apps_db[app_id]["status"] = "running"
        apps_db[app_id]["container_id"] = container.id
        
        db_app container.id
        
        db_app = db = db.query(App).filter(App.id.query(App).filter(App.id == app_id).first()
        if db_app:
            db_app.status = == app_id).first()
        if db_app:
            db_app.status = "running"
            db_app "running"
            db_app.container_id.container_id = container.id
            db = container.id
            db.commit.commit()
        
        print(f"✅()
        
        print(f"✅ Successfully deployed {app_data Successfully deployed {app_data['name']} at {app_data['name']} at {app_data['url']}")
    except Exception as e:
['url']}")
    except Exception as e:
        apps_db[app_id        apps_db[app_id]["]["status"] = "error"
       status"] = "error"
        apps apps_db[app_id]["_db[app_id]["error"]error"] = str(e)
        
        = str(e)
        
        db_app db_app = db.query(App).filter = db.query(App).filter(App(App.id == app.id == app_id).first()
       _id).first()
        if db_app:
            if db_app:
            db_app.status db_app.status = "error"
            = "error"
            db db.commit()
            
        raise

.commit()
            
        raise

async def generateasync def generate_dockerfile(b_dockerfile(build_path:uild_path: str):
    if os str):
    if os.path.exists.path.exists(os.path.join(build(os.path.join(build_path, "_path, "package.json")):
package.json")):
        dockerfile_content        dockerfile_content = """
 = """
FROM node:FROM node:18-alpine
WORKDIR /18-alpine
WORKDIR /app
app
COPY package*.jsonCOPY package*.json ./
 ./
RUN npm install
COPY .RUN npm install
COPY . .
EX .
EXPOSE 3000
CMDPOSE 3000
CMD ["npm", "start ["npm", "start"]
"""
    elif os.path.exists(os.path"]
"""
    elif os.path.exists(os.path.join(build_path.join(build_path, "requirements.txt, "requirements.txt")):
       ")):
        dockerfile_content = """
 dockerfile_content = """
FROM pythonFROM python:3.11:3.11-slim
WORK-slim
WORKDIR /app
DIR /app
COPY requirements.txt .
COPY requirements.txt .
RUN pip install -RUN pip install -r requirements.txt
r requirements.txt
COPY . .
COPY . .
EXPOSE 800EXPOSE 80000
CMD ["python", "app.py"]
"""
    else:
        dockerfile_content = """
FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
"""
    with open
CMD ["python", "app.py"]
"""
    else:
        dockerfile_content = """
FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
"""
    with open(os.path(os.path.join(build_path, ".join(build_path, "Dockerfile"), "w") as f:
Dockerfile"), "w") as f:
               f.write(dockerfile_content)

 f.write(dockerfile_content)

@app.get("/@app.get("/apiapi/apps", response_model/apps", response_model=List[AppStatus])
async def list=List[AppStatus])
async def list_apps(
    current_apps(
    current_user: dict = Depends_user: dict = Depends(get_current_user),
(get_current_user),
    db: Session    db: Session = Depends(get = Depends(get_db)
):
   _db)
):
    """Get """Get apps from database for apps from database for current user"""
    current user"""
    apps = apps = db.query(App).filter db.query(App).filter(App.user_id == current_user["id"]).all(App.user_id == current_user["id"]).all()
    return()
    return [AppStatus(
        [AppStatus(
        id= id=app.id,
        name=app.id,
        name=app.nameapp.name,
        status=,
        status=app.status,
       app.status,
        url=app.url url=app.url,
        port,
        port=app.port=app.port,
        git,
        git_url=app.git_url,
_url=app.git_url,
        container        container_id=app.container_id,
_id=app.container_id,
        created_at=app.created_at.isoformat()
        created_at=app.created_at.isoformat()
    ) for app in    ) for app in apps]

@app.get apps]

@app.get("/api/apps("/api/apps/{app_id}",/{app_id}", response_model response_model=App=AppStatus)
async def get_appStatus)
async def get_app(app_id: str,(app_id: str, current_user: current_user: dict = Depends dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get(get_current_user), db: Session = Depends(get_db)):
    """Get specific app from database"""
    specific app from database"""
    app = db app = db.query(App).filter(App.query(App).filter(App.id == app_id.id == app_id, App.user, App.user_id == current_user["id"]).first()
   _id == current_user["id"]).first()
    if not if not app:
        raise HTTPException app:
        raise HTTPException(404(404, "App not, "App not found")
    found")
    return AppStatus(
        id=app.id,
        name= return AppStatus(
        id=app.id,
        name=app.nameapp.name,
        status=app.status,
        status=app.status,
,
        url=app.url,
        url=app.url,
               port=app. port=app.portport,
        git_url=app.git_url,
,
        git_url=app.git_url,
        container_id=app.container_id,
        container_id=app.container_id,
        created_at=app.created_at.is        created_at=app.created_at.isoformat()
    )

@app.postoformat()
    )

@app.post("/api/apps/{app_id}/("/api/apps/{app_id}/start")
async def start_app(appstart")
async def start_app(app_id: str, current_user: dict_id: str, current_user: dict = Depends(get_current_user = Depends(get_current_user), db), db: Session = Depends(get: Session = Depends(get_db)):
_db)):
    if app_id not in    if app_id not in apps_db or apps_db apps_db or apps_db[app_id[app_id].get("user_id")].get("user_id") != current_user["id"]:
        != current_user["id"]:
        raise raise HTTP HTTPException(404, "App not foundException(404, "App not found")
    app_data = apps_db[app_id]
    if not app")
    app_data = apps_db[app_id]
    if not app_data["container_id"]:
        raise HTTPException(400_data["container_id"]:
        raise HTTPException(400, ", "App not deployed properly")
    try:
        container = docker_clientApp not deployed properly")
    try:
        container = docker_client.containers.containers.get(app_data["container_id"])
.get(app_data["container_id"])
               container.start()
        apps_db container.start()
        apps_db[app_id]["status"] =[app_id]["status"] = "running"
        
        # Update database "running"
        
        # Update database
        db_app = db.query(App).filter(App.id == app_id
        db_app = db.query(App).filter(App.id == app_id).first()
        if db_app).first()
        if db_app:
            db_app.status = "running:
            db_app.status = "running"
            db.commit()
            
        return"
            db.commit()
            
        return {"status": "success", {"status": "success", "message "message": "App started"}
   ": "App started"}
    except Exception as e:
        raise HTTP except Exception as e:
        raise HTTPException(500, f"FailedException(500, f"Failed to start app: {str(e to start app: {str(e)}")

)}")

@app.post("/api/apps@app.post("/api/apps/{app_id}/stop")
async def stop_app(app_id: str,/{app_id}/stop")
async def stop_app(app_id: str, current_user: dict = Depends current_user: dict = Depends(get_current(get_current_user), db: Session =_user), db: Session = Depends Depends(get_db)):
    if(get_db)):
    if app_id app_id not in apps_db or not in apps_db or apps_db apps_db[app_id].get("[app_id].get("useruser_id") != current_user["id_id") != current_user["id"]:
"]:
        raise HTTPException(        raise HTTPException(404,404, "App not found")
 "App not found")
    app    app_data_data = apps_db[app_id = apps_db[app_id]
    if not app_data["]
    if not app_data["container_id"]:
        raisecontainer_id"]:
        raise HTTPException(400 HTTPException(400, "App not, "App not deployed properly")
    deployed properly")
    try:
        container try:
        container = docker_client = docker_client.containers.get.containers.get(app(app_data["container_id"])
       _data["container_id"])
        container.stop()
        container.stop()
        apps_db[app apps_db[app_id]["status"]_id]["status"] = "stopped = "stopped"
        
        #"
        
        # Update database
 Update database
        db_app = db        db_app = db.query(App.query(App).filter(App.id == app).filter(App.id == app_id)._id).first()
        iffirst()
        if db_app:
            db_app:
            db_app.status = db_app.status = "sto "stopped"
            db.commit()
pped"
            db.commit()
            
                   
        return {"status": return {"status": "success", "message": "App "success", "message": "App stopped"}
 stopped"}
    except Exception as e:
        raise    except Exception as e:
        raise HTTPException(500, f"Failed to stop app: {str(e)} HTTPException(500, f"Failed to stop app: {str(e)}")

@app")

@app.post("/api/apps/{app_id}/.post("/api/apps/{app_id}/restart")
async defrestart")
async def restart_app(app_id: str, current_user restart_app(app_id: str, current_user: dict =: dict = Depends(get_current Depends(get_current_user),_user), db: Session = Depends db: Session = Depends(get_db(get_db)):
    if app_id not)):
    if app_id not in apps in apps_db or apps_db_db or apps_db[app_id].[app_id].get("user_idget("user_id") != current_user") != current_user["id"]["id"]:
        raise HTTP:
        raise HTTPException(404Exception(404, "App, "App not found")
    not found")
    app_data = apps_db[app app_data = apps_db[app_id]
    if not app_data["container_id]
    if not app_data["container_id"]:
        raise HTTPException_id"]:
        raise HTTPException(400, "(400, "App not deployed properlyApp not deployed properly")
    try:
        container")
    try:
        container = docker_client.contain = docker_client.containers.get(appers.get(app_data["container_id_data["container_id"])
        container.rest"])
        container.restart()
        appsart()
        apps_db[_db[app_id]["status"]app_id]["status"] = "running = "running"
        
        #"
        
        # Update database
        db_app = db Update database
        db_app = db.query(App)..query(App).filter(App.id == appfilter(App.id == app_id)._id).first()
        iffirst()
        if db_app:
 db_app:
            db_app.status =            db_app.status = "running"
            "running"
            db.commit db.commit()
            
        return {"()
            
        return {"status": "successstatus": "success", "message":", "message": "App rest "App restarted"}
    exceptarted"}
    except Exception as e:
 Exception as e:
        raise HTTPException        raise HTTPException(500, f(500, f"Failed to"Failed to restart app: { restart app: {str(e)}")

@app.delete("/api/apps/{str(e)}")

@app.delete("/api/apps/{app_id}")
asyncapp_id}")
async def delete_app(app def delete_app(app_id: str,_id: str, current_user: dict = current_user: dict = Depends(get_current_user Depends(get_current_user), db: Session), db: Session = Depends(get = Depends(get_db)):
_db)):
    if app_id    if app_id not in apps_db or apps not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(404, "App404, "App not found")
    not found")
    app_data = apps app_data = apps_db[_db[app_id]
   app_id]
    try:
        if try:
        if app_data[" app_data["container_id"]:
           container_id"]:
            container = docker_client container = docker_client.containers.get.containers.get(app_data["container_id"])
            container.stop()
            container.remove()
       (app_data["container_id"])
            container.stop()
            container.remove()
        image_tag = f"mini-cloud image_tag = f"mini-cloud-app-{app_id}:latest"
-app-{app_id}:latest"
        try        try:
:
            docker_client.images.remove(image_tag)
        except            docker_client.images.remove(image_tag)
        except:
           :
            pass
        
        # pass
        
        # Remove from Remove from database
        db_app = db.query(App).filter database
        db_app = db.query(App).filter(App.id(App.id == app_id).first()
        if db == app_id).first()
        if db_app:
            db.delete(db_app)
            db_app:
            db.delete(db_app)
            db.commit()
            
        #.commit()
            
        # Remove from Remove from memory
        del apps_db memory
        del apps_db[[app_id]
        
        return {"app_id]
        
        return {"status": "success", "message":status": "success", "message": "App deleted"}
    "App deleted"}
    except except Exception as e:
        raise HTTPException(500, f" Exception as e:
        raise HTTPException(500, f"Failed to delete app: {str(e)}")

@app.get("/apiFailed to delete app: {str(e)}")

@app.get("/api/app/apps/{app_id}/logs")
async def gets/{app_id}/logs")
async def get_app_logs(app_id: str, current_user: dict = Depends(get_current_user)):
    if_app_logs(app_id: str, current_user: dict = Depends(get_current_user)):
    if app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTP app_id not in apps_db or apps_db[app_id].get("user_id") != current_user["id"]:
        raise HTTPException(Exception(404, "App not404, "App not found")
    found")
    app_data = apps app_data = apps_db_db[app_id]
    if not[app_id]
    if not app_data app_data["container_id"]:
       ["container_id"]:
        raise HTTP raise HTTPException(400, "Exception(400, "App not deployedApp not deployed properly")
    try properly")
    try:
        container:
        container = docker_client.contain = docker_client.containers.geters.get(app_data["container_id(app_data["container_id"])
       "])
        logs = container.logs logs = container.logs(tail=lines).decode('utf-8')
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(500, f(tail=lines).decode('utf-8')
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(500, f"Failed to get logs: {str"Failed to get logs: {str(e)}")

@app.get("/api/st(e)}")

@app.get("/api/stats")
async def get_ats")
async def get_platformplatform_stats(db: Session = Depends_stats(db: Session = Depends(get_db)):
    total_apps =(get_db)):
    total_apps = db.query(App).count()
    running db.query(App).count()
    running_apps = db.query(App_apps = db.query(App).filter(App.status == "running").).filter(App.status == "running").count()
    return {
        "totalcount()
    return {
        "total_apps": total_apps,
       _apps": total_apps,
        "running_apps": running "running_apps": running_apps,
        "stopped_apps": total_apps - running_apps,
       _apps,
        "stopped_apps": total_apps - running_apps,
        " "domain":domain": CON CONFIG["domain"],
        "uptime": "0FIG["domain"],
        "uptime": "0"
    }

app"
    }

app.mount("/dashboard", StaticFiles(directory="dashboard.mount("/dashboard", StaticFiles(directory="dashboard", html", html=True),=True), name="dashboard name="dashboard")

@app.get("/dashboard")
async def")

@app.get("/dashboard")
async def serve_dashboard():
    return FileResponse serve_dashboard():
    return FileResponse('dashboard/index.html')

@app('dashboard/index.html')

@app.get.get("/api/apps/{app_id("/api/apps/{app_id}/stats}/stats")
async def get_app_stats")
async def get_app_stats(app_id(app_id: str, current_user:: str, current_user: dict = dict = Depends(get_current Depends(get_current_user)):
_user)):
    if app_id    if app_id not not in apps_db or apps_db[app_id in apps_db or apps_db[app_id].].get("user_id") != currentget("user_id") != current_user["id"]:
        raise_user["id"]:
        raise HTTP HTTPException(404, "AppException(404, "App not found")
    try:
        container = docker_client.containers.get(app not found")
    try:
        container = docker_client.containers.get(apps_db[app_id]["s_db[app_id]["container_idcontainer_id"])
        stats = container.st"])
        stats = container.statsats(stream=False)
(stream=False)
        cpu_stats = stats["cpu        cpu_stats = stats["cpu_stats"]
_stats"]
        memory_stats = stats["memory_stats"]
               memory_stats = stats["memory_stats"]
        return {
            return {
            "cpu_usage": calculate_c "cpu_usage": calculate_cpupu_percent(cpu_stats),
            "memory__percent(cpu_stats),
            "memory_usageusage": memory_stats.get("usage": memory_stats.get("usage", 0),
            "memory_limit": memory_stats.get("limit", ", 0),
            "memory_limit": memory_stats.get("limit", 0),
            "network_0),
            "network_io": stats["networks"],
            "timestamp": datetime.now().isoformatio": stats["networks"],
            "timestamp": datetime.now().isoformat()
       ()
        }
    except Exception as e }
    except Exception as e:
       :
        raise HTTPException(500, raise HTTPException(500, f"Failed to get stats: f"Failed to get stats: { {str(e)}")

def calculatestr(e)}")

def calculate_cpu_cpu_percent_percent(cpu_stats):
    cpu_delta = cpu_stats(cpu_stats):
    cpu_delta = cpu_stats["["cpu_usage"]["total_usage"] - cpu_stats["precpucpu_usage"]["total_usage"] - cpu_stats["precpu_usage_usage"]["total_usage"]
   "]["total_usage"]
    system system_delta = cpu_stats_delta = cpu_stats["system_c["system_cpu_usage"] - cpupu_usage"] - cpu_stats_stats["precpu_usage"]["["precpu_usage"]["system_csystem_cpu_usage"]
pu_usage"]
    if system_delta > 0 and    if system_delta > 0 and cpu_d cpu_delta > 0:
       elta > 0:
        return return (cpu_delta / system_delta (cpu_delta / system_delta) *) * 100 100..00
    return 0.
    return 0.0

#0

# -------------------------
# 🔒 Admin Backup Endpoints -------------------------
# 🔒 Admin Backup Endpoints
# -----------------
# -------------------------
@app.post("/api/admin/backup--------
@app.post("/api/admin/backup")
async def trigger")
async def trigger_backup(
    background_tasks:_backup(
    background_tasks: BackgroundTasks
):
    BackgroundTasks
):
    """Trigger manual backup """Trigger manual backup (admin only (admin only)"""
    background_t)"""
    background_tasks.addasks.add_task(perform_full_backup)
    return {"status": "_task(perform_full_backup)
    return {"status": "successsuccess", "message": "Back", "message": "Backup started"}

@app.get("/api/admin/backups")
asyncup started"}

@app.get("/api/admin/backups")
async def list def list_backups():
    """List_backups():
    """List available backups available backups"""
    backup_files ="""
    backup_files = []
 []
    backup_dir = "/app    backup_dir = "/app/back/backups"
    
    if osups"
    
    if os.path.exists.path.exists(backup_dir):
       (backup_dir):
        for filename for filename in os.listdir( in os.listdir(backup_dirbackup_dir):
            filepath =):
            filepath = os.path os.path.join(backup_dir.join(backup_dir, filename, filename)
            if os.path.isfile(filepath):
                backup_files.append({
                    ")
            if os.path.isfile(filepath):
                backup_files.append({
                    "name": filename,
                    "size": os.path.getsize(filepathname": filename,
                    "size": os.path.getsize(filepath),
                    "created": datetime),
                    "created": datetime.from.fromtimestamp(os.path.getctime(filepath)).isoformat()
timestamp(os.path.getctime(filepath)).isoformat()
                               })
    
    return {"backups": backup_files}

# Background backup logic })
    
    return {"backups": backup_files}

# Background backup logic
def perform_full_backup():
    """Perform full platform backup"""
    os.makedirs("/app/backups", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"/app/backups/back
def perform_full_backup():
    """Perform full platform backup"""
    os.makedirs("/app/backups", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"/app/backups/backup_{timestamp}.tar.gz"
    
    try:
        subprocess.run(
            ["tar", "-up_{timestamp}.tar.gz"
    
    try:
        subprocess.run(
            ["tar", "-czf", backup_path, "/czf", backup_path, "/app/data", "/tmp/buildapp/data", "/tmp/builds"],
            check=True
        )
        prints"],
            check=True
        )
        print(f"✅ Backup created:(f"✅ Backup created: {backup_path}")
    except Exception {backup_path}")
    except Exception as e:
        print(f" as e:
        print(f"❌ Backup failed: {e}")
from fastapi.responses import Response

@app.get("/metrics")
async def metrics():
    """Prometheus-style metrics endpoint"""
    metrics_data = []
    
    # Platform metrics
    metrics_data.append(f"mini_cloud_apps_total {len(apps_db)}")
    metrics_data.append(f"mini_cloud_apps_running {len([app for app in apps_db.values() if app.get('status') == 'running'])}")
    
    # User metrics (if database is active)
    try:
        users_total = len(users_db) if 'users_db' in globals() else 0
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
    
    # Add to main.py
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
    # Add to main.py
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
    
if __name__ == "__main__":
    import uvicorn
    u❌ Backup failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicvicorn.run(app, host="0.0.orn.run(app, host="0.0.0.0",0.0", port=8000)