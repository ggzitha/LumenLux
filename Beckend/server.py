#!/usr/bin/env python3
"""
Lumen-Zero Backend Server for Raspberry Pi Zero W.
High-efficiency REST API & MJPEG streaming server built with Python standard library.
Runs on GPIO 18 for WS2812B strip and manages USB/CSI cameras.
"""
import argparse
import ipaddress
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Allowed IP subnets (Strict LAN-only access control)
ALLOWED_NETWORKS = [
    ipaddress.ip_network("192.168.88.0/23"),  # covers 192.168.88.0 - 192.168.89.255
    ipaddress.ip_network("88.88.88.0/24"),
    ipaddress.ip_network("10.10.1.0/24"),
    ipaddress.ip_network("192.168.70.0/24"),
    ipaddress.ip_network("172.19.176.0/24"),
    ipaddress.ip_network("192.168.90.0/24"),
    ipaddress.ip_network("192.168.92.0/24"),
    ipaddress.ip_network("192.168.96.0/22"),  # covers 192.168.96.0 - 192.168.99.255
    ipaddress.ip_network("192.168.112.0/22"), # covers 192.168.112.0 - 192.168.115.255
    ipaddress.ip_network("192.168.85.0/30"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

def is_ip_allowed(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        if getattr(ip_obj, "ipv4_mapped", None):
            ip_obj = ip_obj.ipv4_mapped
        return any(ip_obj in net for net in ALLOWED_NETWORKS)
    except ValueError:
        return False

# Import managers
from led_manager import LEDManager
from camera_manager import CameraManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("server")

# Global instances
led_mgr = None
cam_mgr = None

# Hardware Watchdog & Client Activity Tracking
# If hardware is running but no client requests / heartbeats are received for CLIENT_WATCHDOG_TIMEOUT seconds,
# the backend assumes Frontend Docker / Python has terminated or disconnected, and turns off camera and LEDs.
CLIENT_WATCHDOG_TIMEOUT = float(os.environ.get("CLIENT_WATCHDOG_TIMEOUT", 15.0))  # seconds
last_client_activity = time.time()
watchdog_running = True

def touch_client_activity():
    global last_client_activity
    last_client_activity = time.time()

def get_system_stats() -> dict:
    """Retrieve lightweight Raspberry Pi system metrics."""
    stats = {
        "uptime_sec": 0,
        "cpu_temp_c": None,
        "ram_total_mb": None,
        "ram_free_mb": None,
        "load_avg": os.getloadavg() if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0],
        "device": "Raspberry Pi Zero W"
    }
    
    # Read uptime
    try:
        with open("/proc/uptime", "r") as f:
            stats["uptime_sec"] = int(float(f.readline().split()[0]))
    except Exception:
        pass

    # Read CPU temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            stats["cpu_temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        pass

    # Read Meminfo
    try:
        with open("/proc/meminfo", "r") as f:
            mem = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip().split()[0]
                    mem[k] = int(v)
            if "MemTotal" in mem and "MemAvailable" in mem:
                stats["ram_total_mb"] = round(mem["MemTotal"] / 1024, 1)
                stats["ram_free_mb"] = round(mem["MemAvailable"] / 1024, 1)
    except Exception:
        pass

    return stats


class APIHandler(BaseHTTPRequestHandler):
    """Handles REST API requests and MJPEG video streaming."""

    def _set_cors_headers(self, content_type: str = "application/json"):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def do_OPTIONS(self):
        """Handle preflight CORS requests."""
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def _read_json_body(self) -> dict:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode("utf-8")
                return json.loads(body)
        except Exception as e:
            logger.error("Failed to parse request JSON: %s", e)
        return {}

    def _send_json(self, data: dict, status_code: int = 200):
        body_bytes = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self._set_cors_headers("application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _check_ip_allowed(self) -> bool:
        client_ip = self.headers.get("X-Forwarded-For")
        if client_ip:
            client_ip = client_ip.split(",")[0].strip()
        else:
            client_ip = self.headers.get("X-Real-IP") or self.client_address[0]
            
        if not is_ip_allowed(client_ip):
            logger.warning("[AKSES DITOLAK] IP '%s' mencoba mengakses backend.", client_ip)
            self._send_json({
                "status": "forbidden",
                "error": "Akses Aplikasi Ditolak",
                "detail": "Terdeteksi Menggunakan VPN atau Jaringan WAN, silahkan gunakan jaringan LAN",
                "ip_terdeteksi": client_ip,
                "footer": "Akses hanya diperbolehkan melalui jaringan internal/LAN"
            }, status_code=403)
            return False
        return True

    def do_GET(self):
        if not self._check_ip_allowed():
            return
        touch_client_activity()

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        # 0. Heartbeat keepalive
        if path == "/api/heartbeat":
            self._send_json({"status": "ok", "timestamp": time.time()})

        # 1. System & Full Status
        elif path == "/api/status" or path == "/":
            response = {
                "status": "ok",
                "system": get_system_stats(),
                "lights": led_mgr.get_state() if led_mgr else None,
                "camera": cam_mgr.get_state() if cam_mgr else None,
                "timestamp": time.time()
            }
            self._send_json(response)

        # 2. Camera endpoints
        elif path == "/api/cameras":
            cams = cam_mgr.discover_cameras() if cam_mgr else []
            self._send_json({"cameras": cams, "active_id": cam_mgr.active_camera_id if cam_mgr else None})

        elif path == "/api/camera/state":
            state = cam_mgr.get_state() if cam_mgr else {}
            self._send_json(state)

        elif path == "/api/camera/snapshot":
            frame = cam_mgr.get_snapshot() if cam_mgr else b""
            self.send_response(200)
            self._set_cors_headers("image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)

        elif path == "/api/camera/stream":
            # MJPEG stream
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()

            try:
                for chunk in cam_mgr.generate_mjpeg_stream():
                    touch_client_activity()
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                logger.debug("MJPEG client disconnect: %s", e)

        # 3. Lights endpoints
        elif path == "/api/lights":
            state = led_mgr.get_state() if led_mgr else {}
            self._send_json(state)

        else:
            self._send_json({"error": "Not Found", "path": path}, status_code=404)

    def do_POST(self):
        if not self._check_ip_allowed():
            return
        touch_client_activity()

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        data = self._read_json_body()

        # 0. Heartbeat keepalive
        if path == "/api/heartbeat":
            self._send_json({"status": "ok", "timestamp": time.time()})

        # 1. Lights control
        elif path == "/api/lights":
            color = data.get("color")
            if isinstance(color, list) and len(color) >= 3:
                color_tuple = (color[0], color[1], color[2])
            elif isinstance(color, dict) and "r" in color:
                color_tuple = (color.get("r", 255), color.get("g", 255), color.get("b", 255))
            else:
                color_tuple = None

            new_state = led_mgr.set_state(
                power=data.get("power"),
                color=color_tuple,
                brightness=data.get("brightness"),
                effect=data.get("effect"),
                speed=data.get("speed"),
                preset=data.get("preset")
            )
            self._send_json({"status": "ok", "lights": new_state})

        # 2. Camera selection & settings
        elif path == "/api/camera/select":
            cam_id = data.get("camera_id")
            w = data.get("width")
            h = data.get("height")
            fps = data.get("fps")
            new_state = cam_mgr.select_camera(cam_id, width=w, height=h, fps=fps)
            self._send_json({"status": "ok", "camera": new_state})

        elif path == "/api/camera/toggle":
            enabled = data.get("enabled", True)
            cam_mgr.set_enabled(enabled)
            self._send_json({"status": "ok", "camera_enabled": cam_mgr.camera_enabled})

        elif path == "/api/camera/control":
            ctrl = data.get("control")
            val = data.get("value")
            cam_id = data.get("camera_id")
            success = cam_mgr.set_camera_control(ctrl, val, cam_id=cam_id)
            self._send_json({"status": "ok" if success else "failed", "control": ctrl, "value": val})

        elif path == "/api/camera/reset_defaults":
            cam_id = data.get("camera_id")
            success = cam_mgr.reset_camera_defaults(cam_id=cam_id)
            self._send_json({"status": "ok" if success else "failed", "camera": cam_mgr.get_state() if cam_mgr else {}})

        else:
            self._send_json({"error": "Not Found", "path": path}, status_code=404)

    def log_message(self, format, *args):
        # Suppress spammy per-frame HTTP logs to avoid saturating Pi log buffers
        if args and len(args) > 0:
            first_arg = str(args[0])
            if "/api/camera/stream" in first_arg or "/api/status" in first_arg or "/api/heartbeat" in first_arg:
                return
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)


def main():
    global led_mgr, cam_mgr, watchdog_running

    parser = argparse.ArgumentParser(description="Lumen-Zero Pi Backend Server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)), help="Port (default: 5000)")
    parser.add_argument("--pin", type=int, default=int(os.environ.get("LED_PIN", 18)), help="WS2812B GPIO Pin (default: 18)")
    parser.add_argument("--count", type=int, default=int(os.environ.get("LED_COUNT", 100)), help="Number of LEDs (default: 100)")
    args = parser.parse_args()

    # Initialize Hardware Managers
    logger.info("Initializing WS2812B LED Manager on GPIO %d (count: %d)...", args.pin, args.count)
    led_mgr = LEDManager(count=args.count, pin=args.pin)

    logger.info("Initializing Camera Manager...")
    cam_mgr = CameraManager()

    def watchdog_worker():
        global watchdog_running
        logger.info(
            "Hardware Watchdog active (Timeout: %.1fs). "
            "Pi will auto-power down camera and LEDs if Frontend stops or disconnects.",
            CLIENT_WATCHDOG_TIMEOUT
        )
        while watchdog_running:
            time.sleep(2.0)
            try:
                cam_on = bool(cam_mgr and getattr(cam_mgr, "camera_enabled", False))
                led_on = False
                if led_mgr:
                    st = led_mgr.get_state()
                    led_on = bool(st.get("power", False))

                # Only evaluate timeout if hardware is actively ON
                if cam_on or led_on:
                    idle = time.time() - last_client_activity
                    if idle >= CLIENT_WATCHDOG_TIMEOUT:
                        logger.warning(
                            "[WATCHDOG AUTO-OFF] No active client connection for %.1fs (limit: %.1fs). "
                            "Frontend Docker/Python appears stopped. Auto-powering down camera and LEDs...",
                            idle, CLIENT_WATCHDOG_TIMEOUT
                        )
                        if cam_on and cam_mgr:
                            cam_mgr.set_enabled(False)
                        if led_on and led_mgr:
                            led_mgr.set_state(power=False)
            except Exception as e:
                logger.error("Watchdog worker exception: %s", e)

    watchdog_thread = threading.Thread(target=watchdog_worker, name="ClientWatchdog", daemon=True)
    watchdog_thread.start()

    def handle_signal(sig, frame):
        global watchdog_running
        logger.info("Termination signal received. Shutting down gracefully...")
        watchdog_running = False
        try:
            if led_mgr:
                led_mgr.cleanup()
            if cam_mgr:
                cam_mgr.cleanup()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    server_address = ("0.0.0.0", args.port)
    httpd = ThreadingHTTPServer(server_address, APIHandler)
    logger.info("=== Lumen-Zero Backend running on http://0.0.0.0:%d ===", args.port)
    logger.info("Ready to accept connections from Frontend Docker webapp.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        watchdog_running = False
        if led_mgr:
            led_mgr.cleanup()
        if cam_mgr:
            cam_mgr.cleanup()
        httpd.server_close()
        logger.info("Server stopped.")


if __name__ == "__main__":
    main()
