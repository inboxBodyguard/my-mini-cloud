// dashboard/server.js - Production Reverse Proxy & Dashboard Server
require('dotenv').config();
const express = require('express');
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { createProxyMiddleware } = require('http-proxy-middleware');
const WebSocket = require('ws');
const Docker = require('dockerode');
const { exec } = require('child_process');
const util = require('util');
const execPromise = util.promisify(exec);

const app = express();
const PORT = process.env.PORT || 3000;
const DASHBOARD_PORT = process.env.DASHBOARD_PORT || 3001;
const DOCKER_SOCKET = process.env.DOCKER_SOCKET || '/var/run/docker.sock';
const PLATFORM_API_URL = process.env.PLATFORM_API_URL || 'http://platform:8000';

// Docker client
const docker = new Docker({ socketPath: DOCKER_SOCKET });

// SSL for production (if you have certificates)
const SSL_KEY = process.env.SSL_KEY_PATH ? fs.readFileSync(process.env.SSL_KEY_PATH) : null;
const SSL_CERT = process.env.SSL_CERT_PATH ? fs.readFileSync(process.env.SSL_CERT_PATH) : null;

// Store active WebSocket connections
const logSubscribers = new Map(); // appId -> [WebSocket connections]
const buildLogs = new Map(); // buildId -> logs

// Middleware
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// ====================
// REVERSE PROXY ROUTING
// ====================

// Route to platform API
app.use('/api', createProxyMiddleware({
  target: PLATFORM_API_URL,
  changeOrigin: true,
  pathRewrite: { '^/api': '' },
  onProxyReq: (proxyReq, req, res) => {
    // Forward auth headers if present
    if (req.headers.authorization) {
      proxyReq.setHeader('authorization', req.headers.authorization);
    }
  }
}));

// Dynamic app routing based on hostname
app.use('/apps/*', async (req, res, next) => {
  const hostname = req.headers.host;
  
  // Extract subdomain (app-123.domain.com)
  const subdomain = hostname.split('.')[0];
  
  if (subdomain && subdomain !== 'www' && subdomain !== 'dashboard' && subdomain !== 'platform') {
    // Look up which port this app is running on
    try {
      const appsResponse = await fetch(`${PLATFORM_API_URL}/api/apps`);
      const apps = await appsResponse.json();
      
      const targetApp = apps.find(app => {
        const appSubdomain = app.url.split('://')[1]?.split('.')[0];
        return appSubdomain === subdomain;
      });
      
      if (targetApp && targetApp.status === 'running') {
        // Proxy to the app's container
        return createProxyMiddleware({
          target: `http://${targetApp.container_id}:${targetApp.port}`,
          changeOrigin: true,
          ws: true // Support WebSockets
        })(req, res, next);
      }
    } catch (err) {
      console.error('Proxy error:', err);
    }
  }
  
  next(); // Continue to next middleware if not an app
});

// ====================
// REAL-TIME LOGS ENDPOINT
// ====================

// WebSocket server for real-time logs
const wss = new WebSocket.Server({ noServer: true });

wss.on('connection', (ws, req) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const appId = url.searchParams.get('appId');
  const buildId = url.searchParams.get('buildId');
  
  if (appId) {
    // App logs subscription
    if (!logSubscribers.has(appId)) {
      logSubscribers.set(appId, []);
    }
    logSubscribers.get(appId).push(ws);
    
    // Send recent logs if available
    if (buildLogs.has(buildId)) {
      ws.send(JSON.stringify({
        type: 'logs',
        data: buildLogs.get(buildId)
      }));
    }
    
    ws.on('close', () => {
      const subscribers = logSubscribers.get(appId) || [];
      const index = subscribers.indexOf(ws);
      if (index > -1) subscribers.splice(index, 1);
    });
    
    ws.on('error', (err) => {
      console.error('WebSocket error:', err);
    });
  }
});

// Function to broadcast logs to subscribers
function broadcastLogs(appId, logData) {
  const subscribers = logSubscribers.get(appId);
  if (subscribers) {
    subscribers.forEach(ws => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'logs',
          data: logData,
          timestamp: new Date().toISOString()
        }));
      }
    });
  }
}

// ====================
// DASHBOARD API (Enhanced)
// ====================

// System metrics endpoint
app.get('/api/system/metrics', async (req, res) => {
  try {
    // Get Docker system info
    const [dockerInfo, containers, systemStats] = await Promise.all([
      docker.info(),
      docker.listContainers({ all: true }),
      getSystemStats()
    ]);
    
    // Calculate resource usage
    const platformContainers = containers.filter(c => 
      c.Names.some(name => name.includes('mini-cloud'))
    );
    
    const totalMemory = dockerInfo.MemTotal;
    const usedMemory = platformContainers.reduce((sum, c) => sum + (c.MemoryUsage || 0), 0);
    const totalCpu = dockerInfo.NCPU;
    
    res.json({
      docker: {
        version: dockerInfo.ServerVersion,
        containers: dockerInfo.Containers,
        running: dockerInfo.ContainersRunning,
        stopped: dockerInfo.ContainersStopped
      },
      resources: {
        memory: {
          total: totalMemory,
          used: usedMemory,
          percent: totalMemory > 0 ? Math.round((usedMemory / totalMemory) * 100) : 0
        },
        cpu: {
          cores: totalCpu,
          load: systemStats.cpuLoad
        },
        disk: systemStats.disk
      },
      network: {
        totalRx: platformContainers.reduce((sum, c) => sum + (c.NetworkSettings?.NetworkRx || 0), 0),
        totalTx: platformContainers.reduce((sum, c) => sum + (c.NetworkSettings?.NetworkTx || 0), 0)
      }
    });
  } catch (error) {
    console.error('Metrics error:', error);
    res.status(500).json({ error: 'Failed to get system metrics' });
  }
});

// Build logs streaming endpoint
app.get('/api/builds/:buildId/logs', async (req, res) => {
  const { buildId } = req.params;
  
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  
  // Send initial logs if available
  if (buildLogs.has(buildId)) {
    res.write(`data: ${JSON.stringify({ logs: buildLogs.get(buildId) })}\n\n`);
  }
  
  // Keep connection open for new logs
  const interval = setInterval(() => {
    if (buildLogs.has(buildId)) {
      res.write(`data: ${JSON.stringify({ logs: buildLogs.get(buildId), timestamp: new Date().toISOString() })}\n\n`);
    }
  }, 1000);
  
  req.on('close', () => {
    clearInterval(interval);
  });
});

// One-click template deployment
const TEMPLATES = {
  'nodejs-api': {
    name: 'Node.js API',
    git_url: 'https://github.com/vercel/next.js/examples/api-routes',
    description: 'Node.js API with Express',
    env: { PORT: '3000', NODE_ENV: 'production' }
  },
  'python-fastapi': {
    name: 'Python FastAPI',
    git_url: 'https://github.com/tiangolo/fastapi',
    description: 'FastAPI Python backend',
    env: { PORT: '8000', PYTHONUNBUFFERED: '1' }
  },
  'static-site': {
    name: 'Static Site',
    git_url: 'https://github.com/tailwindlabs/tailwindcss',
    description: 'HTML/CSS/JS static site',
    env: { PORT: '80' }
  },
  'wordpress': {
    name: 'WordPress',
    git_url: 'https://github.com/docker-library/wordpress',
    description: 'WordPress with MySQL',
    env: { WORDPRESS_DB_HOST: 'mysql', WORDPRESS_DB_NAME: 'wordpress' }
  }
};

app.post('/api/templates/:templateId/deploy', async (req, res) => {
  const { templateId } = req.params;
  const { appName = `${templateId}-${Date.now()}` } = req.body;
  
  const template = TEMPLATES[templateId];
  if (!template) {
    return res.status(404).json({ error: 'Template not found' });
  }
  
  try {
    // Call platform API to deploy
    const deployResponse = await fetch(`${PLATFORM_API_URL}/api/deploy`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: appName,
        git_url: template.git_url,
        environment_variables: template.env
      })
    });
    
    const result = await deployResponse.json();
    
    // Store build ID for log tracking
    if (result.app_id) {
      buildLogs.set(result.app_id, ['🚀 Starting template deployment...']);
      
      // Simulate build process (in production, hook into actual build)
      simulateBuildLogs(result.app_id, template.name);
    }
    
    res.json({
      ...result,
      template: template.name,
      message: `Deploying ${template.name} template...`
    });
  } catch (error) {
    console.error('Template deployment error:', error);
    res.status(500).json({ error: 'Failed to deploy template' });
  }
});

function simulateBuildLogs(appId, templateName) {
  const messages = [
    `📦 Cloning ${templateName} template...`,
    '🔧 Installing dependencies...',
    '🐳 Building Docker image...',
    '🚀 Starting container...',
    '✅ Deployment complete!'
  ];
  
  let index = 0;
  const interval = setInterval(() => {
    if (index < messages.length) {
      const log = messages[index];
      buildLogs.set(appId, [...(buildLogs.get(appId) || []), log]);
      broadcastLogs(appId, log);
      index++;
    } else {
      clearInterval(interval);
      // Clean up after 1 hour
      setTimeout(() => buildLogs.delete(appId), 3600000);
    }
  }, 2000);
}

// Database backup endpoint
app.post('/api/backup', async (req, res) => {
  const backupId = `backup-${Date.now()}`;
  
  try {
    // Create backup directory
    const backupDir = path.join(__dirname, 'backups');
    if (!fs.existsSync(backupDir)) {
      fs.mkdirSync(backupDir, { recursive: true });
    }
    
    // Backup PostgreSQL database
    const backupFile = path.join(backupDir, `${backupId}.sql`);
    const { stdout, stderr } = await execPromise(
      `pg_dump -h postgres -U admin cloudplatform > ${backupFile}`
    );
    
    // Backup Docker volumes
    const volumeBackupFile = path.join(backupDir, `${backupId}-volumes.tar.gz`);
    await execPromise(
      `docker run --rm -v postgres_data:/data -v ${backupDir}:/backup busybox tar -czf /backup/${backupId}-volumes.tar.gz -C /data .`
    );
    
    res.json({
      id: backupId,
      files: [`${backupId}.sql`, `${backupId}-volumes.tar.gz`],
      size: {
        database: fs.statSync(backupFile).size,
        volumes: fs.statSync(volumeBackupFile).size
      },
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    console.error('Backup error:', error);
    res.status(500).json({ error: 'Backup failed' });
  }
});

// System stats helper
async function getSystemStats() {
  try {
    // CPU load
    const { stdout: cpuStdout } = await execPromise("grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {print usage}'");
    const cpuLoad = parseFloat(cpuStdout.trim()) || 0;
    
    // Disk usage
    const { stdout: diskStdout } = await execPromise("df -h / | awk 'NR==2 {print $5}'");
    const diskUsage = diskStdout.trim().replace('%', '');
    
    return {
      cpuLoad: Math.round(cpuLoad),
      disk: {
        usage: parseInt(diskUsage) || 0,
        human: diskStdout.trim()
      }
    };
  } catch (error) {
    return { cpuLoad: 0, disk: { usage: 0, human: 'N/A' } };
  }
}

// ====================
// DASHBOARD ROUTES
// ====================

// Serve dashboard HTML
app.get(['/', '/dashboard', '/dashboard/*'], (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// Template list endpoint
app.get('/api/templates', (req, res) => {
  const templates = Object.entries(TEMPLATES).map(([id, template]) => ({
    id,
    ...template
  }));
  res.json({ templates });
});

// Catch-all for SPA routing
app.get('*', (req, res) => {
  if (req.accepts('html')) {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
  } else {
    res.status(404).json({ error: 'Not found' });
  }
});

// ====================
// SERVER STARTUP
// ====================

const server = http.createServer(app);

// Handle WebSocket upgrade
server.on('upgrade', (request, socket, head) => {
  const pathname = new URL(request.url, `http://${request.headers.host}`).pathname;
  
  if (pathname === '/ws/logs') {
    wss.handleUpgrade(request, socket, head, (ws) => {
      wss.emit('connection', ws, request);
    });
  } else {
    socket.destroy();
  }
});

server.listen(PORT, () => {
  console.log(`
  🚀 Mini Cloud Dashboard Server
  ===============================
  📊 Dashboard: http://localhost:${PORT}
  🔌 API Proxy: ${PLATFORM_API_URL}
  📡 WebSocket: ws://localhost:${PORT}/ws/logs
  🐳 Docker: ${DOCKER_SOCKET}
  
  📋 Available Templates:
  ${Object.keys(TEMPLATES).map(t => `  • ${t}`).join('\n')}
  
  ⚡ Ready to serve!`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
  console.log('SIGTERM received. Shutting down gracefully...');
  server.close(() => {
    console.log('HTTP server closed.');
    process.exit(0);
  });
});

process.on('SIGINT', () => {
  console.log('SIGINT received. Shutting down gracefully...');
  server.close(() => {
    console.log('HTTP server closed.');
    process.exit(0);
  });
});

// Export for testing
module.exports = { app, server, wss };