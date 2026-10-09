# Lumen-Zero Frontend Web Application (Docker)

High-performance remote management interface for **Raspberry Pi Zero W** camera streaming and **WS2812B NeoPixel** lighting.

The frontend handles the heavy lifting (authentication, connection pooling, proxying, image filtering, FPS counting, UI rendering, and browser cache storage) so the Pi Zero W remains responsive and low-temperature.

---

## 🚀 Features

- **Authentication & Security**:
  - Secure session login with signed HTTP-only cookies.
  - Configurable credentials via environment variables (`USER_WEB`, `USER_PASSWORD`).
- **Live Camera Studio**:
  - Auto-discovery of USB webcams (e.g. Logitech C920/C922 Pro) and CSI camera modules.
  - Camera device selector dropdown with live status indicator.
  - Enable / Pause camera button to conserve Pi Zero CPU when not in use.
  - Real-time client-measured FPS counter.
  - Client-side GPU-accelerated digital zoom & pan (1x - 3x).
  - High-resolution snapshot capture and instant download.
  - Rule-of-thirds framing grid & fullscreen preview mode.
  - Flip Horizontal & Flip Vertical.
  - Hardware V4L2 controls: Auto/Manual Focus, Auto/Manual White Balance (color temp 2800K - 6500K), Auto/Manual Exposure, and Hardware Lens Zoom.
- **WS2812B Light Studio**:
  - Master Power toggle with glowing status.
  - Interactive virtual LED strip simulator matching current color, brightness, and animated effects.
  - Color presets: **White** (default), **Red**, **Green**, **Blue**.
  - Custom RGB sliders and manual numeric inputs (0 - 255) for R, G, B channels.
  - Native browser color picker (`<input type="color">`) synced bidirectionally.
  - Master brightness control slider (0% - 100%).
  - Dynamic effects engine (inspired by WS2812FX):
    - **Static** (Default checked & active)
    - **Rainbow Cycle**
    - **Breathing / Pulse**
    - **Marquee / Theater Chase**
    - **Gradient Flow**
    - **Strobe / Flash**
    - **Color Wipe**
    - **Fire / Candle Flicker**
  - Animation speed slider (ms per frame).
- **Browser Persistence (LocalStorage)**:
  - All user adjustments (camera selection, resolution, FPS, LED color, brightness, effect, speed, filters) are cached in the browser and automatically restored.

---

## 🛠️ Configuration (`.env`)

Copy `.env.example` to `.env`:

```env
USER_WEB=admin
USER_PASSWORD=raspberry
BECKEND_RPI_IP=192.168.88.24
BECKEND_RPI_PORT=5000
FRONTEND_PORT=8080
SECRET_KEY=generate-a-strong-random-key-here
```

---

## 🐳 Running with Docker

### Option 1: Docker Compose (Recommended)
```bash
docker compose up -d --build
```
Access the application at `http://<your-docker-host-ip>:8080`.

### Option 2: Docker CLI
```bash
docker build -t lumen-zero-frontend .
docker run -d \
  --name lumen-frontend \
  -p 8080:8080 \
  --env-file .env \
  --restart unless-stopped \
  lumen-zero-frontend
```

---

## 💻 Running Locally (Development / Testing)

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```
Open your browser at `http://localhost:8080`.
