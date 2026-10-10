"""
Lumen-Zero Frontend Web Application.
Built with FastAPI to perform heavy lifting: authentication, stream proxying,
connection pooling, response caching, inactivity auto-shutdown, and error resilience for Raspberry Pi.
"""
import asyncio
import io
import os
import time
import logging
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, Request, Response, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# Load environment configuration
load_dotenv()

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
OCR_PASSWORD = os.environ.get("OCR_PASSWORD", "QwertY123!")

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
    }



BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Ensure directories exist
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "css").mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "js").mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("frontend_app")

# Serializer for secure signed session cookies
serializer = URLSafeTimedSerializer(SECRET_KEY)
COOKIE_NAME = "lumen_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

app = FastAPI(title="LumenLux-Zero Controller", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Shared HTTPX client with connection pooling and timeouts
backend_client: Optional[httpx.AsyncClient] = None

# Inactivity state tracking (Stage 1: LED auto-off, Stage 2: Full system sleep)
last_activity_time = time.time()
led_auto_off_done = False
inactivity_shut_off = False
watchdog_task: Optional[asyncio.Task] = None

def record_activity():
    """Register user interaction timestamp."""
    global last_activity_time, led_auto_off_done, inactivity_shut_off
    last_activity_time = time.time()
    led_auto_off_done = False
    inactivity_shut_off = False


async def inactivity_watchdog_loop():
    """
    Background monitor with two distinct timeout stages:
    1. At idle >= LED_AUTO_OFF (2 mins / 120s): Automatically turn off WS2812B LEDs only.
       The camera and live video stream stay active!
    2. At idle >= INACTIVITY_TIME (10 mins / 600s): Automatically power off camera and all hardware (complete sleep).
    """
    global last_activity_time, led_auto_off_done, inactivity_shut_off
    logger.info(
        "Inactivity watchdog active. LED auto-off: %d seconds (%d mins) | Full sleep: %d seconds (%d mins).",
        LED_AUTO_OFF, LED_AUTO_OFF // 60,
        INACTIVITY_TIME, INACTIVITY_TIME // 60
    )
    while True:
        try:
            await asyncio.sleep(5)
            idle = time.time() - last_activity_time

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

            # Stage 2: Turn off camera and entire system after 10 minutes of idle
            if idle >= INACTIVITY_TIME and not inactivity_shut_off:
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
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Unexpected error in inactivity watchdog: %s", e)



@app.on_event("startup")
async def startup_event():
    global backend_client, watchdog_task
    backend_client = httpx.AsyncClient(
        base_url=f"http://{BECKEND_RPI_IP}:{BECKEND_RPI_PORT}",
        timeout=httpx.Timeout(10.0, connect=5.0, read=60.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=15)
    )
    logger.info("Frontend initialized. Target backend: http://%s:%d", BECKEND_RPI_IP, BECKEND_RPI_PORT)
    record_activity()
    watchdog_task = asyncio.create_task(inactivity_watchdog_loop())

    # Ensure hardware starts completely OFF on boot until user manually turns on
    try:
        await backend_client.post("/api/lights", json={"power": False}, timeout=3.0)
        await backend_client.post("/api/camera/toggle", json={"enabled": False}, timeout=3.0)
    except Exception:
        pass


@app.on_event("shutdown")
async def shutdown_event():
    global backend_client, watchdog_task
    if watchdog_task:
        watchdog_task.cancel()
    if backend_client:
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
    record_activity()
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


# --- BACKEND PROXY & HEAVY LIFTING APIS ---


@app.post("/api/heartbeat")
async def user_heartbeat(user: str = Depends(require_auth)):
    """Keep-alive ping from active browser."""
    record_activity()
    idle = time.time() - last_activity_time
    return {
        "status": "ok",
        "idle_seconds": int(idle),
        "led_auto_off": LED_AUTO_OFF,
        "inactivity_time": INACTIVITY_TIME,
        "remaining_led_seconds": max(0, int(LED_AUTO_OFF - idle)),
        "remaining_seconds": max(0, int(INACTIVITY_TIME - idle))
    }


@app.get("/api/status")
async def proxy_status(user: str = Depends(require_auth)):
    try:
        res = await backend_client.get("/api/status", timeout=4.0)
        return res.json()
    except Exception as e:
        return {
            "status": "offline",
            "error": f"Raspberry Pi ({BECKEND_RPI_IP}) unreachable: {str(e)}",
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
    payload = await request.json()
    try:
        res = await backend_client.post("/api/camera/select", json=payload, timeout=5.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.post("/api/camera/toggle")
async def proxy_camera_toggle(request: Request, user: str = Depends(require_auth)):
    payload = await request.json()
    try:
        res = await backend_client.post("/api/camera/toggle", json=payload, timeout=4.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.post("/api/camera/control")
async def proxy_camera_control(request: Request, user: str = Depends(require_auth)):
    payload = await request.json()
    try:
        res = await backend_client.post("/api/camera/control", json=payload, timeout=4.0)
        return res.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


@app.post("/api/camera/reset_defaults")
async def proxy_camera_reset_defaults(request: Request, user: str = Depends(require_auth)):
    payload = await request.json()
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
    payload = await request.json()
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
        target_url = f"http://{BECKEND_RPI_IP}:{BECKEND_RPI_PORT}/api/camera/stream"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=None)) as stream_client:
                async with stream_client.stream("GET", target_url) as resp:
                    async for chunk in resp.aiter_raw():
                        if await request.is_disconnected():
                            break
                        record_activity()
                        yield chunk
        except Exception as e:
            logger.warning("Stream proxy connection issue: %s", e)

    return StreamingResponse(
        stream_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=FRONTEND_PORT, reload=True)
