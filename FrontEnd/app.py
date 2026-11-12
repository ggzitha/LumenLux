"""
Lumen-Zero Frontend Web Application.
Built with FastAPI to perform heavy lifting: authentication, stream proxying,
connection pooling, response caching, inactivity auto-shutdown, and error resilience for Raspberry Pi.
"""
from __future__ import annotations
import asyncio
import io
import os
import time
import logging
from typing import Optional, Dict, Any, List, Tuple, Set, Union
from pathlib import Path

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, Request, Response, Form, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import ipaddress
import secrets
import base64
import re
import json
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from PIL import Image
from ocr_engine import detect_thermometers_in_image, perform_display_ocr, generate_project_excel, extract_roi_region
from ocr_trainer import (
    get_training_dataset_stats,
    save_labeled_roi_crop,
    delete_labeled_frame,
    delete_hard_example,
    assess_crop_quality,
    train_custom_model,
    get_train_config
)

# Load environment configuration (override=False to let Docker/Dockge env variables take precedence)
load_dotenv(override=False)

# Allowed IP subnets (Strict LAN-only access control)
DEFAULT_ALLOWED_NETWORKS = [
    "192.168.88.0/23",  # covers 192.168.88.0 - 192.168.89.255
    "88.88.88.0/24",
    "10.10.1.0/24",
    "10.101.27.0/24",   # Docker internal bridge network (FrontEnd dockge)
    "10.101.29.0/24",   # Docker internal bridge network
    "192.168.70.0/24",
    "172.19.176.0/24",
    "192.168.90.0/24",
    "192.168.92.0/24",
    "192.168.96.0/22",  # covers 192.168.96.0 - 192.168.99.255
    "192.168.112.0/22", # covers 192.168.112.0 - 192.168.115.255
    "192.168.85.0/30",
    "127.0.0.0/8",
    "::1/128",
]

ALLOWED_NETWORKS = []
for net_str in DEFAULT_ALLOWED_NETWORKS:
    try:
        ALLOWED_NETWORKS.append(ipaddress.ip_network(net_str.strip()))
    except ValueError:
        pass

# Optional extra subnets from .env
extra_nets = os.environ.get("EXTRA_ALLOWED_NETWORKS", "")
if extra_nets:
    for item in extra_nets.split(","):
        item = item.strip()
        if item:
            try:
                net_obj = ipaddress.ip_network(item)
                if net_obj not in ALLOWED_NETWORKS:
                    ALLOWED_NETWORKS.append(net_obj)
            except ValueError:
                pass

def get_client_ip(request: Request) -> str:
    """Extract real client IP address from proxy headers or connection socket."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",")[0].strip()
        if ip:
            return ip
    x_real = request.headers.get("x-real-ip")
    if x_real:
        return x_real.strip()
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    if request.client and request.client.host:
        return request.client.host.strip()
    return "127.0.0.1"


def is_ip_allowed(ip_str: str) -> bool:
    """Verify if client IP address belongs to permitted internal/LAN subnets."""
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        if getattr(ip_obj, "ipv4_mapped", None):
            ip_obj = ip_obj.ipv4_mapped
        return any(ip_obj in net for net in ALLOWED_NETWORKS)
    except ValueError:
        return False

USER_WEB = os.environ.get("USER_WEB", "admin")
USER_PASSWORD = os.environ.get("USER_PASSWORD", "raspberry")
BECKEND_RPI_IP = os.environ.get("BECKEND_RPI_IP", "192.168.88.21")
BECKEND_RPI_PORT = int(os.environ.get("BECKEND_RPI_PORT", 5000))
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", 8080))
SECRET_KEY = os.environ.get("SECRET_KEY", "lumen-zero-default-super-secure-key-2026")

# Hardware & Feature Defaults from .env
ANTI_FLICKER = os.environ.get("ANTI_FLICKER", "50").strip()
LED_BRIGHTNESS = int(os.environ.get("LED_BRIGHTNESS", 45))
DEFAULT_LED_COLOR = os.environ.get("DEFAULT_LED_COLOR", "WHITE").strip()
LED_AUTO_OFF = int(os.environ.get("LED_AUTO_OFF", 120))  # Auto turn off WS2812B LEDs after 2m (120s)
LED_LENGTH = int(os.environ.get("LED_LENGTH", 100))
DEFAULT_RESOLUTION = os.environ.get("DEFAULT_RESOLUTION", "1920x1080").strip()
DEFAULT_DEVICE_CAM_FIT = os.environ.get("DEFAULT_DEVICE_CAM_FIT", "cover").strip().lower()
OCR_PASSWORD = os.environ.get("OCR_PASSWORD", "illuminati")
DEFAULT_STREAM_ENGINE = os.environ.get("DEFAULT_STREAM_ENGINE", "webrtc").strip().lower()

# Full System Inactivity Timeout (Seconds) - Powers off camera and entire system after 10m (600s)
INACTIVITY_TIME = int(os.environ.get("INACTIVITY_TIME", 600))

def parse_color_setting(color_str: str) -> dict:
    """Parse color string into RGB tuple and Hex string."""
    name = (color_str or "WHITE").strip().upper()
    presets = {
        "WHITE": (255, 255, 255, "#ffffff"),
        "WARM": (255, 214, 164, "#ffd6a4"),
        "WARM_WHITE": (255, 214, 164, "#ffd6a4"),
        "YELLOW": (255, 234, 0, "#ffea00"),
        "RED": (239, 68, 68, "#ef4444"),
        "GREEN": (16, 185, 129, "#10b981"),
        "BLUE": (59, 130, 246, "#3b82f6"),
    }
    if name in presets:
        r, g, b, h = presets[name]
        return {"r": r, "g": g, "b": b, "hex": h, "name": name}
    
    # Try parsing hex if formatted like #RRGGBB or RRGGBB
    clean = name.replace("#", "")
    if len(clean) == 6:
        try:
            r = int(clean[0:2], 16)
            g = int(clean[2:4], 16)
            b = int(clean[4:6], 16)
            return {"r": r, "g": g, "b": b, "hex": f"#{clean.lower()}", "name": f"#{clean}"}
        except ValueError:
            pass
    return {"r": 255, "g": 255, "b": 255, "hex": "#ffffff", "name": "WHITE"}

def parse_resolution_setting(res_str: str) -> str:
    """Normalize resolution setting to lowercase format (e.g. 1920x1080, 3840x2160, 1280x720)."""
    val = (res_str or "1920x1080").strip().lower()
    mapping = {
        "ultra": "1920x1080",
        "high": "1280x720",
        "medium": "640x480",
        "low": "320x240"
    }
    return mapping.get(val, val)

def parse_anti_flicker_setting(flicker_str: str) -> tuple[int, int]:
    """Map anti-flicker frequency to (hz, v4l2_code). 0=off, 1=50Hz, 2=60Hz."""
    val = str(flicker_str or "50").strip().lower()
    if val in ("50", "50hz"):
        return (50, 1)
    elif val in ("60", "60hz"):
        return (60, 2)
    return (0, 0)

def get_app_defaults() -> dict:
    """Build dictionary of environment-defined application defaults for frontend."""
    col = parse_color_setting(DEFAULT_LED_COLOR)
    res_wh = parse_resolution_setting(DEFAULT_RESOLUTION)
    hz, code = parse_anti_flicker_setting(ANTI_FLICKER)
    return {
        "anti_flicker": hz,
        "anti_flicker_code": code,
        "led_brightness": LED_BRIGHTNESS,
        "default_led_color": DEFAULT_LED_COLOR,
        "led_color_rgb": [col["r"], col["g"], col["b"]],
        "led_color_hex": col["hex"],
        "led_auto_off": LED_AUTO_OFF,
        "inactivity_time": INACTIVITY_TIME,
        "led_length": LED_LENGTH,
        "default_resolution": res_wh,
        "default_device_cam_fit": DEFAULT_DEVICE_CAM_FIT,
        "default_stream_engine": DEFAULT_STREAM_ENGINE,
        "data_dir": str(DATA_DIR),
        "projects_dir": str(PROJECTS_DIR),
        "ocr_train_config": get_train_config(),
    }



BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

def resolve_storage_path(env_var: str, default_rel: str) -> Path:
    raw = os.environ.get(env_var, "").strip()
    if raw:
        if os.name == "nt" and (raw.startswith("/app") or raw.startswith("\\app")):
            rel_part = raw.replace("/app/", "").replace("/app", "").replace("\\app\\", "").replace("\\app", "").strip("/\\")
            return (BASE_DIR / rel_part) if rel_part else (BASE_DIR / default_rel)
        return Path(raw)
    return BASE_DIR / default_rel

# Persistent Volume directories (Images, ML Models, and Tesseract OCR datasets)
DATA_DIR = resolve_storage_path("DATA_DIR", "data")
IMAGES_DIR = resolve_storage_path("IMAGES_DIR", "data/images")
PROJECTS_DIR = resolve_storage_path("PROJECTS_DIR", "data/projects")
MODELS_DIR = resolve_storage_path("MODELS_DIR", "data/models")
TESSDATA_DIR = resolve_storage_path("TESSDATA_DIR", "data/tessdata")

# Ensure directories exist safely
for d in (TEMPLATES_DIR, STATIC_DIR / "css", STATIC_DIR / "js", DATA_DIR, IMAGES_DIR, MODELS_DIR, TESSDATA_DIR, PROJECTS_DIR):
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("frontend_app")

# Silence noisy external libraries (httpx, httpcore)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Suppress spammy polling endpoints in uvicorn access logs
class EndpointLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(ep in msg for ep in ("/api/status", "/api/heartbeat", "/api/ping"))

logging.getLogger("uvicorn.access").addFilter(EndpointLogFilter())

def sync_system_tessdata():
    """Ensure traineddata models exist in TESSDATA_DIR by copying container system models if empty."""
    try:
        if not TESSDATA_DIR.exists():
            TESSDATA_DIR.mkdir(parents=True, exist_ok=True)
        if not any(TESSDATA_DIR.glob("*.traineddata")):
            system_dirs = [
                Path("/usr/share/tesseract-ocr/5/tessdata"),
                Path("/usr/share/tesseract-ocr/4.00/tessdata"),
                Path("/usr/share/tessdata"),
                Path(r"C:\Program Files\Tesseract-OCR\tessdata")
            ]
            import shutil
            for s_dir in system_dirs:
                if s_dir.exists() and s_dir.is_dir():
                    copied = 0
                    for t_file in s_dir.glob("*.traineddata"):
                        dest = TESSDATA_DIR / t_file.name
                        if not dest.exists():
                            shutil.copy2(t_file, dest)
                            copied += 1
                    if copied > 0:
                        logger.info("Auto-populated %d Tesseract model(s) from %s to %s", copied, s_dir, TESSDATA_DIR)
                    break
    except Exception as e:
        logger.debug("Tessdata sync notice: %s", e)

sync_system_tessdata()

# Serializer for secure signed session cookies
serializer = URLSafeTimedSerializer(SECRET_KEY)
COOKIE_NAME = "lumen_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

app = FastAPI(title="LumenLux-Zero Controller", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.middleware("http")
async def ip_whitelist_middleware(request: Request, call_next):
    # Allow static assets and favicon so styling and icons load cleanly
    if request.url.path.startswith("/static/") or request.url.path == "/favicon.ico":
        return await call_next(request)

    client_ip = get_client_ip(request)
    if not is_ip_allowed(client_ip):
        logger.warning(
            "[AKSES DITOLAK] IP '%s' mencoba mengakses '%s' (Di luar subnet LAN)",
            client_ip, request.url.path
        )
        if request.url.path.startswith("/api/") or request.url.path == "/stream":
            return JSONResponse(
                status_code=403,
                content={
                    "status": "forbidden",
                    "error": "Akses Aplikasi Ditolak",
                    "detail": "Terdeteksi Menggunakan VPN atau Jaringan WAN, silahkan gunakan jaringan LAN",
                    "ip_terdeteksi": client_ip,
                    "footer": "Akses hanya diperbolehkan melalui jaringan internal/LAN"
                }
            )

        return templates.TemplateResponse(
            request=request,
            name="blocked.html",
            context={
                "client_ip": client_ip
            },
            status_code=403
        )

    return await call_next(request)

# Shared HTTPX client with connection pooling and timeouts
backend_client: Optional[httpx.AsyncClient] = None

# Inactivity state tracking (Stage 1: LED auto-off, Stage 2: Full system sleep)
last_activity_time = time.time()
last_client_seen_time = time.time()
led_auto_off_done = False
inactivity_shut_off = False
watchdog_task: Optional[asyncio.Task] = None
CLIENT_PRESENCE_TIMEOUT = 30.0  # seconds without client communication before considering 0 clients connected

def record_activity():
    """Register genuine user interaction timestamp (resets idle countdown and client presence)."""
    global last_activity_time, last_client_seen_time, led_auto_off_done, inactivity_shut_off
    now = time.time()
    last_activity_time = now
    last_client_seen_time = now
    led_auto_off_done = False
    inactivity_shut_off = False

def touch_client_presence():
    """Register that a web browser client is actively connected/viewing without resetting user idle timer."""
    global last_client_seen_time
    last_client_seen_time = time.time()

def has_active_web_clients() -> bool:
    """Return True if any browser web client has communicated within CLIENT_PRESENCE_TIMEOUT."""
    return (time.time() - last_client_seen_time) <= CLIENT_PRESENCE_TIMEOUT


async def inactivity_watchdog_loop():
    """
    Background monitor with two distinct timeout stages:
    1. At idle >= LED_AUTO_OFF (2 mins / 120s): Automatically turn off WS2812B LEDs only.
       The camera and live video stream stay active!
    2. At idle >= INACTIVITY_TIME (10 mins / 600s): Automatically power off camera and all hardware (complete sleep).
    3. If NO active web client is connected for > CLIENT_PRESENCE_TIMEOUT (30s):
       Cease sending heartbeats to Raspberry Pi. If hardware was left on, safely power it down.
    """
    global last_activity_time, last_client_seen_time, led_auto_off_done, inactivity_shut_off
    logger.info(
        "Inactivity watchdog active. LED auto-off: %d seconds (%d mins) | Full sleep: %d seconds (%d mins).",
        LED_AUTO_OFF, LED_AUTO_OFF // 60,
        INACTIVITY_TIME, INACTIVITY_TIME // 60
    )
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            idle = now - last_activity_time
            client_idle = now - last_client_seen_time
            active_clients = has_active_web_clients()

            # Scenario A: When no web clients are connected, pause sending heartbeats to Raspberry Pi
            if not active_clients:
                continue

            # Scenario B: Stage 2 - Turn off camera and entire system after 10 minutes of user idle
            if idle >= INACTIVITY_TIME:
                if not inactivity_shut_off:
                    logger.warning(
                        "[FULL INACTIVITY TIMEOUT] No user interaction for %d seconds. Powering down camera and all hardware on %s...",
                        int(idle), BECKEND_RPI_IP
                    )
                    inactivity_shut_off = True
                    if backend_client:
                        try:
                            await backend_client.post("/api/camera/toggle", json={"enabled": False}, timeout=4.0)
                        except Exception as e:
                            logger.error("Failed to disable camera on full inactivity: %s", e)
                        try:
                            await backend_client.post("/api/lights", json={"power": False}, timeout=4.0)
                        except Exception as e:
                            logger.error("Failed to turn off lights on full inactivity: %s", e)
                    logger.info("[FULL INACTIVITY TIMEOUT] Hardware successfully powered down.")
                # After 10m inactivity timeout has shut off hardware, STOP pinging backend watchdog!
                continue

            # Only ping backend watchdog when an active web client is present AND system is not in sleep
            if backend_client and not inactivity_shut_off:
                try:
                    await backend_client.post("/api/heartbeat", timeout=2.0)
                except Exception:
                    pass

            # Stage 1: Turn off WS2812B lights after 2 minutes of idle (Camera remains ON)
            if idle >= LED_AUTO_OFF and not led_auto_off_done:
                logger.info(
                    "[LED AUTO-OFF] No user activity for %d seconds. Turning off WS2812B LEDs (Camera stream stays ACTIVE).",
                    int(idle)
                )
                led_auto_off_done = True
                if backend_client:
                    try:
                        await backend_client.post("/api/lights", json={"power": False}, timeout=4.0)
                    except Exception as e:
                        logger.error("Failed to turn off lights on LED auto-off: %s", e)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Unexpected error in inactivity watchdog: %s", e)



def ensure_go2rtc_config():
    """Ensure go2rtc.yaml points to current BECKEND_RPI_IP and BECKEND_RPI_PORT."""
    try:
        yaml_path = BASE_DIR / "go2rtc.yaml"
        rpi_stream = f"http://{BECKEND_RPI_IP}:{BECKEND_RPI_PORT}/api/camera/stream"
        content = f"""# Auto-generated by LumenLux-Zero Frontend
log:
  level: "info"

api:
  listen: ":1984"
  origin: "*"

webrtc:
  listen: ":8557"
  candidates:
    - stun:8557

ffmpeg:
  bin: "ffmpeg"

streams:
  chamber:
    - "ffmpeg:{rpi_stream}#video=h264"
    - "{rpi_stream}"
  chamber_mjpeg:
    - "{rpi_stream}"
"""
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Synchronized go2rtc.yaml with target backend: %s:%d", BECKEND_RPI_IP, BECKEND_RPI_PORT)
    except Exception as e:
        logger.debug("go2rtc.yaml sync notice: %s", e)


@app.on_event("startup")
async def startup_event():
    global backend_client, watchdog_task
    ensure_go2rtc_config()
    backend_client = httpx.AsyncClient(
        base_url=f"http://{BECKEND_RPI_IP}:{BECKEND_RPI_PORT}",
        timeout=httpx.Timeout(10.0, connect=5.0, read=60.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=15)
    )
    logger.info("Frontend initialized. Target backend: http://%s:%d", BECKEND_RPI_IP, BECKEND_RPI_PORT)
    record_activity()
    watchdog_task = asyncio.create_task(inactivity_watchdog_loop())

    # Ensure camera is enabled and synchronized on boot, and lights start OFF
    try:
        await backend_client.post("/api/lights", json={"power": False}, timeout=3.0)
        res_norm = parse_resolution_setting(DEFAULT_RESOLUTION)
        rw, rh = map(int, res_norm.split("x"))
        await backend_client.post("/api/camera/select", json={"width": rw, "height": rh, "fps": 30}, timeout=3.0)
        await backend_client.post("/api/camera/toggle", json={"enabled": True}, timeout=3.0)
    except Exception:
        pass


@app.on_event("shutdown")
async def shutdown_event():
    global backend_client, watchdog_task
    if watchdog_task:
        watchdog_task.cancel()
    if backend_client:
        try:
            logger.info("Server shutting down: sending immediate power-off to Raspberry Pi hardware...")
            await backend_client.post("/api/camera/toggle", json={"enabled": False}, timeout=1.5)
            await backend_client.post("/api/lights", json={"power": False}, timeout=1.5)
        except Exception as e:
            logger.debug("Shutdown hardware request failed: %s", e)
        await backend_client.aclose()


def get_current_user(request: Request) -> Optional[str]:
    """Verify session cookie or signed token and return username if valid."""
    token = request.cookies.get(COOKIE_NAME) or request.query_params.get("token")
    if not token:
        return None
    try:
        username = serializer.loads(token, max_age=SESSION_MAX_AGE)
        return username
    except (BadSignature, SignatureExpired):
        return None


def require_auth(request: Request):
    """Dependency that redirects to login if unauthenticated."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"}
        )
    touch_client_presence()
    return user


# --- AUTHENTICATION ROUTES ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": None,
            "rpi_ip": BECKEND_RPI_IP
        }
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    if username == USER_WEB and password == USER_PASSWORD:
        token = serializer.dumps(username)
        record_activity()
        response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
            secure=False
        )
        logger.info("User '%s' logged in successfully.", username)
        return response

    logger.warning("Failed login attempt for user: '%s'", username)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": "Invalid username or password. Please try again.",
            "rpi_ip": BECKEND_RPI_IP
        },
        status_code=401
    )


@app.get("/logout")
async def logout():
    # Power off camera and lights on explicit logout
    try:
        if backend_client:
            await backend_client.post("/api/camera/toggle", json={"enabled": False}, timeout=3.0)
            await backend_client.post("/api/lights", json={"power": False}, timeout=3.0)
            logger.info("User logged out: turned off camera and lights.")
    except Exception as e:
        logger.warning("Could not power off on logout: %s", e)

    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_route():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


# --- MAIN DASHBOARD ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_auth)):
    initial_cameras = []
    try:
        if backend_client:
            res = await backend_client.get("/api/cameras", timeout=2.5)
            if res.status_code == 200:
                initial_cameras = res.json().get("cameras", [])
    except Exception as e:
        logger.debug("Could not pre-fetch cameras for initial render: %s", e)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "rpi_ip": BECKEND_RPI_IP,
            "rpi_port": BECKEND_RPI_PORT,
            "inactivity_time": INACTIVITY_TIME,
            "app_defaults": get_app_defaults(),
            "initial_cameras": initial_cameras
        }
    )


# --- CONFIG & OCR AUTHENTICATION APIS ---

@app.get("/api/config/defaults")
async def api_config_defaults(user: str = Depends(require_auth)):
    """Return environment-configured defaults for frontend controls."""
    return get_app_defaults()


@app.post("/api/ocr/verify")
async def verify_ocr_password(payload: dict, user: str = Depends(require_auth)):
    """Verify security password for OCR feature access."""
    record_activity()
    provided = str(payload.get("password", "")).strip()
    if secrets.compare_digest(provided, OCR_PASSWORD):
        logger.info("OCR password successfully verified for user '%s'", user)
        return {"success": True, "message": "OCR access granted"}
    
    logger.warning("Invalid OCR password attempt for user '%s'", user)
    return JSONResponse(
        status_code=401,
        content={"success": False, "error": "Invalid OCR password. Access denied."}
    )


# --- OCR STUDIO & CALIBRATION PROJECTS APIS ---

PROJECTS_DIR = IMAGES_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Also check legacy DATA_DIR / "projects" and migrate any existing folders if present
LEGACY_PROJECTS_DIR = DATA_DIR / "projects"
if LEGACY_PROJECTS_DIR.exists() and LEGACY_PROJECTS_DIR != PROJECTS_DIR:
    try:
        import shutil
        for legacy_p in LEGACY_PROJECTS_DIR.iterdir():
            if legacy_p.is_dir():
                target_dest = PROJECTS_DIR / legacy_p.name
                if not target_dest.exists():
                    shutil.move(str(legacy_p), str(target_dest))
                    logger.info("Migrated legacy OCR project '%s' to %s", legacy_p.name, PROJECTS_DIR)
    except Exception as e:
        logger.debug("Legacy project migration notice: %s", e)

# Banned characters on filesystem directory naming across Linux, Windows, and NFS/Samba
BANNED_DIR_CHARS = set('<>:"/\\|?*')

def validate_project_name(name: str) -> Optional[str]:
    """Check if project name contains illegal filesystem characters. Returns error string if invalid."""
    if not name or not name.strip():
        return "Project name cannot be empty."
    name_stripped = name.strip()
    if len(name_stripped) > 80:
        return "Project name cannot exceed 80 characters."
    if any(c in BANNED_DIR_CHARS for c in name_stripped):
        return 'Project name contains forbidden characters (: < > " / \\ | ? *). Please use clean characters.'
    if any(ord(c) < 32 for c in name_stripped):
        return "Project name contains invalid control characters."
    if name_stripped.startswith(".") or name_stripped.endswith("."):
        return "Project name cannot start or end with a dot (.)."
    if ".." in name_stripped:
        return "Project name cannot contain consecutive dots (..)."
    return None

def sanitize_project_name(raw_name: str) -> str:
    """Safe folder name for project ID on disk."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(raw_name)).strip().strip('.')
    cleaned = re.sub(r'\s+', '_', cleaned)
    safe = re.sub(r'[^a-zA-Z0-9_\.-]', '', cleaned).strip('._')
    return safe or 'Project_01'

def resolve_project_dir(project_id: str) -> Path:
    """Locate project directory under PROJECTS_DIR, legacy location, or workspace root (e.g. Proj_Test)."""
    safe_name = sanitize_project_name(project_id)
    p = PROJECTS_DIR / safe_name
    if not p.exists() and (PROJECTS_DIR / str(project_id)).exists():
        return PROJECTS_DIR / str(project_id)
    if not p.exists() and LEGACY_PROJECTS_DIR.exists() and (LEGACY_PROJECTS_DIR / safe_name).exists():
        return LEGACY_PROJECTS_DIR / safe_name
    if not p.exists() and (BASE_DIR.parent / safe_name).is_dir():
        return BASE_DIR.parent / safe_name
    return p

def get_project_metadata(proj_dir: Path) -> dict:
    meta_file = proj_dir / "project.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "name": proj_dir.name,
        "created_at": proj_dir.stat().st_ctime if proj_dir.exists() else time.time(),
        "boxes": [
            {"id": "box_std", "label": "Standard", "x": 0.15, "y": 0.20, "w": 0.38, "h": 0.32, "target": "device", "status": "candidate"},
            {"id": "box_01", "label": "Thermo 01", "x": 0.12, "y": 0.20, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_02", "label": "Thermo 02", "x": 0.38, "y": 0.16, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_03", "label": "Thermo 03", "x": 0.64, "y": 0.22, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_04", "label": "Thermo 04", "x": 0.24, "y": 0.52, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_05", "label": "Thermo 05", "x": 0.52, "y": 0.54, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"}
        ]
    }

def save_project_metadata(proj_dir: Path, data: dict):
    meta_file = proj_dir / "project.json"
    try:
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error("Could not write project metadata: %s", e)


@app.get("/api/ocr/projects")
async def list_ocr_projects(user: str = Depends(require_auth)):
    """List all calibration projects and their snapshot image counts."""
    record_activity()
    projects = []
    seen_ids = set()

    scan_dirs = [PROJECTS_DIR]
    if LEGACY_PROJECTS_DIR.exists() and LEGACY_PROJECTS_DIR != PROJECTS_DIR:
        scan_dirs.append(LEGACY_PROJECTS_DIR)

    for p_dir in scan_dirs:
        if p_dir.exists():
            for p in sorted(p_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if p.is_dir() and p.name not in seen_ids:
                    seen_ids.add(p.name)
                    meta = get_project_metadata(p)
                    snapshots = sorted(
                        [f.name for f in p.glob("*.jpg")] + [f.name for f in p.glob("*.png")],
                        reverse=True
                    )
                    projects.append({
                        "id": p.name,
                        "name": meta.get("name", p.name),
                        "created_at": meta.get("created_at", p.stat().st_ctime),
                        "snapshot_count": len(snapshots),
                        "snapshots": snapshots,
                        "boxes": meta.get("boxes", [])
                    })

    # Also include workspace Proj_Test directory if present
    workspace_test_dir = BASE_DIR.parent / "Proj_Test"
    if workspace_test_dir.is_dir() and "Proj_Test" not in seen_ids:
        seen_ids.add("Proj_Test")
        meta = get_project_metadata(workspace_test_dir)
        snapshots = sorted(
            [f.name for f in workspace_test_dir.glob("*.jpg")] + [f.name for f in workspace_test_dir.glob("*.png")]
        )
        projects.append({
            "id": "Proj_Test",
            "name": meta.get("name", "Proj_Test"),
            "created_at": meta.get("created_at", workspace_test_dir.stat().st_ctime),
            "snapshot_count": len(snapshots),
            "snapshots": snapshots,
            "boxes": meta.get("boxes", [])
        })

    logger.info("Retrieved %d OCR calibration project(s) from %s", len(projects), PROJECTS_DIR)
    return {"status": "ok", "projects": projects}


@app.post("/api/ocr/projects")
async def create_ocr_project(payload: dict, user: str = Depends(require_auth)):
    """Create a new OCR calibration project."""
    record_activity()
    raw_name = str(payload.get("name", "")).strip()

    val_err = validate_project_name(raw_name)
    if val_err:
        raise HTTPException(status_code=400, detail=val_err)

    safe_name = sanitize_project_name(raw_name)
    proj_dir = PROJECTS_DIR / safe_name
    proj_dir.mkdir(parents=True, exist_ok=True)

    meta = get_project_metadata(proj_dir)
    meta["name"] = raw_name
    meta["created_at"] = time.time()
    save_project_metadata(proj_dir, meta)

    logger.info("Created new OCR Project '%s' (%s)", raw_name, safe_name)
    return {"status": "ok", "project": {"id": safe_name, "name": raw_name, "snapshot_count": 0, "snapshots": [], "boxes": meta.get("boxes", [])}}



@app.delete("/api/ocr/projects/{project_id}")
async def delete_ocr_project(project_id: str, user: str = Depends(require_auth)):
    """Delete a calibration project and its snapshots."""
    record_activity()
    proj_dir = resolve_project_dir(project_id)
    if proj_dir.exists() and proj_dir.is_dir():
        import shutil
        shutil.rmtree(proj_dir, ignore_errors=True)
        logger.info("Deleted OCR Project '%s'", proj_dir.name)
        return {"status": "ok", "deleted": proj_dir.name}
    raise HTTPException(status_code=404, detail="Project not found")


@app.get("/api/ocr/projects/{project_id}")
async def get_ocr_project(project_id: str, user: str = Depends(require_auth)):
    """Retrieve details, bounding boxes, and snapshot list for a project."""
    record_activity()
    proj_dir = resolve_project_dir(project_id)
    if not proj_dir.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    
    meta = get_project_metadata(proj_dir)
    snapshots = sorted(
        [f.name for f in proj_dir.glob("*.jpg")] + [f.name for f in proj_dir.glob("*.png")],
        reverse=True
    )
    return {
        "status": "ok",
        "project": {
            "id": proj_dir.name,
            "name": meta.get("name", proj_dir.name),
            "created_at": meta.get("created_at", proj_dir.stat().st_ctime),
            "snapshot_count": len(snapshots),
            "snapshots": snapshots,
            "boxes": meta.get("boxes", [])
        }
    }


@app.post("/api/ocr/projects/{project_id}/boxes")
async def save_ocr_boxes(project_id: str, payload: dict, user: str = Depends(require_auth)):
    """Save user-customized bounding boxes (ROIs) for the project."""
    record_activity()
    proj_dir = resolve_project_dir(project_id)
    if not proj_dir.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    
    boxes = payload.get("boxes", [])
    meta = get_project_metadata(proj_dir)
    meta["boxes"] = boxes
    save_project_metadata(proj_dir, meta)
    return {"status": "ok", "boxes": boxes}


@app.post("/api/ocr/projects/{project_id}/save_snapshot")
async def save_project_snapshot(project_id: str, payload: dict, user: str = Depends(require_auth)):
    """Save captured snapshot image directly to this project's directory."""
    record_activity()
    proj_dir = resolve_project_dir(project_id)
    proj_dir.mkdir(parents=True, exist_ok=True)

    img_b64 = payload.get("image_base64", "")
    filename = payload.get("filename", "")
    if not filename:
        filename = f"LumenLux_{time.strftime('%d_%B_%Y-%H-%M-%S')}.jpg"

    if "," in img_b64:
        img_b64 = img_b64.split(",", 1)[1]

    try:
        data = base64.b64decode(img_b64)
        target_path = proj_dir / filename
        with open(target_path, "wb") as f:
            f.write(data)
        logger.info("Saved snapshot %s to project '%s'", filename, proj_dir.name)
        return {"status": "ok", "filename": filename, "path": str(target_path)}
    except Exception as e:
        logger.error("Failed to save snapshot to project: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/ocr/projects/{project_id}/detect")
async def auto_detect_boxes(project_id: str, payload: dict, user: str = Depends(require_auth)):
    """
    Auto-detect digital thermometers and analyze if frame contains analog dial meters.
    Accepts base64 image or falls back to live camera snapshot.
    """
    record_activity()
    proj_dir = resolve_project_dir(project_id)

    img_b64 = payload.get("image_base64")
    raw_bytes = None
    if img_b64:
        if "," in img_b64:
            img_b64 = img_b64.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(img_b64)
        except Exception:
            pass

    if not raw_bytes and backend_client:
        try:
            res = await backend_client.get("/api/camera/snapshot", timeout=4.0)
            if res.status_code == 200:
                raw_bytes = res.content
        except Exception:
            pass

    if not raw_bytes:
        # Check if project has an existing snapshot
        snaps = list(proj_dir.glob("*.jpg")) + list(proj_dir.glob("*.png"))
        if snaps:
            with open(snaps[-1], "rb") as f:
                raw_bytes = f.read()

    if not raw_bytes:
        return {"status": "error", "message": "No image available for detection"}

    detection_result = await asyncio.to_thread(detect_thermometers_in_image, raw_bytes)
    boxes = detection_result.get("boxes", [])
    if boxes and proj_dir.exists():
        meta = get_project_metadata(proj_dir)
        meta["boxes"] = boxes
        save_project_metadata(proj_dir, meta)
    return {"status": "ok", "detected_count": len(boxes), **detection_result}






# --- BACKEND PROXY & HEAVY LIFTING APIS ---


@app.post("/api/heartbeat")
async def user_heartbeat(user: str = Depends(require_auth)):
    """Keep-alive ping from active browser."""
    record_activity()
    if backend_client and not inactivity_shut_off:
        try:
            await backend_client.post("/api/heartbeat", timeout=2.0)
        except Exception:
            pass
    idle = time.time() - last_activity_time
    return {
        "status": "ok",
        "idle_seconds": int(idle),
        "led_auto_off": LED_AUTO_OFF,
        "inactivity_time": INACTIVITY_TIME,
        "remaining_led_seconds": max(0, int(LED_AUTO_OFF - idle)),
        "remaining_seconds": max(0, int(INACTIVITY_TIME - idle))
    }


@app.get("/api/ping")
async def ping_network():
    """Ultra-lightweight ping endpoint for genuine network RTT latency measurement."""
    return {"status": "pong", "time": time.time()}


_status_cache = {"data": None, "timestamp": 0.0, "fail_count": 0}


@app.get("/api/status")
async def proxy_status(user: str = Depends(require_auth)):
    now = time.time()
    if _status_cache["data"] and (now - _status_cache["timestamp"] < 1.2):
        return _status_cache["data"]

    last_err = None
    for attempt in range(2):
        try:
            res = await backend_client.get("/api/status", timeout=3.5, headers={"Connection": "close"})
            if res.status_code == 200:
                data = res.json()
                _status_cache["data"] = data
                _status_cache["timestamp"] = time.time()
                _status_cache["fail_count"] = 0
                return data
        except Exception as e:
            last_err = e
            if attempt == 0:
                await asyncio.sleep(0.12)

    _status_cache["fail_count"] = _status_cache.get("fail_count", 0) + 1
    # Grace period: prevent false DISCONNECTED toggling on transient HTTP/1.0 socket close or Pi busyness
    if _status_cache["data"] and _status_cache["fail_count"] < 3 and (now - _status_cache["timestamp"] < 15.0):
        return _status_cache["data"]

    return {
        "status": "offline",
        "error": f"Raspberry Pi ({BECKEND_RPI_IP}) unreachable: {str(last_err)}",
        "rpi_ip": BECKEND_RPI_IP,
        "rpi_port": BECKEND_RPI_PORT,
        "timestamp": time.time(),
        "system": None,
        "lights": None,
        "camera": None
    }


@app.get("/api/cameras")
async def proxy_cameras(user: str = Depends(require_auth)):
    try:
        res = await backend_client.get("/api/cameras", timeout=4.0)
        return res.json()
    except Exception as e:
        return {"cameras": [], "error": str(e), "active_id": None}


@app.post("/api/camera/select")
async def proxy_camera_select(request: Request, user: str = Depends(require_auth)):
    record_activity()
    payload = await request.json()
    _status_cache["data"] = None
    try:
        res = await backend_client.post("/api/camera/select", json=payload, timeout=5.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.post("/api/camera/toggle")
async def proxy_camera_toggle(request: Request, user: str = Depends(require_auth)):
    record_activity()
    payload = await request.json()
    _status_cache["data"] = None
    try:
        res = await backend_client.post("/api/camera/toggle", json=payload, timeout=4.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.post("/api/camera/control")
async def proxy_camera_control(request: Request, user: str = Depends(require_auth)):
    record_activity()
    payload = await request.json()
    _status_cache["data"] = None
    try:
        res = await backend_client.post("/api/camera/control", json=payload, timeout=4.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.post("/api/camera/reset_defaults")
async def proxy_camera_reset_defaults(request: Request, user: str = Depends(require_auth)):
    record_activity()
    payload = await request.json()
    _status_cache["data"] = None
    try:
        res = await backend_client.post("/api/camera/reset_defaults", json=payload, timeout=4.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.get("/api/lights")
async def proxy_lights_get(user: str = Depends(require_auth)):
    try:
        res = await backend_client.get("/api/lights", timeout=4.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.post("/api/lights")
async def proxy_lights_post(request: Request, user: str = Depends(require_auth)):
    record_activity()
    payload = await request.json()
    _status_cache["data"] = None
    try:
        res = await backend_client.post("/api/lights", json=payload, timeout=4.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.get("/api/camera/snapshot")
async def proxy_snapshot(user: str = Depends(require_auth)):
    try:
        res = await backend_client.get("/api/camera/snapshot", timeout=5.0)
        return Response(content=res.content, media_type="image/jpeg")
    except Exception:
        return Response(status_code=503)


@app.get("/stream")
async def proxy_mjpeg_stream(request: Request):
    """
    Stream MJPEG from Raspberry Pi backend to the client with async chunk forwarding.
    Authenticates via cookie, token, or valid internal referer without 307 redirect loops.
    """
    user = get_current_user(request)
    if not user:
        referer = request.headers.get("referer", "")
        # Allow requests originating from the dashboard
        if not (request.client and request.client.host in ("127.0.0.1", "localhost") or "/login" not in referer and referer):
            return Response(status_code=401)
    async def stream_generator():
        last_activity_touch = 0.0

        # Attempt 1: Direct Raspberry Pi MJPEG (Lowest latency <5ms, native camera frames, 100% compatible)
        rpi_mjpeg = f"http://{BECKEND_RPI_IP}:{BECKEND_RPI_PORT}/api/camera/stream"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.5, read=None, write=None, pool=None)) as client:
                async with client.stream("GET", rpi_mjpeg) as resp:
                    if resp.status_code == 200:
                        async for chunk in resp.aiter_raw():
                            if await request.is_disconnected():
                                break
                            now = time.time()
                            if now - last_activity_touch > 5.0:
                                last_activity_touch = now
                                touch_client_presence()
                            yield chunk
                        return
                    else:
                        logger.warning("Direct Pi MJPEG stream returned status %d, falling back to go2rtc", resp.status_code)
        except Exception as e:
            logger.debug("Direct Pi MJPEG stream notice: %s", e)

        # Attempt 2: go2rtc multiplexed MJPEG fallback
        go2rtc_mjpeg = f"http://{GO2RTC_HOST}/api/stream.mjpeg?src=chamber"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=2.5, read=None, write=None, pool=None)) as client:
                async with client.stream("GET", go2rtc_mjpeg) as resp:
                    if resp.status_code == 200:
                        async for chunk in resp.aiter_raw():
                            if await request.is_disconnected():
                                break
                            now = time.time()
                            if now - last_activity_touch > 5.0:
                                last_activity_touch = now
                                touch_client_presence()
                            yield chunk
                        return
        except Exception as e:
            logger.warning("Stream proxy connection issue: %s", e)

    return StreamingResponse(
        stream_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no"
        }
    )


# ==============================================================================
# go2rtc H.264 WebRTC & MSE STREAMING PROXY (ULTRA LOW BANDWIDTH ~1.5 - 2 Mbps)
# ==============================================================================
GO2RTC_HOST = os.environ.get("GO2RTC_HOST", "10.101.27.3:1984")

@app.api_route("/api/webrtc", methods=["GET", "POST", "OPTIONS"])
async def proxy_webrtc(request: Request):
    """
    Exchange WebRTC SDP offer/answer with go2rtc for ultra-low latency H.264 video.
    """
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS"})

    target = f"http://{GO2RTC_HOST}/api/webrtc?src=chamber"
    try:
        if request.method == "POST":
            raw_body = await request.body()
            body_str = raw_body.decode("utf-8", errors="ignore").strip()

            # Handle form-encoded "data=..." or "sdp=..."
            if body_str.startswith("data=") or body_str.startswith("sdp="):
                from urllib.parse import unquote_plus
                _, _, val = body_str.partition("=")
                body_str = unquote_plus(val).strip()

            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.post(
                    target,
                    content=body_str.encode("utf-8"),
                    headers={"Content-Type": "application/sdp"}
                )
                if res.status_code != 200:
                    logger.warning("[WebRTC Proxy] go2rtc /api/webrtc returned status %d: %s", res.status_code, res.text[:200])
                return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type", "application/sdp"))
        else:
            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.get(target)
                return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type", "text/plain"))
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"go2rtc offline: {str(e)}"}, status_code=503)


@app.get("/api/stream.mp4")
async def proxy_fmp4_stream(request: Request):
    """
    Direct fragmented MP4 (fMP4) H.264 stream for native HTML5 video tags.
    """
    target = f"http://{GO2RTC_HOST}/api/stream.mp4?src=chamber"
    try:
        async def mp4_generator():
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=4.0, read=None, write=None, pool=None)) as client:
                async with client.stream("GET", target) as resp:
                    async for chunk in resp.aiter_raw():
                        if await request.is_disconnected():
                            break
                        yield chunk

        return StreamingResponse(
            mp4_generator(),
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Accel-Buffering": "no"
            }
        )
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=503)


@app.websocket("/api/ws")
async def proxy_go2rtc_ws(websocket: WebSocket):
    """
    Bidirectional WebSocket proxy to go2rtc for MSE H.264 streaming.
    Passes directly through Nginx Proxy Manager on port 443 with zero extra ports.
    """
    await websocket.accept()
    import websockets
    target_ws = f"ws://{GO2RTC_HOST}/api/ws?src=chamber"
    try:
        async with websockets.connect(target_ws) as backend_ws:
            async def forward_to_client():
                async for message in backend_ws:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            async def forward_to_backend():
                while True:
                    data = await websocket.receive()
                    if "text" in data and data["text"]:
                        await backend_ws.send(data["text"])
                    elif "bytes" in data and data["bytes"]:
                        await backend_ws.send(data["bytes"])

            await asyncio.gather(forward_to_client(), forward_to_backend())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.debug("WebSocket go2rtc proxy notice: %s", e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ==============================================================================
# OCR STUDIO ROI TESTING, EXCEL EXPORT & ASSET SERVING
# ==============================================================================



@app.post("/api/ocr/projects/{project_id}/test_box")
async def test_roi_box_ocr(project_id: str, request: Request, user: str = Depends(require_auth)):
    """
    Instantly test perspective unwarping and OCR recognition for a specific ROI bounding box
    on the latest snapshot or live camera frame. Returns crop thumbnail base64 and recognized reading.
    """
    record_activity()
    proj_dir = resolve_project_dir(project_id)
    payload = await request.json()
    box = payload.get("box", {})
    image_b64 = payload.get("image_base64")

    raw_bytes = None
    if image_b64:
        try:
            if "," in image_b64:
                image_b64 = image_b64.split(",", 1)[1]
            raw_bytes = base64.b64decode(image_b64)
        except Exception:
            pass

    if not raw_bytes:
        snaps = sorted(
            list(proj_dir.glob("*.jpg")) + list(proj_dir.glob("*.png")),
            key=lambda p: p.stat().st_mtime
        )
        if snaps:
            try:
                with open(snaps[-1], "rb") as f:
                    raw_bytes = f.read()
            except Exception:
                pass

    if not raw_bytes and backend_client:
        try:
            res = await backend_client.get("/api/camera/snapshot", timeout=4.0)
            if res.status_code == 200:
                raw_bytes = res.content
        except Exception:
            pass

    if not raw_bytes:
        return JSONResponse({"status": "error", "message": "No snapshot or camera image available to test."}, status_code=400)

    try:
        pil_img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        # Live canvas test frames sent from the UI are always single-feed full viewport frames
        is_single = bool(image_b64)
        ocr_res = await asyncio.to_thread(perform_display_ocr, pil_img, box, is_single)

        crop_b64 = ""
        if ocr_res.get("crop_bytes"):
            crop_b64 = "data:image/jpeg;base64," + base64.b64encode(ocr_res["crop_bytes"]).decode("ascii")

        return JSONResponse({
            "status": "ok",
            "ocr_status": ocr_res.get("status", "error"),
            "text": ocr_res.get("text", "N/A"),
            "value": ocr_res.get("value"),
            "engine": ocr_res.get("engine", "tesseract"),
            "confidence": ocr_res.get("confidence", 0.0),
            "display_type": ocr_res.get("display_type", "standard_lcd"),
            "model_version": ocr_res.get("model_version", "v6_small"),
            "crop_base64": crop_b64
        })
    except Exception as e:
        logger.error("Error testing ROI box OCR: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/ocr/projects/{project_id}/export")
async def export_project_excel(project_id: str, user: str = Depends(require_auth)):
    """Generate Excel (.xlsx) calibration report with embedded snapshot images and OCR readouts."""
    proj_dir = resolve_project_dir(project_id)
    if not proj_dir.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")

    meta_file = proj_dir / "project.json"
    meta = {}
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass

    proj_name = meta.get("name", project_id)
    boxes = meta.get("boxes", [])

    try:
        excel_bytes = await asyncio.to_thread(generate_project_excel, proj_dir, proj_name, boxes)
        filename = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', proj_name)}_Calibration_Report.xlsx"
        return Response(
            content=excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        logger.error("Excel generation failed for project %s: %s", project_id, e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/ocr/projects/{project_id}/snapshots/{filename}")
async def get_project_snapshot(project_id: str, filename: str, user: str = Depends(require_auth)):
    """Serve individual snapshot image from project directory."""
    proj_dir = resolve_project_dir(project_id)
    clean_name = os.path.basename(filename)
    file_path = proj_dir / clean_name
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot file not found")
    return FileResponse(file_path, media_type="image/jpeg")


# ==============================================================================
# OCR STUDIO: PYTORCH CPU 7-SEGMENT CNN TRAINING & LABELING ENDPOINTS
# ==============================================================================

@app.get("/api/ocr/train/stats")
async def api_ocr_train_stats(user: str = Depends(require_auth)):
    """Return current training dataset statistics, class counts (0-9 + blank), and model status."""
    record_activity()
    try:
        stats = await asyncio.to_thread(get_training_dataset_stats)
        return {"status": "ok", **stats}
    except Exception as e:
        logger.error("Error fetching training stats: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/ocr/train/label")
async def api_ocr_train_label(request: Request, user: str = Depends(require_auth)):
    """
    Collect and label an ROI crop image with user-entered ground-truth reading (e.g. '14.6' or '14.954').
    Segments the crop into individual digits and saves them into `<TRAIN_DATA_DIR>/digits/<class>/`.
    """
    record_activity()
    payload = await request.json()
    label_val = str(payload.get("label", "")).strip()
    crop_b64 = payload.get("crop_base64", "")
    image_b64 = payload.get("image_base64", "")
    box = payload.get("box", {})
    project_id = str(payload.get("project_id", "manual")).strip() or "manual"

    if not label_val:
        return JSONResponse({"status": "error", "message": "Please enter the reading value to label (e.g. 14.6)."}, status_code=400)

    pil_crop = None
    if crop_b64:
        try:
            if "," in crop_b64:
                crop_b64 = crop_b64.split(",", 1)[1]
            raw_crop = base64.b64decode(crop_b64)
            pil_crop = Image.open(io.BytesIO(raw_crop)).convert("RGB")
        except Exception as e:
            return JSONResponse({"status": "error", "message": f"Invalid crop image data: {e}"}, status_code=400)
    elif image_b64 and box:
        try:
            if "," in image_b64:
                image_b64 = image_b64.split(",", 1)[1]
            raw_full = base64.b64decode(image_b64)
            pil_full = Image.open(io.BytesIO(raw_full)).convert("RGB")
            pil_crop, _ = await asyncio.to_thread(extract_roi_region, pil_full, box, True)
        except Exception as e:
            return JSONResponse({"status": "error", "message": f"Failed to extract ROI from frame: {e}"}, status_code=400)

    if pil_crop is None:
        return JSONResponse({"status": "error", "message": "No cropped image provided for labeling."}, status_code=400)

    try:
        res = await asyncio.to_thread(save_labeled_roi_crop, pil_crop, label_val, box, project_id)
        return JSONResponse(res)
    except Exception as e:
        logger.error("Error saving labeled ROI crop: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@app.post("/api/ocr/train/seed_project/{project_id}")
async def api_ocr_train_seed_project(project_id: str, user: str = Depends(require_auth)):
    """
    Bootstrap / collect labeled ROI crops from all snapshots in an existing project
    (or Proj_Test fallback) so the user can quickly build 20-30 samples per class.
    """
    record_activity()
    proj_dir = resolve_project_dir(project_id)
    if not proj_dir.is_dir():
        fallback_test = BASE_DIR.parent / "Proj_Test"
        if fallback_test.is_dir():
            proj_dir = fallback_test
        else:
            return JSONResponse({"status": "error", "message": f"Project '{project_id}' not found."}, status_code=404)

    def _seed_worker() -> Dict[str, Any]:
        meta = get_project_metadata(proj_dir)
        boxes = meta.get("boxes", [])
        snaps = sorted(list(proj_dir.glob("*.jpg")) + list(proj_dir.glob("*.png")), key=lambda p: p.stat().st_mtime)
        added_frames = 0
        added_digits = 0
        for snap_p in snaps:
            try:
                pil_im = Image.open(snap_p).convert("RGB")
            except Exception:
                continue
            for b in boxes:
                ocr_res = perform_display_ocr(pil_im, b, is_single_feed=False)
                val = ocr_res.get("value")
                if val is not None and ocr_res.get("crop_bytes"):
                    cam_t = (b.get("camera_type") or b.get("target") or "").lower()
                    lbl_t = str(b.get("label", "")).lower()
                    is_std = (cam_t == "device" or "standard" in lbl_t or "fluke" in lbl_t or "1502" in lbl_t)
                    val_str = f"{val:.3f}" if is_std else f"{val:.1f}"
                    try:
                        crop_im = Image.open(io.BytesIO(ocr_res["crop_bytes"])).convert("RGB")
                        saved = save_labeled_roi_crop(crop_im, val_str, b, proj_dir.name)
                        added_frames += 1
                        added_digits += int(saved.get("digits_saved", 0))
                    except Exception:
                        pass
        return {
            "status": "ok",
            "added_frames": added_frames,
            "added_digits": added_digits,
            "dataset_stats": get_training_dataset_stats()
        }

    try:
        res = await asyncio.to_thread(_seed_worker)
        return JSONResponse(res)
    except Exception as e:
        logger.error("Error seeding training data from project %s: %s", project_id, e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/ocr/projects/{project_id}/batch_crop_box")
async def api_ocr_batch_crop_box(project_id: str, request: Request, user: str = Depends(require_auth)):
    """
    Auto-crop a selected ROI bounding box across ALL (or user-selected) snapshots in a project,
    returning a queue/grid of crops with suggested OCR readings and quality checks.
    """
    record_activity()
    proj_dir = resolve_project_dir(project_id)
    if not proj_dir.is_dir():
        fallback_test = BASE_DIR.parent / "Proj_Test"
        if fallback_test.is_dir():
            proj_dir = fallback_test
        else:
            return JSONResponse({"status": "error", "message": f"Project '{project_id}' not found."}, status_code=404)

    payload = await request.json()
    box = payload.get("box", {})
    selected_filenames = payload.get("filenames", [])
    custom_images = payload.get("custom_images", [])  # Optional client-uploaded folder/files [{filename, image_base64}]

    def _batch_crop_worker() -> Dict[str, Any]:
        all_snaps = sorted(
            list(proj_dir.glob("*.jpg")) + list(proj_dir.glob("*.png")),
            key=lambda p: p.stat().st_mtime
        )
        if selected_filenames:
            sel_set = set(selected_filenames)
            target_snaps = [p for p in all_snaps if p.name in sel_set]
        else:
            target_snaps = all_snaps

        cam_t = (box.get("camera_type") or box.get("target") or "").lower()
        lbl_t = str(box.get("label", "")).lower()
        is_std = (cam_t == "device" or "standard" in lbl_t or "fluke" in lbl_t or "1502" in lbl_t)
        box_decimals = box.get("decimals")

        crops_list = []

        # 1. Process project snapshot files
        for idx, snap_p in enumerate(target_snaps):
            try:
                pil_im = Image.open(snap_p).convert("RGB")
                ocr_res = perform_display_ocr(pil_im, box, is_single_feed=False)
                crop_bytes = ocr_res.get("crop_bytes")
                if not crop_bytes:
                    crop_pil, _ = extract_roi_region(pil_im, box, is_single_feed=False)
                    buf = io.BytesIO()
                    crop_pil.save(buf, format="JPEG", quality=92)
                    crop_bytes = buf.getvalue()
                else:
                    crop_pil = Image.open(io.BytesIO(crop_bytes)).convert("RGB")

                quality = assess_crop_quality(crop_pil)
                val = ocr_res.get("value")
                if val is not None:
                    if box_decimals is not None:
                        sug_str = f"{val:.{int(box_decimals)}f}"
                    else:
                        sug_str = f"{val:.3f}" if is_std else f"{val:.1f}"
                else:
                    sug_str = ""

                mtime = snap_p.stat().st_mtime
                crops_list.append({
                    "index": idx + 1,
                    "filename": snap_p.name,
                    "timestamp": mtime,
                    "date_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    "crop_base64": "data:image/jpeg;base64," + base64.b64encode(crop_bytes).decode("ascii"),
                    "suggested_reading": sug_str,
                    "raw_text": ocr_res.get("text", ""),
                    "confidence": ocr_res.get("confidence", 0.0),
                    "engine": ocr_res.get("engine", ""),
                    "needs_review": bool(ocr_res.get("needs_review", False)),
                    "review_reasons": ocr_res.get("review_reasons", []),
                    "quality": quality
                })
            except Exception as ex:
                logger.warning("Batch crop skipped %s: %s", snap_p.name, ex)

        # 2. Process any custom uploaded local files
        for c_idx, item in enumerate(custom_images):
            try:
                b64 = item.get("image_base64", "")
                fname = item.get("filename", f"Upload_{c_idx + 1}.jpg")
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                raw = base64.b64decode(b64)
                pil_im = Image.open(io.BytesIO(raw)).convert("RGB")
                ocr_res = perform_display_ocr(pil_im, box, is_single_feed=False)
                crop_bytes = ocr_res.get("crop_bytes")
                crop_pil = Image.open(io.BytesIO(crop_bytes)).convert("RGB") if crop_bytes else pil_im
                quality = assess_crop_quality(crop_pil)
                val = ocr_res.get("value")
                sug_str = (f"{val:.3f}" if is_std else f"{val:.1f}") if val is not None else ""
                crops_list.append({
                    "index": len(crops_list) + 1,
                    "filename": fname,
                    "timestamp": time.time(),
                    "date_str": "Uploaded File",
                    "crop_base64": "data:image/jpeg;base64," + base64.b64encode(crop_bytes).decode("ascii") if crop_bytes else "",
                    "suggested_reading": sug_str,
                    "raw_text": ocr_res.get("text", ""),
                    "confidence": ocr_res.get("confidence", 0.0),
                    "engine": ocr_res.get("engine", ""),
                    "needs_review": bool(ocr_res.get("needs_review", False)),
                    "review_reasons": ocr_res.get("review_reasons", []),
                    "quality": quality
                })
            except Exception as ex:
                logger.warning("Custom image batch crop skipped: %s", ex)

        return {
            "status": "ok",
            "project_id": proj_dir.name,
            "box_id": box.get("id", ""),
            "box_label": box.get("label", "Display"),
            "total_snapshots": len(all_snaps),
            "cropped_count": len(crops_list),
            "crops": crops_list
        }

    try:
        res = await asyncio.to_thread(_batch_crop_worker)
        return JSONResponse(res)
    except Exception as e:
        logger.error("Error in batch_crop_box: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/ocr/train/label_batch")
async def api_ocr_train_label_batch(request: Request, user: str = Depends(require_auth)):
    """
    Save multiple labeled ROI crops from the batch queue/grid in a single request.
    Automatically learns the decimal point format per device/box from the user's labels.
    """
    record_activity()
    payload = await request.json()
    project_id = str(payload.get("project_id", "manual")).strip() or "manual"
    box = payload.get("box", {})
    items = payload.get("items", [])

    if not items or not isinstance(items, list):
        return JSONResponse({"status": "error", "message": "No labeled items provided."}, status_code=400)

    def _save_batch_worker() -> Dict[str, Any]:
        saved_frames = 0
        saved_digits = 0
        errors = []
        results = []
        for idx, it in enumerate(items):
            lbl = str(it.get("label", "")).strip()
            crop_b64 = str(it.get("crop_base64", "")).strip()
            snap_name = str(it.get("snapshot_name", "")).strip()
            item_box = it.get("box") or box
            if not lbl or not crop_b64:
                continue
            try:
                if "," in crop_b64:
                    crop_b64 = crop_b64.split(",", 1)[1]
                raw = base64.b64decode(crop_b64)
                pil_crop = Image.open(io.BytesIO(raw)).convert("RGB")
                r = save_labeled_roi_crop(pil_crop, lbl, item_box, project_id=project_id, snapshot_name=snap_name)
                saved_frames += 1
                saved_digits += int(r.get("digits_saved", 0))
                results.append({
                    "snapshot_name": snap_name,
                    "label_reading": lbl,
                    "frame_id": r.get("frame_id"),
                    "digits_saved": r.get("digits_saved", 0),
                    "digit_previews": r.get("digit_previews", [])
                })
            except Exception as ex:
                errors.append(f"{snap_name or idx}: {ex}")

        return {
            "status": "ok",
            "saved_frames": saved_frames,
            "saved_digits": saved_digits,
            "results": results,
            "errors": errors,
            "dataset_stats": get_training_dataset_stats()
        }

    try:
        res = await asyncio.to_thread(_save_batch_worker)
        return JSONResponse(res)
    except Exception as e:
        logger.error("Error in label_batch: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@app.delete("/api/ocr/train/hard_example/{hard_id}")
async def api_ocr_train_delete_hard_example(hard_id: str, user: str = Depends(require_auth)):
    """Remove a hard example from the review queue after labeling or dismissing."""
    record_activity()
    try:
        res = await asyncio.to_thread(delete_hard_example, hard_id)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.delete("/api/ocr/train/frame/{frame_id}")
async def api_ocr_train_delete_frame(frame_id: str, user: str = Depends(require_auth)):
    """Delete a labeled frame and all its segmented digit samples."""
    record_activity()
    try:
        res = await asyncio.to_thread(delete_labeled_frame, frame_id)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/ocr/train/run")
async def api_ocr_train_run(request: Request, user: str = Depends(require_auth)):
    """
    Execute CPU training of the 7-Segment PyTorch CNN with augmentation
    (brightness/contrast jitter, small shifts, shear) and whole-frame holdout validation.
    """
    record_activity()
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    try:
        res = await asyncio.to_thread(train_custom_model, payload)
        return JSONResponse(res)
    except Exception as e:
        logger.error("CPU Model Training error: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=FRONTEND_PORT, reload=True)

