"""
Camera Manager for Raspberry Pi (Pi 4 & Pi Zero W).
Supports USB Webcams (Logitech C920 / C922) and CSI cameras with:
- Continuous MJPEG streaming (Steady camera LED & smooth 30 FPS, zero rapid flashing)
- Video Proc Amp controls (Brightness, Contrast, Saturation, Sharpness, White Balance, Gain, Backlight, Anti-flicker)
- Camera Controls (Zoom, Focus, Exposure, Pan, Tilt, Low Light Compensation)
- Rock-solid process lifecycle management, anti-hang mutex, and graceful hardware buffer release
"""
import glob
import io
import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger("camera_manager")

# Standard V4L2 Control Mappings for DirectShow / Web UI
CTRL_MAP = {
    # Camera Control tab
    "zoom": "zoom_absolute",
    "focus": "focus_absolute",
    "focus_auto": "focus_automatic_continuous",
    "exposure": "exposure_time_absolute",
    "exposure_auto": "auto_exposure",
    "aperture": "iris_absolute",
    "pan": "pan_absolute",
    "tilt": "tilt_absolute",
    "roll": "roll_absolute",
    "low_light_comp": "exposure_dynamic_framerate",
    # Video Proc Amp tab
    "brightness": "brightness",
    "contrast": "contrast",
    "hue": "hue",
    "saturation": "saturation",
    "sharpness": "sharpness",
    "gamma": "gamma",
    "white_balance": "white_balance_temperature",
    "white_balance_auto": "white_balance_automatic",
    "backlight_comp": "backlight_compensation",
    "gain": "gain",
    "powerline_freq": "power_line_frequency",
}

class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.ctrl_lock = threading.Lock()
        self.active_camera_id = None
        self.camera_enabled = True
        
        # Stream settings
        self.target_fps = 30
        self.resolution = (640, 480)
        
        # State tracking
        self.last_frame = None
        self.last_frame_time = 0
        self.client_count = 0
        self.running = True
        
        # Continuous capture process
        self.capture_proc = None
        self.capture_thread = None
        self.csi_scanned = False
        self.csi_cameras_cache = []

        # Cached controls & supported controls per device
        self.controls_cache = {}
        self.supported_ctrls = {}
        
        # Discover and initialize
        self.discover_cameras()
        self._start_capture_loop()

    def _is_video_capture_device(self, dev_path: str) -> bool:
        """Verify device node actually supports video capture, not codecs or metadata."""
        try:
            out = subprocess.check_output(["v4l2-ctl", "-d", dev_path, "-D"], text=True, stderr=subprocess.DEVNULL, timeout=2.0)
            out_lower = out.lower()
            if "bus info         : platform:" in out_lower and "unicam" not in out_lower:
                return False
            caps_part = out.split("Device Caps")[1] if "Device Caps" in out else out
            return "video capture" in caps_part.lower()
        except Exception:
            return False

    def discover_cameras(self) -> list[dict]:
        """Scan for connected USB webcams and CSI cameras."""
        cameras = []
        try:
            out = subprocess.check_output(["v4l2-ctl", "--list-devices"], text=True, stderr=subprocess.DEVNULL, timeout=3.0)
            blocks = out.strip().split("\n\n")
            for block in blocks:
                lines = [line.strip() for line in block.split("\n") if line.strip()]
                if not lines:
                    continue
                header = lines[0]
                header_lower = header.lower()
                if any(x in header_lower for x in ["bcm2835", "codec", "isp", "hevc", "decode"]):
                    continue
                
                device_paths = [l for l in lines[1:] if l.startswith("/dev/video")]
                capture_nodes = [p for p in device_paths if self._is_video_capture_device(p)]
                if capture_nodes:
                    primary_path = capture_nodes[0]
                    cam_id = os.path.basename(primary_path)
                    name = header.split("(")[0].strip() or f"USB Camera ({cam_id})"
                    cameras.append({
                        "id": cam_id,
                        "name": name,
                        "type": "usb",
                        "device_path": primary_path,
                        "all_nodes": capture_nodes,
                        "active": (cam_id == self.active_camera_id)
                    })
        except Exception as e:
            logger.debug("v4l2-ctl discovery error: %s", e)

        # Query CSI cameras only once to prevent libcamera busy lock
        if not self.csi_scanned:
            self.csi_scanned = True
            try:
                from picamera2 import Picamera2
                picam = Picamera2()
                if hasattr(picam, "camera_names") and picam.camera_names:
                    for idx, name in enumerate(picam.camera_names):
                        self.csi_cameras_cache.append({
                            "id": f"csi_{idx}",
                            "name": f"Raspberry Pi CSI Camera ({name})",
                            "type": "csi",
                            "device_path": f"/dev/media{idx}",
                            "active": False
                        })
                picam.close()
            except Exception as e:
                logger.debug("picamera2 check: %s", e)

        cameras.extend(self.csi_cameras_cache)

        if not cameras:
            cameras.append({
                "id": "virtual_test",
                "name": "Virtual Test Camera (Synthetic Test Card)",
                "type": "virtual",
                "device_path": "/dev/null",
                "active": True
            })

        if not self.active_camera_id and cameras:
            self.active_camera_id = cameras[0]["id"]
            cameras[0]["active"] = True

        # Apply default 50 Hz anti-flicker on hardware
        if self.active_camera_id and not self.active_camera_id.startswith("virtual"):
            try:
                dev_path = f"/dev/{self.active_camera_id}"
                subprocess.run(["v4l2-ctl", "-d", dev_path, "--set-ctrl=power_line_frequency=1"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5)
            except Exception:
                pass

        return cameras

    def get_camera_controls(self, cam_id: str = None) -> dict:
        """Query available v4l2 controls for the given camera and cache supported keys."""
        target_id = cam_id or self.active_camera_id
        if not target_id or target_id.startswith("virtual"):
            return {
                "brightness": {"value": 128, "min": 0, "max": 255, "default": 128},
                "contrast": {"value": 128, "min": 0, "max": 255, "default": 128},
                "hue": {"value": 0, "min": -180, "max": 180, "default": 0},
                "saturation": {"value": 128, "min": 0, "max": 255, "default": 128},
                "sharpness": {"value": 128, "min": 0, "max": 255, "default": 128},
                "gamma": {"value": 100, "min": 100, "max": 500, "default": 100},
                "white_balance_automatic": {"value": 1, "default": 1},
                "white_balance_temperature": {"value": 4000, "min": 2000, "max": 6500, "default": 4000},
                "backlight_compensation": {"value": 0, "min": 0, "max": 1, "default": 0},
                "gain": {"value": 0, "min": 0, "max": 255, "default": 0},
                "power_line_frequency": {"value": 1, "default": 1},
                "auto_exposure": {"value": 3, "default": 3},
                "exposure_time_absolute": {"value": 250, "min": 3, "max": 2047, "default": 250},
                "exposure_dynamic_framerate": {"value": 0, "default": 0},
                "focus_automatic_continuous": {"value": 1, "default": 1},
                "focus_absolute": {"value": 0, "min": 0, "max": 250, "default": 0},
                "zoom_absolute": {"value": 100, "min": 100, "max": 500, "default": 100},
                "iris_absolute": {"value": 0, "min": 0, "max": 10, "default": 0},
                "pan_absolute": {"value": 0, "min": -36000, "max": 36000, "default": 0},
                "tilt_absolute": {"value": 0, "min": -36000, "max": 36000, "default": 0},
                "roll_absolute": {"value": 0, "min": 0, "max": 0, "default": 0}
            }

        dev_path = f"/dev/{target_id}" if not target_id.startswith("/") else target_id
        controls = {}
        with self.ctrl_lock:
            try:
                out = subprocess.check_output(["v4l2-ctl", "-d", dev_path, "-l"], text=True, stderr=subprocess.DEVNULL, timeout=2.0)
                supported = set()
                for line in out.splitlines():
                    m = re.match(r"^\s*([a-zA-Z0-9_]+)\s+0x[0-9a-fA-F]+\s+\([^)]+\)\s*:(.*)$", line)
                    if m:
                        ctrl_name = m.group(1)
                        attrs_str = m.group(2)
                        supported.add(ctrl_name)
                        attrs = {}
                        for item in attrs_str.split():
                            if "=" in item:
                                k, v = item.split("=", 1)
                                try:
                                    attrs[k] = int(v)
                                except ValueError:
                                    attrs[k] = v
                        controls[ctrl_name] = attrs
                self.supported_ctrls[target_id] = supported
            except Exception as e:
                logger.warning("Error reading v4l2 controls for %s: %s", dev_path, e)

        return controls

    def set_camera_control(self, ctrl_name: str, value, cam_id: str = None) -> bool:
        """Set a single control parameter, mapping aliases and skipping unsupported ones."""
        target_id = cam_id or self.active_camera_id
        v4l_ctrl = CTRL_MAP.get(ctrl_name, ctrl_name)
        val = int(value)

        # For auto exposure: V4L2 menu 3=Aperture Priority (Auto), 1=Manual
        if v4l_ctrl == "auto_exposure":
            val = 3 if bool(val) else 1

        if not target_id or target_id.startswith("virtual"):
            self.controls_cache[v4l_ctrl] = val
            return True

        # Check supported list if cached
        supp = self.supported_ctrls.get(target_id)
        if supp is None:
            self.get_camera_controls(target_id)
            supp = self.supported_ctrls.get(target_id, set())

        if supp and v4l_ctrl not in supp:
            # Control not supported by this hardware (e.g. iris/roll on C922)
            logger.debug("Skipping unsupported control %s on %s", v4l_ctrl, target_id)
            self.controls_cache[v4l_ctrl] = val
            return True

        dev_path = f"/dev/{target_id}" if not target_id.startswith("/") else target_id
        with self.ctrl_lock:
            try:
                cmd = ["v4l2-ctl", "-d", dev_path, f"--set-ctrl={v4l_ctrl}={val}"]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
                if res.returncode == 0:
                    self.controls_cache[v4l_ctrl] = val
                    logger.info("Set V4L2 control %s=%s on %s", v4l_ctrl, val, dev_path)
                    return True
                else:
                    logger.warning("v4l2-ctl returned code %d for %s=%s", res.returncode, v4l_ctrl, val)
                    return False
            except Exception as e:
                logger.error("Failed to set control %s=%s on %s: %s", v4l_ctrl, val, dev_path, e)
                return False

    def reset_camera_defaults(self, cam_id: str = None) -> bool:
        """Reset all camera controls to factory defaults."""
        target_id = cam_id or self.active_camera_id
        if not target_id or target_id.startswith("virtual"):
            return True
        dev_path = f"/dev/{target_id}" if not target_id.startswith("/") else target_id
        try:
            ctrls = self.get_camera_controls(target_id)
            with self.ctrl_lock:
                for name, data in ctrls.items():
                    if "default" in data:
                        subprocess.run(["v4l2-ctl", "-d", dev_path, f"--set-ctrl={name}={data['default']}"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
            logger.info("Reset camera controls to defaults for %s", dev_path)
            return True
        except Exception as e:
            logger.error("Error resetting defaults: %s", e)
            return False

    def select_camera(self, cam_id: str, width: int = None, height: int = None, fps: int = None) -> dict:
        """Switch active camera and configure stream settings."""
        logger.info("Selecting camera %s: %sx%s @ %sfps", cam_id, width, height, fps)
        with self.lock:
            self.active_camera_id = cam_id
            if width and height:
                self.resolution = (int(width), int(height))
            if fps:
                self.target_fps = max(1, min(60, int(fps)))
            # Pre-generate synthetic frame at new resolution so aspect ratio is immediate
            self.last_frame = self._generate_synthetic_frame("Switching...")
        self._restart_stream_pipeline()
        return self.get_state()

    def set_enabled(self, enabled: bool):
        logger.info("Camera enabled state changing to: %s", enabled)
        with self.lock:
            self.camera_enabled = bool(enabled)
            if not self.camera_enabled:
                self.last_frame = self._generate_synthetic_frame("Camera Disabled / Sleep")
        if not self.camera_enabled:
            self._restart_stream_pipeline()
        logger.info("Camera enabled state set to: %s", self.camera_enabled)

    def _restart_stream_pipeline(self):
        """Cleanly terminate capture pipeline and allow uvcvideo buffers to unmap."""
        with self.lock:
            proc = self.capture_proc
            self.capture_proc = None

        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=0.5)
                except Exception:
                    pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            # Give Linux kernel uvcvideo driver 200ms to cleanly release device buffers
            time.sleep(0.2)

    def _start_capture_loop(self):
        self.capture_thread = threading.Thread(target=self._continuous_stream_worker, name="CamContinuousWorker", daemon=True)
        self.capture_thread.start()

    def _generate_synthetic_frame(self, text: str = "") -> bytes:
        """Fallback synthetic test pattern."""
        try:
            from PIL import Image, ImageDraw
            w, h = self.resolution
            img = Image.new("RGB", (w, h), color=(14, 20, 32))
            draw = ImageDraw.Draw(img)
            
            bar_colors = [
                (255, 0, 0), (255, 128, 0), (255, 255, 0),
                (0, 255, 0), (0, 200, 255), (0, 0, 255), (180, 0, 255)
            ]
            bar_w = max(1, w // len(bar_colors))
            bar_h = int(h * 0.18)
            for idx, c in enumerate(bar_colors):
                draw.rectangle([idx * bar_w, h - bar_h, (idx + 1) * bar_w, h], fill=c)

            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            draw.text((int(w * 0.08), int(h * 0.3)), "LUMENLUX-ZERO CAMERA STREAM", fill=(255, 255, 255))
            draw.text((int(w * 0.08), int(h * 0.42)), f"Device: {self.active_camera_id or 'Virtual'} ({w}x{h})", fill=(0, 242, 254))
            draw.text((int(w * 0.08), int(h * 0.54)), now_str, fill=(200, 200, 200))
            status_str = f"STATUS: {text.upper()}" if text else ("CAMERA ACTIVE" if self.camera_enabled else "CAMERA DISABLED")
            status_col = (16, 185, 129) if self.camera_enabled else (239, 68, 68)
            draw.text((int(w * 0.08), int(h * 0.66)), status_str, fill=status_col)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            return buf.getvalue()
        except Exception:
            return b""

    def _continuous_stream_worker(self):
        """
        Persistent background stream reader.
        Keeps /dev/video0 open continuously while streaming:
        - Prevents Logitech C922 LED rapid flashing!
        - Maintains rock-solid 30 FPS.
        - Backs off if capture process fails prematurely (prevents tight loop crash).
        """
        logger.info("Persistent camera capture worker started.")
        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"

        while self.running:
            if not self.camera_enabled or not self.active_camera_id or self.active_camera_id.startswith("virtual"):
                # Camera disabled or virtual: ensure hardware device is closed so camera LED turns off
                if self.capture_proc is not None:
                    self._restart_stream_pipeline()
                with self.lock:
                    self.last_frame = self._generate_synthetic_frame("Camera Disabled / Sleep" if not self.camera_enabled else "Virtual Device")
                time.sleep(0.4)
                continue

            dev_path = f"/dev/{self.active_camera_id}"
            if not os.path.exists(dev_path):
                with self.lock:
                    self.last_frame = self._generate_synthetic_frame("Device not found")
                time.sleep(1.0)
                continue

            with self.lock:
                w, h = self.resolution

            cmd = [
                "v4l2-ctl", "-d", dev_path,
                "--set-fmt-video", f"width={w},height={h},pixelformat=MJPG",
                "--stream-mmap", "--stream-to=-"
            ]

            start_time = time.time()
            frames_captured = 0

            try:
                logger.info("Starting persistent V4L2 stream on %s (%dx%d)...", dev_path, w, h)
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=1024 * 1024
                )
                with self.lock:
                    self.capture_proc = proc

                buffer = b""
                while self.running and self.camera_enabled and self.capture_proc is proc:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while True:
                        soi_idx = buffer.find(SOI)
                        if soi_idx == -1:
                            buffer = buffer[-2:]
                            break
                        eoi_idx = buffer.find(EOI, soi_idx + 2)
                        if eoi_idx == -1:
                            buffer = buffer[soi_idx:]
                            break
                        frame = buffer[soi_idx : eoi_idx + 2]
                        buffer = buffer[eoi_idx + 2 :]

                        frames_captured += 1
                        with self.lock:
                            self.last_frame = frame
                            self.last_frame_time = time.time()

            except Exception as e:
                logger.error("Continuous stream worker error: %s", e)
            finally:
                self._restart_stream_pipeline()
                elapsed = time.time() - start_time
                if frames_captured == 0 or elapsed < 1.5:
                    logger.warning("Stream capture exited prematurely (%d frames in %.2fs). Backing off 1.0s before restart...",
                                   frames_captured, elapsed)
                    time.sleep(1.0)

    def get_snapshot(self) -> bytes:
        with self.lock:
            if self.last_frame:
                return self.last_frame
        return self._generate_synthetic_frame("Snapshot")

    def generate_mjpeg_stream(self):
        """Generator yielding MJPEG multipart chunks with client tracking."""
        self.client_count += 1
        logger.info("Client connected to stream (active: %d)", self.client_count)
        last_yielded = 0
        interval = 1.0 / max(1, self.target_fps)
        try:
            while self.running:
                now = time.time()
                if now - last_yielded >= interval:
                    frame = None
                    with self.lock:
                        frame = self.last_frame
                    if frame:
                        last_yielded = now
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                            + frame + b"\r\n"
                        )
                time.sleep(0.01)
        finally:
            self.client_count = max(0, self.client_count - 1)
            logger.info("Client disconnected from stream (active: %d)", self.client_count)

    def get_state(self) -> dict:
        with self.lock:
            return {
                "active_camera_id": self.active_camera_id,
                "camera_enabled": self.camera_enabled,
                "target_fps": self.target_fps,
                "resolution": {
                    "width": self.resolution[0],
                    "height": self.resolution[1]
                },
                "client_count": self.client_count,
                "controls": self.get_camera_controls()
            }

    def cleanup(self):
        self.running = False
        self._restart_stream_pipeline()
        if self.capture_thread and self.capture_thread.is_alive():
            self.capture_thread.join(timeout=1.0)
        logger.info("CameraManager cleaned up.")
