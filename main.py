import os
import uuid
import json
import asyncio
import subprocess
import secrets
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

# Security imports
import bcrypt
from jose import JWTError, jwt
from passlib.context import CryptContext

from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
import redis.asyncio as redis
from pydantic import BaseModel, EmailStr, validator
import docker
from docker.models.containers import Container
from sqlalchemy.orm import Session
from database import get_db, User, App, APIKey, AuditLog
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ===== CONFIGURATION =====
CONFIG = {
    "domain": os.getenv("PLATFORM_DOMAIN", "localhost"),
    "docker_network": "mini-cloud-network",
    "data_volume": "mini-cloud-data",
    "port_range_start": 10000,
    "jwt_secret": os.getenv("SECRET_KEY", secrets.token_urlsafe(64)),
    "jwt_algorithm": "HS256",
    "jwt_expire_minutes": 60,
    "docker_host": os.getenv("DOCKER_HOST", "tcp://docker-socket:2375")
}

# Security
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

# Rate Limiter
limiter = Limiter(key_func=get_remote_address)

# Redis for rate limiting and caching
redis_client = None

# Docker client with timeout and retries
try:
    docker_client = docker.DockerClient(base_url=CONFIG["docker_host"], timeout=30)
except:
    docker_client = None

# ===== PYDANTIC MODELS =====
class UserCreate(BaseModel):
    email: EmailStr
    password: str
    
    @validator('password')
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters')
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one digit')
        return v

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str
    expires_in: int

class DeploymentRequest(BaseModel):
    name: str
    git_url: str
    environment_variables: Dict[str, str] = {}
    port: Optional[int] = None
    memory_limit: str = "512M"
    cpu_limit: str = "0.5"

class AppStatus(BaseModel):
    id: str
    name: str
    status: str
    url: str
    port: int
    created_at: str
    git_url: Optional[str] = None
    container_id: Optional[str] = None
    memory_usage: Optional[str] = None
    cpu_usage: Optional[str] = None

# ===== SECURITY FUNCTIONS =====
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=CONFIG["jwt_expire_minutes"])
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, CONFIG["jwt_secret"], algorithm=CONFIG["jwt_algorithm"])
    return encoded_jwt

def create_refresh_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=7)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, CONFIG["jwt_secret"], algorithm=CONFIG["jwt_algorithm"])
    return encoded_jwt

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, CONFIG["jwt_secret"], algorithms=[CONFIG["jwt_algorithm"]])
        if payload.get("type") == "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token not allowed here"
            )
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"user_id": user_id, "email": payload.get("email")}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def log_audit(db: Session, user_id: str, action: str, details: str = ""):
    audit_log = AuditLog(
        id=str(uuid.uuid4()),
        user_id=user_id,
        action=action,
        details=details,
        ip_address="",  # Get from request in actual endpoint
        user_agent=""
    )
    db.add(audit_log)
    db.commit()

# ===== LIFECYCLE =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global redis_client
    redis_client = redis.Redis(host="redis", port=6379, decode_responses=True)
    await FastAPILimiter.init(redis_client)
    
    # Initialize Docker network
    try:
        docker_client.networks.get(CONFIG["docker_network"])
    except:
        docker_client.networks.create(CONFIG["docker_network"], driver="bridge")
    
    yield
    
    # Shutdown
    if redis_client:
        await redis_client.close()

# Create FastAPI app
app = FastAPI(title="Mini Cloud Platform", version="2.0.0", lifespan=lifespan)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=json.loads(os.getenv("ALLOWED_ORIGINS", '["http://localhost:3000"]')),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[CONFIG["domain"], f"*.{CONFIG['domain']}"] if CONFIG["domain"] != "localhost" else ["*"]
)

# Rate limiting error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ===== AUTH ENDPOINTS =====
@app.post("/api/auth/register", response_model=Token)
@limiter.limit("5/minute")
async def register(
    request: Request,
    user: UserCreate, 
    db: Session = Depends(get_db)
):
    # Check if user exists
    existing_user = db.query(User).filter(User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = str(uuid.uuid4())
    hashed_password = get_password_hash(user.password)
    
    db_user = User(
        id=user_id,
        email=user.email,
        hashed_password=hashed_password,
        is_active=True,
        created_at=datetime.utcnow()
    )
    db.add(db_user)
    db.commit()
    
    access_token = create_access_token({"sub": user_id, "email": user.email})
    refresh_token = create_refresh_token({"sub": user_id})
    
    log_audit(db, user_id, "user_registered", f"Email: {user.email}")
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": CONFIG["jwt_expire_minutes"] * 60
    }

@app.post("/api/auth/login", response_model=Token)
@limiter.limit("10/minute")
async def login(
    request: Request,
    user: UserLogin, 
    db: Session = Depends(get_db)
):
    db_user = db.query(User).filter(User.email == user.email).first()
    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    if not db_user.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated")
    
    access_token = create_access_token({"sub": db_user.id, "email": db_user.email})
    refresh_token = create_refresh_token({"sub": db_user.id})
    
    log_audit(db, db_user.id, "user_login", "Successful login")
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": CONFIG["jwt_expire_minutes"] * 60
    }

@app.post("/api/auth/refresh", response_model=Token)
async def refresh_token(
    refresh_token: str,
    db: Session = Depends(get_db)
):
    try:
        payload = jwt.decode(refresh_token, CONFIG["jwt_secret"], algorithms=[CONFIG["jwt_algorithm"]])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        
        user_id = payload.get("sub")
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        
        new_access_token = create_access_token({"sub": user.id, "email": user.email})
        new_refresh_token = create_refresh_token({"sub": user.id})
        
        return {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer",
            "expires_in": CONFIG["jwt_expire_minutes"] * 60
        }
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

# ===== APP DEPLOYMENT =====
@app.post("/api/deploy", response_model=dict)
@limiter.limit("3/minute")
async def deploy_app(
    request: Request,
    deployment: DeploymentRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(verify_token),
    db: Session = Depends(get_db)
):
    # Validate Git URL
    if not deployment.git_url.startswith(("http://", "https://", "git@")):
        raise HTTPException(status_code=400, detail="Invalid Git URL")
    
    # Check for duplicate app names
    existing_app = db.query(App).filter(
        App.name == deployment.name,
        App.user_id == current_user["user_id"]
    ).first()
    if existing_app:
        raise HTTPException(status_code=400, detail="App with this name already exists")
    
    app_id = str(uuid.uuid4())[:8]
    
    # Determine port
    last_app = db.query(App).order_by(App.port.desc()).first()
    port = deployment.port or (CONFIG["port_range_start"] + (last_app.port if last_app else 0))
    
    # Generate subdomain
    if CONFIG["domain"] == "localhost":
        url = f"http://localhost:{port}"
        subdomain = f"localhost:{port}"
    else:
        subdomain = f"{deployment.name.lower().replace(' ', '-')}-{app_id}"
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
        user_id=current_user["user_id"],
        memory_limit=deployment.memory_limit,
        cpu_limit=deployment.cpu_limit,
        created_at=datetime.utcnow()
    )
    db.add(db_app)
    db.commit()
    
    # Start deployment in background
    background_tasks.add_task(
        deploy_app_background,
        app_id, deployment, subdomain, current_user["user_id"], db
    )
    
    log_audit(db, current_user["user_id"], "app_deploy_started", f"App: {deployment.name}")
    
    return {
        "app_id": app_id,
        "status": "building",
        "url": url,
        "message": "Deployment started",
        "estimated_time": "2-5 minutes"
    }

async def deploy_app_background(app_id: str, deployment: DeploymentRequest, subdomain: str, user_id: str, db: Session):
    app = db.query(App).filter(App.id == app_id).first()
    if not app:
        return
    
    try:
        print(f"🛠️ Building app: {app.name} ({app_id})")
        
        # Clone repo
        build_path = f"/tmp/builds/{app_id}"
        os.makedirs(build_path, exist_ok=True)
        
        result = subprocess.run(
            ["git", "clone", "--depth", "1", deployment.git_url, build_path],
            capture_output=True, text=True, timeout=300
        )
        
        if result.returncode != 0:
            raise Exception(f"Git clone failed: {result.stderr}")
        
        # Check for Dockerfile, create if missing
        dockerfile_path = os.path.join(build_path, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            await generate_dockerfile(build_path)
        
        # Build Docker image
        image_tag = f"mini-cloud-app-{app_id}:{secrets.token_hex(8)}"
        print(f"🐳 Building Docker image: {image_tag}")
        
        build_logs = []
        for line in docker_client.images.build(
            path=build_path,
            tag=image_tag,
            rm=True,
            forcerm=True,
            buildargs=deployment.environment_variables,
            network_mode=CONFIG["docker_network"],
            pull=True
        ):
            if 'stream' in line:
                log_line = line['stream'].strip()
                if log_line:
                    build_logs.append(log_line)
                    print(log_line)
        
        # Create container
        environment_vars = {
            **deployment.environment_variables,
            "PORT": str(app.port),
            "APP_ID": app_id
        }
        
        container = docker_client.containers.run(
            image_tag,
            detach=True,
            name=f"app-{app_id}",
            network=CONFIG["docker_network"],
            environment=environment_vars,
            labels={
                "traefik.enable": "true",
                f"traefik.http.routers.app-{app_id}.rule": f"Host(`{subdomain}.{CONFIG['domain']}`)",
                f"traefik.http.routers.app-{app_id}.entrypoints": "websecure",
                f"traefik.http.routers.app-{app_id}.tls.certresolver": "myresolver",
                f"traefik.http.services.app-{app_id}.loadbalancer.server.port": str(app.port),
                "com.minicloud.user_id": user_id,
                "com.minicloud.app_id": app_id
            },
            mem_limit=deployment.memory_limit,
            mem_reservation=deployment.memory_limit.replace("M", "").replace("G", "") + "M",
            cpu_period=100000,
            cpu_quota=int(float(deployment.cpu_limit) * 100000),
            security_opt=["no-new-privileges:true"],
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
            healthcheck={
                "test": ["CMD", "curl", "-f", f"http://localhost:{app.port}/health || exit 1"],
                "interval": 30000000000,
                "timeout": 5000000000,
                "retries": 3
            }
        )
        
        # Update database
        app.status = "running"
        app.container_id = container.id
        app.image_tag = image_tag
        app.updated_at = datetime.utcnow()
        db.commit()
        
        print(f"✅ Successfully deployed {app.name} at {app.url}")
        log_audit(db, user_id, "app_deployed", f"App: {app.name}, Container: {container.id}")
        
    except subprocess.TimeoutExpired:
        app.status = "error"
        app.error_message = "Build timeout (5 minutes exceeded)"
        db.commit()
        log_audit(db, user_id, "app_deploy_failed", f"App: {app.name} - Timeout")
        
    except Exception as e:
        app.status = "error"
        app.error_message = str(e)
        db.commit()
        print(f"❌ Deployment failed for {app_id}: {e}")
        log_audit(db, user_id, "app_deploy_failed", f"App: {app.name} - {str(e)}")
        
        # Cleanup
        try:
            subprocess.run(["rm", "-rf", f"/tmp/builds/{app_id}"])
        except:
            pass

async def generate_dockerfile(build_path: str):
    if os.path.exists(os.path.join(build_path, "package.json")):
        dockerfile_content = """FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
COPY . .
RUN npm run build --if-present
USER node
EXPOSE 3000
CMD ["npm", "start"]
"""
    elif os.path.exists(os.path.join(build_path, "requirements.txt")):
        dockerfile_content = """FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["python", "app.py"]
"""
    elif os.path.exists(os.path.join(build_path, "go.mod")):
        dockerfile_content = """FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -a -installsuffix cgo -o main .

FROM alpine:latest
RUN apk --no-cache add ca-certificates
WORKDIR /root/
COPY --from=builder /app/main .
EXPOSE 8080
CMD ["./main"]
"""
    else:
        dockerfile_content = """FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
"""
    
    with open(os.path.join(build_path, "Dockerfile"), "w") as f:
        f.write(dockerfile_content)

# ===== APP MANAGEMENT =====
@app.get("/api/apps", response_model=List[AppStatus])
@limiter.limit("30/minute")
async def list_apps(
    request: Request,
    current_user: dict = Depends(verify_token),
    db: Session = Depends(get_db)
):
    apps = db.query(App).filter(App.user_id == current_user["user_id"]).order_by(App.created_at.desc()).all()
    
    result = []
    for app in apps:
        # Get container stats if running
        memory_usage = cpu_usage = None
        if app.container_id and app.status == "running":
            try:
                container = docker_client.containers.get(app.container_id)
                stats = container.stats(stream=False)
                memory_usage = stats['memory_stats']['usage']
                cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
                system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
                cpu_usage = (cpu_delta / system_delta * 100) if system_delta > 0 else 0
            except:
                pass
        
        result.append(AppStatus(
            id=app.id,
            name=app.name,
            status=app.status,
            url=app.url,
            port=app.port,
            git_url=app.git_url,
            container_id=app.container_id,
            created_at=app.created_at.isoformat(),
            memory_usage=str(memory_usage) if memory_usage else None,
            cpu_usage=f"{cpu_usage:.2f}%" if cpu_usage else None
        ))
    
    return result

@app.get("/api/apps/{app_id}", response_model=AppStatus)
async def get_app(
    app_id: str,
    current_user: dict = Depends(verify_token),
    db: Session = Depends(get_db)
):
    app = db.query(App).filter(
        App.id == app_id,
        App.user_id == current_user["user_id"]
    ).first()
    
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    
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
@limiter.limit("10/minute")
async def start_app(
    request: Request,
    app_id: str,
    current_user: dict = Depends(verify_token),
    db: Session = Depends(get_db)
):
    app = db.query(App).filter(
        App.id == app_id,
        App.user_id == current_user["user_id"]
    ).first()
    
    if not app or not app.container_id:
        raise HTTPException(status_code=404, detail="App not found")
    
    try:
        container = docker_client.containers.get(app.container_id)
        container.start()
        app.status = "running"
        app.updated_at = datetime.utcnow()
        db.commit()
        
        log_audit(db, current_user["user_id"], "app_started", f"App: {app.name}")
        return {"status": "success", "message": "App started"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start app: {str(e)}")

@app.post("/api/apps/{app_id}/stop")
@limiter.limit("10/minute")
async def stop_app(
    request: Request,
    app_id: str,
    current_user: dict = Depends(verify_token),
    db: Session = Depends(get_db)
):
    app = db.query(App).filter(
        App.id == app_id,
        App.user_id == current_user["user_id"]
    ).first()
    
    if not app or not app.container_id:
        raise HTTPException(status_code=404, detail="App not found")
    
    try:
        container = docker_client.containers.get(app.container_id)
        container.stop(timeout=10)
        app.status = "stopped"
        app.updated_at = datetime.utcnow()
        db.commit()
        
        log_audit(db, current_user["user_id"], "app_stopped", f"App: {app.name}")
        return {"status": "success", "message": "App stopped"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop app: {str(e)}")

@app.delete("/api/apps/{app_id}")
@limiter.limit("5/minute")
async def delete_app(
    request: Request,
    app_id: str,
    current_user: dict = Depends(verify_token),
    db: Session = Depends(get_db)
):
    app = db.query(App).filter(
        App.id == app_id,
        App.user_id == current_user["user_id"]
    ).first()
    
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    
    try:
        # Stop and remove container
        if app.container_id:
            try:
                container = docker_client.containers.get(app.container_id)
                container.stop(timeout=10)
                container.remove(v=True, force=True)
            except:
                pass
        
        # Remove image
        if app.image_tag:
            try:
                docker_client.images.remove(app.image_tag, force=True)
            except:
                pass
        
        # Remove build directory
        try:
            subprocess.run(["rm", "-rf", f"/tmp/builds/{app_id}"])
        except:
            pass
        
        # Delete from database
        db.delete(app)
        db.commit()
        
        log_audit(db, current_user["user_id"], "app_deleted", f"App: {app.name}")
        return {"status": "success", "message": "App deleted"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete app: {str(e)}")

# ===== SYSTEM ENDPOINTS =====
@app.get("/api/system/stats")
@limiter.limit("60/minute")
async def system_stats(
    request: Request,
    current_user: dict = Depends(verify_token),
    db: Session = Depends(get_db)
):
    try:
        # Docker info
        info = docker_client.info()
        
        # Container counts
        all_containers = docker_client.containers.list(all=True)
        user_containers = [c for c in all_containers if c.labels.get("com.minicloud.user_id") == current_user["user_id"]]
        
        # Platform stats
        total_apps = db.query(App).filter(App.user_id == current_user["user_id"]).count()
        running_apps = db.query(App).filter(
            App.user_id == current_user["user_id"],
            App.status == "running"
        ).count()
        
        # Resource usage
        total_memory = info['MemTotal']
        used_memory = sum(c.attrs['HostConfig']['Memory'] for c in user_containers if 'Memory' in c.attrs['HostConfig'])
        
        return {
            "platform": {
                "total_apps": total_apps,
                "running_apps": running_apps,
                "stopped_apps": total_apps - running_apps
            },
            "resources": {
                "memory": {
                    "total": total_memory,
                    "used": used_memory,
                    "percent": round((used_memory / total_memory * 100), 2) if total_memory > 0 else 0
                },
                "cpu_count": info['NCPU'],
                "docker_version": info['ServerVersion']
            },
            "user": {
                "container_count": len(user_containers),
                "email": current_user["email"]
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")

@app.get("/health")
async def health_check():
    try:
        # Check database
        db = next(get_db())
        db.execute("SELECT 1")
        
        # Check Docker
        docker_client.ping()
        
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "version": "2.0.0"
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Service unhealthy: {str(e)}")

# ===== STATIC FILES =====
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")
app.mount("/", StaticFiles(directory="public", html=True), name="public")

@app.get("/")
async def serve_dashboard():
    return FileResponse('public/index.html')

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )