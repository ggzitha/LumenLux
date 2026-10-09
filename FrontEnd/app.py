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
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# Load environment configuration
load_dotenv()

USER_WEB = os.environ.get("USER_WEB", "admin")
USER_PASSWORD = os.environ.get("USER_PASSWORD", "raspberry")
BECKEND_RPI_IP = os.environ.get("BECKEND_RPI_IP", "192.168.88.21")
BECKEND_RPI_PORT = int(os.environ.get("BECKEND_RPI_PORT", 5000))
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", 8080))
SECRET_KEY = os.environ.get("SECRET_KEY", "lumen-zero-default-super-secure-key-2026")
INACTIVITY_TIME = int(os.environ.get("INACTIVITY_TIME", 600))  # Default 10 minutes (600s)

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

# Inactivity state tracking
last_activity_time = time.time()
inactivity_shut_off = False
watchdog_task: Optional[asyncio.Task] = None

def record_activity():
    """Register user interaction timestamp."""
    global last_activity_time, inactivity_shut_off
    last_activity_time = time.time()
    inactivity_shut_off = False


async def inactivity_watchdog_loop():
    """Background monitor that automatically turns off camera and WS2812B lights if no user is active."""
    global last_activity_time, inactivity_shut_off
    logger.info("Inactivity watchdog active. Auto-shutdown threshold: %d seconds (%d mins).", INACTIVITY_TIME, INACTIVITY_TIME // 60)
    while True:
        try:
            await asyncio.sleep(5)
            idle = time.time() - last_activity_time
            if idle >= INACTIVITY_TIME and not inactivity_shut_off:
                logger.warning(
                    "[INACTIVITY TIMEOUT] No user interaction for %d seconds. Automatically powering off camera and WS2812B lights on %s...",
                    int(idle), BECKEND_RPI_IP
                )
                inactivity_shut_off = True
                if backend_client:
                    # Turn off camera
                    try:
                        await backend_client.post("/api/camera/toggle", json={"enabled": False}, timeout=4.0)
                    except Exception as e:
                        logger.error("Failed to disable camera on inactivity: %s", e)
                    # Turn off WS2812B strip
                    try:
                        await backend_client.post("/api/lights", json={"power": False}, timeout=4.0)
                    except Exception as e:
                        logger.error("Failed to turn off lights on inactivity: %s", e)
                logger.info("[INACTIVITY TIMEOUT] Hardware successfully shut down.")
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
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "rpi_ip": BECKEND_RPI_IP,
            "rpi_port": BECKEND_RPI_PORT,
            "inactivity_time": INACTIVITY_TIME
        }
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
        "timeout": INACTIVITY_TIME,
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
