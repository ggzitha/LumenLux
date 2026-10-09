# Lumen-Zero: Remote Light (WS2812B) & Camera Management System

A distributed system engineered for remote lighting and camera control over IP with **Raspberry Pi Zero W** and a **Dockerized Python Frontend Web Application**.

---

## 🏗️ Architecture

```
[ Web Browser ]
      │ (HTTP/WebSocket)
      ▼
┌─────────────────────────────────────────────────────────────┐
│  FRONTEND (Runs on Docker Server)                           │
│  - FastAPI Web Application                                  │
│  - Authentication Guard (USER_WEB / USER_PASSWORD)          │
│  - Asynchronous Connection Pooling & Pi Zero Safe Proxy     │
│  - Client GPU Image Filters & Digital Zoom                  │
│  - Real-time Browser FPS Measurement                        │
│  - Browser LocalStorage User Preferences Persistence        │
└──────────────────────────────┬──────────────────────────────┘
                               │ (REST & MJPEG Stream over IP)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  BACKEND (Runs on Raspberry Pi Zero W - 192.168.88.24)      │
│  - Ultra-lightweight Python standard library server         │
│  - Zero Idle CPU usage on Static Lighting mode              │
│  - Direct V4L2 Hardware MJPEG Capture (Logitech C920/C922)  │
│  - CSI Camera Support (Picamera2)                           │
│  - Hardware WS2812B NeoPixel Control (GPIO 18 PWM)          │
│  - WS2812FX Dynamic Effects Engine (Rainbow, Breath, etc.)  │
└─────────────────────────────────────────────────────────────┘
```

---

## 📁 Repository Structure

```
.
├── Beckend/
│   ├── server.py              # Lightweight REST API & MJPEG streaming server
│   ├── led_manager.py         # Hardware WS2812B strip controller with sleep-on-static
│   ├── camera_manager.py      # V4L2 & CSI camera discovery, controls & capture
│   ├── effects.py             # Precomputed integer math effect engine (WS2812FX)
│   ├── LED_Test.py            # Original CLI reference script
│   ├── lumen-pi.service       # Systemd service unit for Raspberry Pi
│   ├── setup.sh               # One-click installation script for Pi
│   └── README.md
│
├── FrontEnd/
│   ├── app.py                 # FastAPI application with auth and resilient proxying
│   ├── requirements.txt       # Python dependencies
│   ├── Dockerfile             # Production Docker container image
│   ├── docker-compose.yml     # Docker Compose orchestration
│   ├── .env.example           # Environment variables template
│   ├── .env                   # Local configuration
│   ├── templates/
│   │   ├── login.html         # Sleek dark-mode glassmorphism login portal
│   │   └── index.html         # Camera Studio & WS2812B Lighting Console
│   ├── static/
│   │   ├── css/main.css       # Design system, glassmorphism, responsive grid
│   │   └── js/app.js          # Reactive controls, FPS counter, localStorage cache
│   └── README.md
│
└── README.md
```

---

## 🚀 Quick Start Guide

### 1. Raspberry Pi Zero (Backend)
The backend service is already installed and running on your Raspberry Pi Zero (`192.168.88.24`):
```bash
# Check service status on the Pi:
sudo systemctl status lumen-pi.service
```

### 2. Frontend (Docker Server)
On your Docker server:
1. Copy the `FrontEnd/` folder.
2. Configure `.env`:
   ```env
   USER_WEB=admin
   USER_PASSWORD=raspberry
   BECKEND_RPI_IP=192.168.88.24
   BECKEND_RPI_PORT=5000
   FRONTEND_PORT=8080
   SECRET_KEY=your-random-secret-key
   ```
3. Run with Docker Compose:
   ```bash
   docker compose up -d --build
   ```
4. Access the web dashboard at `http://<your-docker-server-ip>:8080`.
