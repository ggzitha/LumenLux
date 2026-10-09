# Lumen-Zero Backend (Raspberry Pi Zero W)

Ultra-lightweight REST API & hardware controller for **WS2812B NeoPixel** LED strips and **USB/CSI cameras** on Raspberry Pi Zero W.

---

## ⚡ Hardware Wiring

- **WS2812B Data**: Raspberry Pi **GPIO 18** (Physical Pin 12 - Hardware PWM)
- **WS2812B Power**: 5V External Power Supply (shared GND with Raspberry Pi)
- **Camera**: USB Webcam (e.g. Logitech C920/C922 Pro) via OTG USB adapter, or CSI Camera module via ribbon cable.

---

## 📂 Architecture & Files

- `server.py`: Standard library `ThreadingHTTPServer` REST & MJPEG server with CORS and error resilience.
- `led_manager.py`: Thread-safe WS2812B strip controller with sleep-on-static (0% idle CPU for static lighting).
- `effects.py`: Low-overhead integer-math effect engine (Static, Rainbow, Breathing, Marquee, Gradient, Strobe, Wipe, Fire).
- `camera_manager.py`: V4L2 and Picamera2 camera discovery, hardware controls, and hardware MJPEG frame grabber.
- `lumen-pi.service`: Systemd service definition.
- `setup.sh`: One-click setup script.

---

## 🚀 Running the Server

### Manual execution
```bash
sudo python3 /home/pi/Beckend/server.py --port 5000 --pin 18 --count 100
```

### Systemd Service (Autostart on Boot)
```bash
sudo systemctl enable lumen-pi.service
sudo systemctl start lumen-pi.service
sudo systemctl status lumen-pi.service
```

---

## 📡 REST API Summary

- `GET /api/status`: System health metrics (CPU load, temperature, RAM, uptime) + light & camera state.
- `GET /api/cameras`: Returns list of detected cameras (`id`, `name`, `type`, `device_path`).
- `POST /api/camera/select`: Switch camera, set width, height, and target FPS.
- `POST /api/camera/control`: Set V4L2 control values (focus, exposure, white balance, zoom).
- `POST /api/camera/toggle`: Enable / disable camera capture.
- `GET /api/camera/stream`: MJPEG multipart stream.
- `GET /api/camera/snapshot`: Single JPEG snapshot.
- `GET /api/lights`: Returns WS2812B state.
- `POST /api/lights`: Update power, RGB color, brightness, effect, speed.
