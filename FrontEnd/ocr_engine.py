from __future__ import annotations
"""
LumenLux OCR Engine & Thermometer Analyzer.
Provides:
- Auto-detection of digital LCD/VFD displays vs analog needle dial thermometers.
- Preprocessing and recognition optimized for 7-segment LCD and LED readouts.
- Project snapshot catalog management.
- Calibration report generation in Excel (.xlsx) with embedded display thumbnails.
"""
import io
import os
import re
import math
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageOps, ImageEnhance

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    cv2 = None
    HAS_CV2 = False

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    import onnxruntime as ort
    HAS_ONNX = True
except ImportError:
    HAS_ONNX = False
    ort = None

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.drawing.image import Image as OpenpyxlImage
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

logger = logging.getLogger("ocr_engine")

MODELS_DIR = Path(os.environ.get("MODELS_DIR", Path(__file__).resolve().parent / "data" / "models"))


class OnnxOcrEngine:
    """
    High-performance ONNX AI OCR Engine supporting PP-OCRv6 Medium and PP-OCRv4.
    Tuned for digital LCD/VFD/LED display recognition with multi-threaded CPU execution.
    """
    def __init__(self, models_dir: Optional[Path] = None):
        self.models_dir = Path(models_dir) if models_dir else MODELS_DIR
        self.session = None
        self.det_session = None
        self.char_list: List[str] = []
        self._initialized = False
        self.model_version: str = os.environ.get("OCR_MODEL_VERSION", "v6").strip().lower()

    def initialize(self) -> bool:
        if not HAS_ONNX or ort is None:
            return False

        v6_small_rec = self.models_dir / "ch_PP-OCRv6_small_rec_infer.onnx"
        v6_small_det = self.models_dir / "ch_PP-OCRv6_small_det_infer.onnx"
        v6_tiny_rec = self.models_dir / "ch_PP-OCRv6_tiny_rec_infer.onnx"
        v6_tiny_det = self.models_dir / "ch_PP-OCRv6_tiny_det_infer.onnx"
        v6_med_rec = self.models_dir / "ch_PP-OCRv6_medium_rec_infer.onnx"
        v6_med_det = self.models_dir / "ch_PP-OCRv6_medium_det_infer.onnx"
        v6_keys_path = self.models_dir / "ppocrv6_keys.txt"

        v4_rec_path = self.models_dir / "ch_PP-OCRv4_rec_infer.onnx"
        v4_det_path = self.models_dir / "ch_PP-OCRv4_det_infer.onnx"
        v4_keys_path = self.models_dir / "ppocr_keys_v1.txt"

        target_version = os.environ.get("OCR_MODEL_VERSION", "v6").strip().lower()

        # Selection hierarchy: tiny, small, medium, v4
        if target_version in ("v6_tiny", "tiny") and v6_tiny_rec.is_file() and v6_keys_path.is_file():
            rec_model_path = v6_tiny_rec
            det_model_path = v6_tiny_det if v6_tiny_det.is_file() else v4_det_path
            keys_path = v6_keys_path
            self.model_version = "v6_tiny"
        elif (target_version in ("v6_small", "small", "v6") and v6_small_rec.is_file() and v6_keys_path.is_file()):
            rec_model_path = v6_small_rec
            det_model_path = v6_small_det if v6_small_det.is_file() else v6_med_det if v6_med_det.is_file() else v4_det_path
            keys_path = v6_keys_path
            self.model_version = "v6_small"
        elif (target_version in ("v6_medium", "medium") and v6_med_rec.is_file() and v6_keys_path.is_file()) or (target_version == "v6" and v6_med_rec.is_file()):
            rec_model_path = v6_med_rec
            det_model_path = v6_med_det if v6_med_det.is_file() else v4_det_path
            keys_path = v6_keys_path
            self.model_version = "v6_medium"
        elif v4_rec_path.is_file() and v4_keys_path.is_file():
            rec_model_path = v4_rec_path
            det_model_path = v4_det_path
            keys_path = v4_keys_path
            self.model_version = "v4"
        elif v6_small_rec.is_file():
            rec_model_path = v6_small_rec
            det_model_path = v6_small_det
            keys_path = v6_keys_path
            self.model_version = "v6_small"
        elif v6_med_rec.is_file():
            rec_model_path = v6_med_rec
            det_model_path = v6_med_det
            keys_path = v6_keys_path
            self.model_version = "v6_medium"
        else:
            rec_model_path = v4_rec_path
            det_model_path = v4_det_path
            keys_path = v4_keys_path
            self.model_version = "v4"
            self._ensure_models_exist(rec_model_path, keys_path)

        if not rec_model_path.is_file() or not keys_path.is_file():
            logger.warning("[ONNX AI OCR] Model files missing in %s", self.models_dir)
            return False

        try:
            with open(keys_path, "r", encoding="utf-8") as f:
                lines = [line.strip("\r\n") for line in f.readlines()]
            self.char_list = ["blank"] + lines + [" "]

            opts = ort.SessionOptions()
            # Default to 4 threads max: Prevents CPU 100% saturation and prevents freezing video/browser streams
            target_threads = int(os.environ.get("ONNX_CPU_THREADS", "4"))
            cpu_threads = max(1, min(os.cpu_count() or 4, target_threads))
            opts.intra_op_num_threads = cpu_threads
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self.session = ort.InferenceSession(str(rec_model_path), sess_options=opts, providers=["CPUExecutionProvider"])

            if det_model_path.is_file():
                try:
                    self.det_session = ort.InferenceSession(str(det_model_path), sess_options=opts, providers=["CPUExecutionProvider"])
                    logger.info("[ONNX AI OCR] Loaded %s text detection model from %s", det_model_path.name, det_model_path)
                except Exception as ex:
                    logger.warning("[ONNX AI OCR] Could not load det model: %s", ex)

            self._initialized = True
            det_name = det_model_path.stem if det_model_path.is_file() else "None"
            logger.info("[ONNX AI OCR] Loaded ONNX OCR models on CPU (%d threads) [TIER: %s, REC: %s, DET: %s]", cpu_threads, self.model_version.upper(), rec_model_path.name, det_name)
            return True
        except Exception as e:
            logger.error("[ONNX AI OCR] Initialization failed: %s", e)
            self.session = None
            self.det_session = None
            self._initialized = False
            return False

    def _ensure_models_exist(self, rec_path: Path, keys_path: Path) -> None:
        """Download lightweight PP-OCRv4 model and dictionary if not yet present."""
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            import urllib.request
            headers = {"User-Agent": "Mozilla/5.0"}
            
            if not keys_path.is_file():
                logger.info("[ONNX AI OCR] Downloading ppocr_keys_v1.txt...")
                url_keys = "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/ppocr/utils/ppocr_keys_v1.txt"
                req = urllib.request.Request(url_keys, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r, open(keys_path, "wb") as f:
                    f.write(r.read())
                logger.info("[ONNX AI OCR] Saved ppocr_keys_v1.txt")

            if not rec_path.is_file():
                logger.info("[ONNX AI OCR] Downloading ch_PP-OCRv4_rec_infer.onnx (~10MB)...")
                url_rec = "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv4/ch_PP-OCRv4_rec_infer.onnx"
                req = urllib.request.Request(url_rec, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as r, open(rec_path, "wb") as f:
                    f.write(r.read())
                logger.info("[ONNX AI OCR] Saved ch_PP-OCRv4_rec_infer.onnx")
        except Exception as ex:
            logger.warning("[ONNX AI OCR] Auto-download models failed (offline fallback to Tesseract): %s", ex)

    def recognize(self, pil_or_cv_img: Any, box: Optional[Dict[str, Any]] = None) -> Tuple[str, float]:
        """
        Runs fast ONNX inference on display crop.
        Returns (recognized_text, confidence_score).
        """
        if not self._initialized:
            if not self.initialize():
                return "", 0.0

        if not self.session:
            return "", 0.0

        try:
            if isinstance(pil_or_cv_img, Image.Image):
                arr = np.array(pil_or_cv_img.convert("RGB"))
            elif HAS_CV2 and isinstance(pil_or_cv_img, np.ndarray):
                if len(pil_or_cv_img.shape) == 2:
                    arr = cv2.cvtColor(pil_or_cv_img, cv2.COLOR_GRAY2RGB)
                elif pil_or_cv_img.shape[2] == 4:
                    arr = cv2.cvtColor(pil_or_cv_img, cv2.COLOR_BGRA2RGB)
                else:
                    arr = cv2.cvtColor(pil_or_cv_img, cv2.COLOR_BGR2RGB)
            else:
                return "", 0.0

            h, w = arr.shape[:2]
            if h < 8 or w < 8:
                return "", 0.0

            # If glowing green digits (Fluke 1502A / LED) present, crop tightly around active digits
            r, g, b = arr[:,:,0].astype(int), arr[:,:,1].astype(int), arr[:,:,2].astype(int)
            mask = ((g - np.maximum(r, b)) > 18) & (g > 115)
            if np.count_nonzero(mask) >= 25:
                y_idx, x_idx = np.where(mask)
                pad_x, pad_y = 8, 6
                min_y = max(0, int(np.min(y_idx)) - pad_y)
                max_y = min(h, int(np.max(y_idx)) + pad_y)
                min_x = max(0, int(np.min(x_idx)) - pad_x)
                max_x = min(w, int(np.max(x_idx)) + pad_x)
                if (max_y - min_y) > 10 and (max_x - min_x) > 15:
                    arr = arr[min_y:max_y, min_x:max_x]
                    h, w = arr.shape[:2]

            target_h = 48
            target_w = max(48, int(np.ceil(int(target_h * (w / float(h))) / 32) * 32))
            
            if HAS_CV2 and cv2 is not None:
                resized = cv2.resize(arr, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
            else:
                resized = np.array(Image.fromarray(arr).resize((target_w, target_h), Image.Resampling.BILINEAR))

            if str(getattr(self, "model_version", "v4")).lower().startswith("v6"):
                # PP-OCRv6 ImageNet normalization
                mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
                std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
                norm = (resized.astype(np.float32) / 255.0 - mean) / std
            else:
                # PP-OCRv4 normalization
                norm = (resized.astype(np.float32) / 255.0 - 0.5) / 0.5
            tensor = np.expand_dims(np.transpose(norm, (2, 0, 1)), axis=0)

            input_name = self.session.get_inputs()[0].name
            preds = self.session.run(None, {input_name: tensor})[0]

            preds_idx = np.argmax(preds[0], axis=-1)
            preds_prob = np.max(preds[0], axis=-1)

            chars = []
            probs = []
            prev = 0
            for idx, p in zip(preds_idx, preds_prob):
                if idx != 0 and idx != prev:
                    if idx < len(self.char_list):
                        chars.append(self.char_list[idx])
                        probs.append(p)
                prev = idx

            raw_str = "".join(chars).strip()
            # Strip non-ASCII noise glyphs (e.g. Chinese character dictionary bleed)
            raw_str = re.sub(r'[^\x20-\x7E]+', '', raw_str).strip()
            conf = float(np.mean(probs)) if probs else 0.0
            return raw_str, conf
        except Exception as e:
            logger.error("[ONNX AI OCR] Recognition inference error: %s", e)
            return "", 0.0

    def detect_text_blobs(self, img_arr: np.ndarray, min_prob: float = 0.35) -> List[Tuple[float, float, float, float]]:
        """
        Uses DBNet text detector to find normalized bounding boxes (x, y, w, h) of text in an image.
        """
        if not self._initialized:
            self.initialize()
        if not self.det_session:
            return []

        try:
            h, w = img_arr.shape[:2]
            det_w, det_h = 960, 544
            pil_img = Image.fromarray(img_arr).resize((det_w, det_h), Image.Resampling.BILINEAR)
            inp = (np.array(pil_img).astype(np.float32) / 255.0 - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
            inp = np.transpose(inp, (2, 0, 1))[np.newaxis, ...].astype(np.float32)

            input_name = self.det_session.get_inputs()[0].name
            pred = self.det_session.run(None, {input_name: inp})[0][0, 0]
            mask = (pred > min_prob)

            visited = np.zeros_like(mask, dtype=bool)
            ys, xs = np.where(mask)
            coords = set(zip(ys, xs))
            blobs = []

            while coords:
                y, x = coords.pop()
                if visited[y, x]:
                    continue
                queue = [(y, x)]
                visited[y, x] = True
                min_x, max_x = x, x
                min_y, max_y = y, y
                count = 0
                while queue:
                    cy, cx = queue.pop()
                    count += 1
                    if cx < min_x: min_x = cx
                    if cx > max_x: max_x = cx
                    if cy < min_y: min_y = cy
                    if cy > max_y: max_y = cy
                    for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        ny, nx = cy + dy, cx + dx
                        if (ny, nx) in coords:
                            coords.remove((ny, nx))
                            visited[ny, nx] = True
                            queue.append((ny, nx))
                if count > 100:
                    blobs.append((float(min_x / det_w), float(min_y / det_h), float((max_x - min_x) / det_w), float((max_y - min_y) / det_h)))
            return blobs
        except Exception as ex:
            logger.warning("[ONNX AI OCR] Detection inference error: %s", ex)
            return []


# Global singleton instance
onnx_ocr_engine = OnnxOcrEngine()


def verify_is_digital_display(cv_crop: Any) -> Tuple[bool, float, str]:
    """
    Verify whether a cropped image region actually contains an electronic digital display
    (7-segment LCD, dot-matrix LCD, VFD, or LED) vs a non-digital instrument
    (mercury-in-glass stem, analog dial, plastic strip, or non-display surface).
    Returns (is_digital: bool, confidence: float, reason: str).
    """
    if not HAS_CV2 or cv_crop is None or np is None or cv2 is None:
        return True, 0.8, "OpenCV / NumPy not loaded, assuming valid display"

    if hasattr(cv_crop, "size") and cv_crop.size == 0:
        return False, 0.0, "Empty region"

    h, w = cv_crop.shape[:2]
    if h < 12 or w < 12:
        return False, 0.0, "Region too small for digital display"

    aspect = float(w) / max(1, h)
    # Mercury glass stems have extreme aspect ratios (> 5.0 or < 0.2)
    if aspect > 5.5 or aspect < 0.18:
        return False, 0.0, "Aspect ratio resembles narrow glass stem / capillary tube, not a digital screen"

    gray = cv2.cvtColor(cv_crop, cv2.COLOR_BGR2GRAY)

    # 1. Check for Green VFD / LED display (e.g. Fluke 1502A standard thermometer)
    b, g, r = cv2.split(cv_crop)
    g_diff = cv2.subtract(g, cv2.max(r, b))
    green_count = int(np.count_nonzero(g_diff > 20))
    if green_count >= max(35, int(w * h * 0.002)):
        return True, 0.98, "Green VFD/LED digital display detected"

    # 2. Check for standard 7-segment / numeric LCD display
    # Sharp horizontal and vertical segment edge energy
    sobel_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    edge_energy_x = float(np.mean(np.abs(sobel_x)))
    edge_energy_y = float(np.mean(np.abs(sobel_y)))
    total_edge_energy = edge_energy_x + edge_energy_y

    # Contrast spread
    std_dev = float(np.std(gray))
    min_val, max_val = float(np.min(gray)), float(np.max(gray))
    contrast_range = max_val - min_val

    # Plastic strips typically have very smooth gradients (low edge energy and low std_dev)
    if total_edge_energy < 14.0 or contrast_range < 35.0:
        return False, 0.1, "Low contrast / smooth gradient texture (no digit segment edges)"

    # 3. Connected Component Analysis for digital digit segments
    # Adaptive thresholding to isolate segments
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 4
    )
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(thresh)

    digit_segments = 0
    box_area = float(w * h)
    for i in range(1, num_labels):
        stat_w = stats[i, cv2.CC_STAT_WIDTH]
        stat_h = stats[i, cv2.CC_STAT_HEIGHT]
        stat_area = stats[i, cv2.CC_STAT_AREA]
        # Filter component size reasonable for digits / decimal points
        if (stat_area / box_area) > 0.002 and (stat_area / box_area) < 0.25:
            c_aspect = float(stat_w) / max(1, stat_h)
            if 0.15 <= c_aspect <= 4.0:
                digit_segments += 1

    # Digital displays have multiple distinct segment glyphs (at least 2 segments for digits and decimal)
    if digit_segments >= 2 and total_edge_energy >= 16.0:
        return True, min(1.0, 0.5 + (digit_segments * 0.05)), f"Digital display with {digit_segments} segment elements"

    return False, 0.2, "Insufficient digital segment characteristics"


def classify_non_digital_meter(cv_img: Any) -> Optional[Dict[str, Any]]:
    """
    Check if the frame contains non-digital / analog instruments:
    - Circular dial meters / needle pointer hygrometers
    - Mercury-in-glass / liquid-in-glass capillary thermometers
    - Plastic color-change strip thermometers
    Returns warning dict if non-digital instrument found, or None.
    """
    if not HAS_CV2 or cv_img is None or np is None or cv2 is None:
        return None

    h, w = cv_img.shape[:2]
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)

    # 1. Circular Dial Meter Detection (Hough Circles)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=int(min(w, h) * 0.18),
        param1=90,
        param2=65,
        minRadius=int(min(w, h) * 0.10),
        maxRadius=int(min(w, h) * 0.42)
    )

    if circles is not None and len(circles[0]) >= 1:
        dial_count = len(circles[0])
        return {
            "unsupported_type": "analog_dial",
            "message": (
                f"Terdeteksi {dial_count} buah termometer/higrometer analog berjarum (Dial/Needle). "
                "Alat ukur jarum/dial tidak memiliki display digital dan tidak didukung untuk OCR digital. "
                "Harap gunakan termometer digital berlayar LCD/LED."
            ),
            "count": dial_count
        }

    # 2. Mercury-in-glass / Liquid Column Thermometer Detection
    # Characterized by very narrow, high-aspect ratio vertical glass stems with liquid capillary columns
    # Find contours with high vertical/horizontal aspect ratio (> 5.5) and significant length
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80, minLineLength=int(min(w, h) * 0.35), maxLineGap=15)

    # Parallel long vertical/horizontal capillary edges indicative of glass stems
    if lines is not None and len(lines) >= 4:
        # Check if lines form narrow parallel pairs (stem column)
        long_verticals = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)
            if dy > (dx * 3) and dy > (h * 0.3):
                long_verticals.append(x1)
        
        if len(long_verticals) >= 3:
            # Check for absence of rectangular LCD screens
            return {
                "unsupported_type": "mercury_glass",
                "message": (
                    "Terdeteksi termometer air raksa / kaca kapiler (Mercury-in-glass / Liquid-in-glass). "
                    "Termometer batang kaca tidak memiliki layar digital dan tidak didukung untuk OCR digital. "
                    "Harap gunakan termometer digital berlayar LCD/LED."
                ),
                "count": 1
            }

    # 3. Plastic Color-Change Strip Detection
    # Thin horizontal/vertical strips with uniform smooth color blocks
    # Checked during candidate evaluation
    return None


def detect_thermometers_in_image(img_input: Any) -> Dict[str, Any]:
    """
    Detects digital readable display screens (LCD / VFD / LED) of sensors and standard thermometers
    inside the calibration chamber. Places ROI bounding boxes tightly around the active readable display.
    """
    # 1. Load PIL Image
    if isinstance(img_input, (str, Path)):
        pil_img = Image.open(str(img_input)).convert("RGB")
    elif isinstance(img_input, bytes):
        pil_img = Image.open(io.BytesIO(img_input)).convert("RGB")
    else:
        pil_img = img_input.convert("RGB")

    width, height = pil_img.size

    if np is None:
        return {
            "is_digital": True,
            "is_unsupported": False,
            "unsupported_type": None,
            "message": "NumPy not loaded, using fallback layout.",
            "boxes": _fallback_default_boxes()["boxes"],
            "detected_count": 5
        }

    is_composite = (width >= 1950 and height >= 2180)
    arr_full = np.array(pil_img)
    if is_composite:
        border = 50
        slot_w = width - border * 2
        slot_h = (height - border * 2) // 2
        chamber_arr = arr_full[border:border + slot_h, border:border + slot_w]
        device_arr = arr_full[border + slot_h:border + slot_h * 2, border:border + slot_w]
    else:
        slot_w = width
        slot_h = height
        chamber_arr = arr_full
        device_arr = None

    result_boxes = []

    # 1. Detect Standard Instrument (Fluke 1502A) in Device Slot
    if device_arr is not None:
        r = device_arr[:, :, 0].astype(int)
        g = device_arr[:, :, 1].astype(int)
        b = device_arr[:, :, 2].astype(int)
        green_lit = (g > 95) & (g > r + 20) & (g > b + 20)
        ys, xs = np.where(green_lit)
        # Isolate main VFD digit panel (filter out incidental top status LEDs at y < 0.25)
        valid_vfd = ys >= int(0.22 * slot_h)
        if np.count_nonzero(valid_vfd) > 60:
            ys_vfd, xs_vfd = ys[valid_vfd], xs[valid_vfd]
            min_x, max_x = int(np.min(xs_vfd)), int(np.max(xs_vfd))
            min_y, max_y = int(np.min(ys_vfd)), int(np.max(ys_vfd))
            pad_w = int((max_x - min_x) * 0.05)
            pad_h = int((max_y - min_y) * 0.12)
            bx = max(0, min_x - pad_w)
            by = max(0, min_y - pad_h)
            bw = min(slot_w - bx, (max_x - min_x) + pad_w * 2)
            bh = min(slot_h - by, (max_y - min_y) + pad_h * 2)
            nx = round(bx / slot_w, 4)
            ny = round(by / slot_h, 4)
            nw = round(bw / slot_w, 4)
            nh = round(bh / slot_h, 4)
            result_boxes.append({
                "id": "box_standard",
                "label": "Standard (Fluke 1502A)",
                "camera_type": "device",
                "target": "device",
                "mode": "quad",
                "x": nx, "y": ny, "w": nw, "h": nh, "width": nw, "height": nh,
                "points": [
                    [nx, ny],
                    [round(nx + nw, 4), ny],
                    [round(nx + nw, 4), round(ny + nh, 4)],
                    [nx, round(ny + nh, 4)]
                ],
                "status": "candidate"
            })

    # 2. Detect Sensors in Chamber Slot via PP-OCRv4 AI text detection
    det_blobs = onnx_ocr_engine.detect_text_blobs(chamber_arr) if onnx_ocr_engine else []
    if det_blobs:
        clusters = []
        for blb in sorted(det_blobs, key=lambda x: x[0]):
            bx, by, bw, bh = blb
            matched = False
            for cl in clusters:
                if abs(cl["cx"] - (bx + bw / 2.0)) < 0.16:
                    cl["boxes"].append(blb)
                    cl["min_x"] = min(cl["min_x"], bx)
                    cl["max_x"] = max(cl["max_x"], bx + bw)
                    cl["min_y"] = min(cl["min_y"], by)
                    cl["max_y"] = max(cl["max_y"], by + bh)
                    cl["cx"] = (cl["min_x"] + cl["max_x"]) / 2.0
                    matched = True
                    break
            if not matched:
                clusters.append({
                    "cx": bx + bw / 2.0,
                    "min_x": bx, "max_x": bx + bw,
                    "min_y": by, "max_y": by + bh,
                    "boxes": [blb]
                })

        disp_idx = 1
        for cl in sorted(clusters, key=lambda c: c["cx"]):
            cl_w = cl["max_x"] - cl["min_x"]
            cl_h = cl["max_y"] - cl["min_y"]
            
            # Frame the temperature readout line on the digital display
            disp_w = 0.150
            disp_h = 0.082
            disp_x = max(0.0, cl["cx"] - 0.075)
            disp_y = max(0.0, cl["min_y"] + 0.026 if cl_h > 0.15 else 0.520)
            if disp_y + disp_h > 0.98:
                disp_y = max(0.0, 0.98 - disp_h)
            
            nx = round(disp_x, 4)
            ny = round(disp_y, 4)
            nw = round(disp_w, 4)
            nh = round(disp_h, 4)
            result_boxes.append({
                "id": f"box_disp_{disp_idx:02d}",
                "label": f"Display {disp_idx:02d} (LCD)",
                "camera_type": "chamber",
                "target": "chamber",
                "mode": "quad",
                "x": nx, "y": ny, "w": nw, "h": nh, "width": nw, "height": nh,
                "points": [
                    [nx, ny],
                    [round(nx + nw, 4), ny],
                    [round(nx + nw, 4), round(ny + nh, 4)],
                    [nx, round(ny + nh, 4)]
                ],
                "status": "candidate"
            })
            disp_idx += 1

    # Fallback to OpenCV contour detection if AI detection found 0 chamber boxes
    if not any(b["camera_type"] == "chamber" for b in result_boxes) and HAS_CV2 and cv2 is not None:
        cv_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
        img_area = width * height
        candidates = []
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 35, 110)
        k_h = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
        k_v = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 7))
        edges_closed = cv2.morphologyEx(cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_h), cv2.MORPH_CLOSE, k_v)
        contours, _ = cv2.findContours(edges_closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        min_screen_area = img_area * 0.004
        max_screen_area = img_area * 0.25
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            if min_screen_area <= area <= max_screen_area and 0.85 <= float(w)/max(1, h) <= 3.4:
                candidates.append((x, y, w, h, area, "lcd"))
        cv_boxes = _nms_filter(candidates, width, height)
        for b in cv_boxes:
            result_boxes.append(b)

    if not result_boxes:
        return {
            "is_digital": False,
            "is_unsupported": False,
            "unsupported_type": None,
            "message": "Tidak terdeteksi display digital yang jelas secara otomatis. Silakan posisikan ROI manual.",
            "boxes": [],
            "detected_count": 0
        }

    return {
        "is_digital": True,
        "is_unsupported": False,
        "unsupported_type": None,
        "message": f"Terdeteksi {len(result_boxes)} display instrumen aktif (Standar & Sensor).",
        "boxes": result_boxes,
        "detected_count": len(result_boxes)
    }


def _nms_filter(candidates: List[Any], img_w: int, img_h: int) -> List[Dict[str, Any]]:
    """
    Filter overlapping bounding boxes, prioritize tight digital display screens,
    and sort cleanly from top to bottom, left to right.
    """
    if not candidates:
        return []

    # Sort candidates by area descending
    sorted_candidates = sorted(candidates, key=lambda c: c[4], reverse=True)
    kept = []

    for item in sorted_candidates:
        x, y, w, h, area = item[0], item[1], item[2], item[3], item[4]
        dtype = item[5] if len(item) > 5 else "lcd"
        overlap = False
        for kx, ky, kw, kh, _, _ in kept:
            # Intersection over Union (IoU) calculation
            ix1 = max(x, kx)
            iy1 = max(y, ky)
            ix2 = min(x + w, kx + kw)
            iy2 = min(y + h, ky + kh)
            iw = max(0, ix2 - ix1)
            ih = max(0, iy2 - iy1)
            inter_area = iw * ih
            union_area = area + (kw * kh) - inter_area
            if union_area > 0 and (inter_area / union_area) > 0.32:
                overlap = True
                break
        if not overlap:
            kept.append((x, y, w, h, area, dtype))
        # Limit to at most 6 readable sensor displays in the chamber
        if len(kept) >= 6:
            break

    # Sort kept boxes: top-to-bottom, left-to-right
    kept = sorted(kept, key=lambda c: (int(c[1] / (img_h * 0.30)), c[0]))

    result = []
    display_idx = 1
    for item in kept:
        x, y, w, h, _, dtype = item
        norm_x = round(x / img_w, 4)
        norm_y = round(y / img_h, 4)
        norm_w = round(w / img_w, 4)
        norm_h = round(h / img_h, 4)

        if dtype == "vfd":
            label = "Standard Fluke (VFD)"
            target_cam = "device"
        else:
            label = f"Display {display_idx:02d} (LCD)"
            target_cam = "chamber"
            display_idx += 1

        points = [
            [norm_x, norm_y],
            [round(norm_x + norm_w, 4), norm_y],
            [round(norm_x + norm_w, 4), round(norm_y + norm_h, 4)],
            [norm_x, round(norm_y + norm_h, 4)]
        ]

        result.append({
            "id": f"box_{len(result) + 1:02d}",
            "label": label,
            "x": norm_x,
            "y": norm_y,
            "w": norm_w,
            "h": norm_h,
            "width": norm_w,
            "height": norm_h,
            "points": points,
            "camera_type": target_cam,
            "target": target_cam,
            "mode": "quad",
            "status": "candidate"
        })

    return result


def _fallback_default_boxes() -> Dict[str, Any]:
    """Default layout when autodetection finds no obvious edges."""
    return {
        "is_analog": False,
        "analog_warning": None,
        "boxes": [
            {"id": "box_01", "label": "Thermo 01", "x": 0.12, "y": 0.20, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_02", "label": "Thermo 02", "x": 0.38, "y": 0.16, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_03", "label": "Thermo 03", "x": 0.64, "y": 0.22, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_04", "label": "Thermo 04", "x": 0.24, "y": 0.52, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"},
            {"id": "box_05", "label": "Thermo 05", "x": 0.52, "y": 0.54, "w": 0.22, "h": 0.24, "target": "chamber", "status": "candidate"}
        ],
        "analog_count": 0
    }


def get_slot_bounds(img_w: int, img_h: int, camera_type: str = "chamber", is_single_feed: bool = False) -> Tuple[int, int, int, int]:
    """
    Computes absolute pixel bounding rectangle (slot_x, slot_y, slot_w, slot_h)
    for the designated camera feed within single or dual stitched composite captures.
    """
    if is_single_feed:
        return 0, 0, img_w, img_h

    cam = (camera_type or "chamber").lower()
    is_device = (cam == "device" or "standard" in cam)

    # 1. Multi-camera vertical stack with 50px outside border (2020 x 2260 default)
    if img_w >= 1950 and img_h >= 2180:
        border = 50
        slot_w = img_w - (border * 2)
        slot_h = (img_h - (border * 2)) // 2
        slot_x = border
        slot_y = border + slot_h if is_device else border
        return slot_x, slot_y, slot_w, slot_h

    # 2. Raw dual vertical stack without twibbon border (e.g. 1920 x 2160, strictly dual 16:9 composite)
    # Only triggers when dimensions match a dual 1080p stack (h >= 2000 and 1800 <= w <= 2100)
    if img_h >= 2000 and 1800 <= img_w <= 2100:
        half_h = img_h // 2
        slot_y = half_h if is_device else 0
        return 0, slot_y, img_w, half_h

    # 3. Single camera snapshot with 50px border (e.g. 2020 x 1180)
    if img_w >= 1950 and 1100 <= img_h <= 1250:
        border = 50
        return border, border, img_w - (border * 2), img_h - (border * 2)

    # 4. Standard raw single feed
    return 0, 0, img_w, img_h


def auto_deskew_and_unwarp_quad(cv_crop: Any) -> Any:
    """
    Detects perspective keystoning, trapezoid distortion, or rotational tilt
    inside a cropped thermometer ROI and warps it into an upright rectangular display.
    """
    if not HAS_CV2 or cv_crop is None or np is None or cv2 is None or cv_crop.size == 0:
        return cv_crop

    h, w = cv_crop.shape[:2]
    if h < 20 or w < 20:
        return cv_crop

    gray = cv2.cvtColor(cv_crop, cv2.COLOR_BGR2GRAY)

    # 1. Try to find quadrilateral LCD glass border or active display area
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 4)
    contours, _ = cv2.findContours(thresh, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    best_quad = None
    min_area = (w * h) * 0.15
    max_area = (w * h) * 0.96
    largest_area = 0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if min_area <= area <= max_area and area > largest_area:
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.035 * peri, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                best_quad = approx
                largest_area = area

    if best_quad is not None:
        pts = best_quad.reshape(4, 2).astype("float32")
        rect = np.zeros((4, 2), dtype="float32")
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)] # TL
        rect[2] = pts[np.argmax(s)] # BR
        diff = np.diff(pts, axis=1)
        rect[1] = pts[np.argmin(diff)] # TR
        rect[3] = pts[np.argmax(diff)] # BL

        tw = int(max(np.linalg.norm(rect[1] - rect[0]), np.linalg.norm(rect[2] - rect[3])))
        th = int(max(np.linalg.norm(rect[3] - rect[0]), np.linalg.norm(rect[2] - rect[1])))
        if tw > 25 and th > 15:
            dst = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]])
            M = cv2.getPerspectiveTransform(rect, dst)
            return cv2.warpPerspective(cv_crop, M, (tw, th), flags=cv2.INTER_CUBIC)

    # 2. Rotated rectangle deskew using minAreaRect
    if contours:
        all_pts = np.vstack(contours)
        min_rect = cv2.minAreaRect(all_pts)
        angle = min_rect[2]
        if angle < -45:
            angle = -(90 + angle)
        elif angle > 45:
            angle = 90 - angle
        else:
            angle = -angle

        if 1.5 <= abs(angle) <= 42.0:
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            return cv2.warpAffine(cv_crop, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    return cv_crop


def extract_roi_region(pil_img: Image.Image, box: Dict[str, Any], is_single_feed: bool = False) -> Tuple[Image.Image, Optional[Any]]:
    """
    Extracts the display region from a composite snapshot using either
    user-defined 4-point quadrilateral perspective pins or normalized bounding box coords.
    Returns (cropped_pil_image, cropped_cv2_image_or_none).
    """
    img_w, img_h = pil_img.size
    cam_type = box.get("camera_type") or box.get("target") or "chamber"
    slot_x, slot_y, slot_w, slot_h = get_slot_bounds(img_w, img_h, cam_type, is_single_feed=is_single_feed)

    cv_full = None
    if HAS_CV2 and np is not None and cv2 is not None:
        cv_full = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    # Case A: User supplied 4-point perspective polygon (Trapezoid / Skew Alignment)
    points = box.get("points") or box.get("quad")
    if points and len(points) == 4:
        poly = []
        for p in points:
            px = float(slot_x + p[0] * slot_w)
            py = float(slot_y + p[1] * slot_h)
            poly.append([px, py])

        tl, tr, br, bl = poly[0], poly[1], poly[2], poly[3]
        # Outward safety padding (6%) so slightly tight manual touch pins do not cut off digit bottoms or decimal points
        cx = (tl[0] + tr[0] + br[0] + bl[0]) / 4.0
        cy = (tl[1] + tr[1] + br[1] + bl[1]) / 4.0
        pad_factor = 1.06
        tl = [cx + (tl[0] - cx) * pad_factor, cy + (tl[1] - cy) * pad_factor]
        tr = [cx + (tr[0] - cx) * pad_factor, cy + (tr[1] - cy) * pad_factor]
        br = [cx + (br[0] - cx) * pad_factor, cy + (br[1] - cy) * pad_factor]
        bl = [cx + (bl[0] - cx) * pad_factor, cy + (bl[1] - cy) * pad_factor]

        target_w = max(40, int(max(math.hypot(tr[0] - tl[0], tr[1] - tl[1]), math.hypot(br[0] - bl[0], br[1] - bl[1]))))
        target_h = max(25, int(max(math.hypot(bl[0] - tl[0], bl[1] - tl[1]), math.hypot(br[0] - tr[0], br[1] - tr[1]))))

        if HAS_CV2 and cv_full is not None:
            src_pts = np.float32([tl, tr, br, bl])
            dst_pts = np.float32([[0, 0], [target_w, 0], [target_w, target_h], [0, target_h]])
            M = cv2.getPerspectiveTransform(src_pts, dst_pts)
            warped_cv = cv2.warpPerspective(cv_full, M, (target_w, target_h), flags=cv2.INTER_CUBIC)
            warped_rgb = cv2.cvtColor(warped_cv, cv2.COLOR_BGR2RGB)
            return Image.fromarray(warped_rgb), warped_cv
        else:
            # Pillow QUAD transform fallback
            quad_data = [tl[0], tl[1], bl[0], bl[1], br[0], br[1], tr[0], tr[1]]
            pil_warped = pil_img.transform((target_w, target_h), Image.Transform.QUAD, data=quad_data, resample=Image.Resampling.BICUBIC)
            return pil_warped, None

    # Case B: Standard bounding box
    norm_x = float(box.get("x", 0))
    norm_y = float(box.get("y", 0))
    norm_w = float(box.get("width") if box.get("width") is not None else box.get("w", 0.2))
    norm_h = float(box.get("height") if box.get("height") is not None else box.get("h", 0.2))

    px = max(0, min(img_w - 10, int(slot_x + norm_x * slot_w)))
    py = max(0, min(img_h - 10, int(slot_y + norm_y * slot_h)))
    pw = max(10, min(img_w - px, int(norm_w * slot_w)))
    ph = max(10, min(img_h - py, int(norm_h * slot_h)))

    crop_pil = pil_img.crop((px, py, px + pw, py + ph))
    crop_cv = None
    if HAS_CV2 and cv_full is not None:
        crop_cv = cv_full[py:py + ph, px:px + pw].copy()
        # Automatically detect and rectify tilt / trapezoid distortion
        crop_cv = auto_deskew_and_unwarp_quad(crop_cv)
        crop_pil = Image.fromarray(cv2.cvtColor(crop_cv, cv2.COLOR_BGR2RGB))

    return crop_pil, crop_cv


def preprocess_for_7segment_ocr(cv_crop: Any, pil_crop: Image.Image, box: Optional[Dict[str, Any]] = None) -> Tuple[Image.Image, str]:
    """
    Optimized preprocessing pipeline for 7-segment LCD and VFD/LED thermometer displays.
    Bridges segment gaps and normalizes contrast for maximum Tesseract OCR precision.
    Supports Fluke 1502A Green VFD readouts as well as Chamber HTC LCD displays.
    """
    cam_type = (box.get("camera_type") or box.get("target") or "").lower() if box else ""
    lbl = str(box.get("label", "")).lower() if box else ""
    is_device_cam = (cam_type == "device" or "standard" in lbl or "fluke" in lbl or "1502" in lbl)

    # -------------------------------------------------------------
    # Fallback when OpenCV is not available
    # -------------------------------------------------------------
    if not HAS_CV2 or cv_crop is None or np is None or cv2 is None or (hasattr(cv_crop, "size") and cv_crop.size == 0):
        # Check green VFD in Pillow
        w, h = pil_crop.size
        im_pixels = pil_crop.load()
        min_x, max_x, min_y, max_y = w, 0, h, 0
        green_count = 0
        for y in range(h):
            for x in range(w):
                r_val, g_val, b_val = im_pixels[x, y][:3]
                if g_val > 65 and g_val > (r_val + 18) and g_val > (b_val + 18):
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)
                    green_count += 1

        if green_count >= max(35, int(w * h * 0.002)) or (is_device_cam and green_count >= 20):
            pad = 16
            x1 = max(0, min_x - pad)
            y1 = max(0, min_y - pad)
            x2 = min(w, max_x + pad)
            y2 = min(h, max_y + pad)
            bw = max(10, x2 - x1)
            bh = max(10, y2 - y1)
            tmask = Image.new("L", (bw, bh), 255)
            tpixels = tmask.load()
            for y in range(y1, y2):
                for x in range(x1, x2):
                    r_val, g_val, b_val = im_pixels[x, y][:3]
                    if g_val > 65 and g_val > (r_val + 16) and g_val > (b_val + 16):
                        tpixels[x - x1, y - y1] = 0
            from PIL import ImageFilter
            dilated = tmask.filter(ImageFilter.MinFilter(3))
            return ImageOps.expand(dilated, border=20, fill=255), "green_vfd"

        # Standard LCD Pillow fallback
        gray = ImageOps.grayscale(pil_crop)
        enhancer = ImageEnhance.Contrast(gray)
        contrast = enhancer.enhance(2.2)
        return ImageOps.expand(contrast, border=15, fill=255), "standard_lcd"

    # -------------------------------------------------------------
    # OpenCV High-Precision Preprocessing Pipeline
    # -------------------------------------------------------------
    h, w = cv_crop.shape[:2]
    b, g, r = cv2.split(cv_crop)
    total_px = float(w * h)

    # 1. Green VFD / LED Readout (Fluke 1502A Standard Thermometer)
    g_diff = cv2.subtract(g, cv2.max(r, b))
    green_count = int(np.count_nonzero(g_diff > 20))
    is_vfd = (
        green_count >= max(35, int(total_px * 0.002))
        or (is_device_cam and green_count >= 20)
    )

    if is_vfd:
        # Isolate luminous green segments
        _, thresh = cv2.threshold(g_diff, 18, 255, cv2.THRESH_BINARY)
        _, g_min = cv2.threshold(g, 125, 255, cv2.THRESH_BINARY)
        thresh = cv2.bitwise_and(thresh, g_min)

        # Auto-crop to active glowing digit bounding box to eliminate buttons, logos, and bezel
        pts = cv2.findNonZero(thresh)
        if pts is not None and len(pts) >= 40:
            bx, by, bw, bh = cv2.boundingRect(pts)
            pad_x = 16
            pad_y = 12
            x1 = max(0, bx - pad_x)
            y1 = max(0, by - pad_y)
            x2 = min(w, bx + bw + pad_x)
            y2 = min(h, by + bh + pad_y)
            thresh = thresh[y1:y2, x1:x2]

        # Morphological Closing: Bridges physical segment gaps so digits form continuous readable glyphs
        k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k_close)

        # Subtle dilation to ensure thick continuous digit strokes and preserve decimal dot
        k_dot = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        reinforced = cv2.dilate(closed, k_dot, iterations=1)

        # Invert: Black text on pure white background
        inverted = cv2.bitwise_not(reinforced)

        # Add 20px white margin so edge digits are never cut off by Tesseract character boundary finder
        padded = cv2.copyMakeBorder(inverted, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=[255, 255, 255])
        return Image.fromarray(padded), "green_vfd"

    # 2. Standard 7-Segment LCD Display (HTC-1 / HTC-2 Black on Grey LCD)
    gray = cv2.cvtColor(cv_crop, cv2.COLOR_BGR2GRAY)

    # CLAHE to equalize lighting across the LCD glass
    clahe = cv2.createCLAHE(clipLimit=2.8, tileGridSize=(6, 6))
    eq = clahe.apply(gray)

    # Bilateral filter to smooth liquid-crystal grain while keeping segment borders sharp
    filtered = cv2.bilateralFilter(eq, 7, 45, 45)

    # Adaptive thresholding
    thresh = cv2.adaptiveThreshold(filtered, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 21, 6)

    # Morphological closing to bridge 7-segment gaps
    k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k_close)

    # Invert to black digits on white background
    inverted = cv2.bitwise_not(closed)
    padded = cv2.copyMakeBorder(inverted, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=[255, 255, 255])
    return Image.fromarray(padded), "standard_lcd"


def extract_thermometer_value(text: str, box: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """
    Parses numeric calibration temperatures and fixes common 7-segment character ambiguities.
    Supports high precision 3-decimal readouts from standard thermometers (e.g. 20.953 from Fluke 1502A).
    """
    if not text:
        return None

    cam_type = (box.get("camera_type") or box.get("target") or "").lower() if box else ""
    lbl = str(box.get("label", "")).lower() if box else ""
    is_standard = (cam_type == "device" or "standard" in lbl or "fluke" in lbl or "1502" in lbl)

    # 1. Normalize spaces around dots: '20 . 953' -> '20.953'
    t = re.sub(r'(\d+)\s*\.\s*(\d+)', r'\1.\2', text)
    # 1b. Fix space-split digits before/after dot: '2 5.34' -> '25.34', '2 5.3' -> '25.3', '25.3 4' -> '25.34'
    t = re.sub(r'(\d+\.\d+)\s+(\d+)', r'\1\2', t)
    t = re.sub(r'\b(\d)\s+(\d\.\d+)\b', r'\1\2', t)
    t = re.sub(r'\b(\d)\s+(\d)\s+(\d)\b', r'\1\2.\3', t)
    # 2. Spaces between digits when dot was omitted: '20 953' -> '20.953'
    t = re.sub(r'(\b\d{1,2})\s+(\d{2,4}\b)', r'\1.\2', t)
    # 3. Replace comma with dot
    t = t.replace(",", ".")
    # 4. Clean 7-segment letter confusions
    t = t.replace("O", "0").replace("o", "0")
    t = t.replace("S", "5").replace("s", "5")
    t = t.replace("B", "8")
    t = t.replace("l", "1").replace("I", "1").replace("|", "1")
    t = t.replace("Z", "2").replace("z", "2")

    # A. Prioritized 3-decimal standard thermometer format (e.g. 20.953 from Fluke 1502A)
    if is_standard:
        # Match standard 3-decimal pattern: e.g. 20.953 in '20.95380', '20.953 C', '20.947'
        m3 = re.search(r'[-+]?\b(\d{1,3}\.\d{3})\b', t)
        if not m3:
            m3 = re.search(r'[-+]?(\d{1,2}\.\d{3})', t)
        if m3:
            try:
                val = float(m3.group(1))
                if -50.0 <= val <= 250.0:
                    return round(val, 3)
            except ValueError:
                pass

        # Match 5 digits without dot: e.g. '2095580', '218341', or '20955 C' where 20955 is the reading
        m5 = re.search(r'\b(\d{5})\d?(?:80|86|88|[cC]|\b)', t)
        if not m5:
            m5 = re.search(r'\b(\d{5})\d?', t)
        if m5:
            try:
                val = float(m5.group(1)) / 1000.0
                if -50.0 <= val <= 250.0:
                    return round(val, 3)
            except ValueError:
                pass

        # Match 4 digits: e.g. '2095' -> 20.95
        m4 = re.search(r'\b(\d{4})\b', t)
        if m4:
            try:
                val = float(m4.group(1)) / 100.0
                if -50.0 <= val <= 250.0:
                    return round(val, 2)
            except ValueError:
                pass

    # B. For chamber thermometers (HTC-2, HTC-1, LCD displays):
    # HTC sensors physically have 1-decimal resolution (e.g. 24.0°C).
    # The degree symbol (°C) touching the last digit is frequently read as an extra digit (e.g. '24.02', '24.07', '24.04', '25.77').
    if not is_standard:
        m_deg = re.search(r'[-+]?\b(\d{1,2}\.\d)\d\b', t)
        if m_deg:
            try:
                val = float(m_deg.group(1))
                if -50.0 <= val <= 250.0:
                    return round(val, 1)
            except ValueError:
                pass

    # Look for standard 1-decimal temperature: e.g. 24.0, 25.1
    m1 = re.search(r'[-+]?\b(\d{1,2}\.\d)\b', t)
    if m1:
        try:
            val = float(m1.group(1))
            if -50.0 <= val <= 250.0:
                return round(val, 1)
        except ValueError:
            pass

    # For chamber thermometers: 3 digits without visible dot (e.g. '250' -> 25.0, '254' -> 25.4, '240' -> 24.0)
    if not is_standard:
        m3_int = re.search(r'\b(\d{3})\b', t)
        if m3_int:
            try:
                val = float(m3_int.group(1)) / 10.0
                if 0.0 <= val <= 99.9:
                    return round(val, 1)
            except ValueError:
                pass

    # C. Floating point numbers with decimal point (e.g. 20.953, 20.5, -5.2, 100.0)
    matches = re.findall(r"[-+]?\d{1,3}\.\d{1,4}", t)
    for m in matches:
        try:
            val = float(m)
            if -50.0 <= val <= 250.0:
                return val
        except ValueError:
            continue

    # D. Standard integer fallback (e.g. '20')
    int_matches = re.findall(r"[-+]?\d{1,3}", t)
    sorted_ints = sorted(int_matches, key=lambda x: len(x.lstrip('-+')), reverse=True)
    for m in sorted_ints:
        try:
            val = float(m)
            # Avoid picking spurious single digit like '2' for chamber thermometer displays
            if not is_standard and len(m.lstrip('-+')) == 1 and val < 10.0:
                continue
            if -50.0 <= val <= 250.0:
                return val
        except ValueError:
            continue

    return None


def perform_display_ocr(pil_img: Image.Image, box: Dict[str, Any], is_single_feed: bool = False) -> Dict[str, Any]:
    """
    Primary: Ultra-fast ONNX AI OCR (PP-OCRv4 CPU optimized).
    Fallback: Specialized Tesseract OCR with 7-segment morphology pipeline if AI model invalid or unconfident.
    """
    # 1. Extract crop with accurate slot coordinate mapping & perspective unwarping
    crop, cv_crop = extract_roi_region(pil_img, box, is_single_feed=is_single_feed)

    # 2. Save high-resolution thumbnail bytes for UI modal & Excel inspection
    thumb_buf = io.BytesIO()
    thumb_img = crop.copy()
    thumb_img.thumbnail((720, 480), Image.Resampling.LANCZOS)
    thumb_img.save(thumb_buf, format="JPEG", quality=92)
    crop_bytes = thumb_buf.getvalue()

    # --- PRIMARY ENGINE: ONNX AI OCR ---
    onnx_text = ""
    onnx_val = None
    onnx_conf = 0.0
    if onnx_ocr_engine:
        input_target = cv_crop if cv_crop is not None else crop

        # Standard / VFD green segment isolation (Fluke 1502A)
        cam_type = (box.get("camera_type") or box.get("target") or "").lower()
        lbl = str(box.get("label", "")).lower()
        is_standard = (cam_type == "device" or "standard" in lbl or "fluke" in lbl or "1502" in lbl)

        crop_arr = np.array(crop) if isinstance(crop, Image.Image) else cv_crop
        onnx_text, onnx_conf = onnx_ocr_engine.recognize(input_target, box=box)
        if onnx_text:
            onnx_val = extract_thermometer_value(onnx_text, box=box)

        # If raw crop did not yield a valid number, attempt green VFD segment isolation as fallback
        if (onnx_val is None or onnx_conf < 0.45) and crop_arr is not None and len(crop_arr.shape) == 3:
            r_c = crop_arr[:, :, 0].astype(int)
            g_c = crop_arr[:, :, 1].astype(int)
            b_c = crop_arr[:, :, 2].astype(int)
            green_lit = (g_c > 110) & (g_c > r_c + 15) & (g_c > b_c + 15)
            if np.count_nonzero(green_lit) > 80:
                clean = np.where(green_lit, 0, 255).astype(np.uint8)
                fallback_target = Image.fromarray(np.stack([clean] * 3, axis=-1))
                alt_text, alt_conf = onnx_ocr_engine.recognize(fallback_target, box=box)
                alt_val = extract_thermometer_value(alt_text, box=box) if alt_text else None
                if alt_val is not None and (onnx_val is None or alt_conf > onnx_conf):
                    onnx_text, onnx_conf, onnx_val = alt_text, alt_conf, alt_val

    # If ONNX AI produced a confident valid numeric reading, return immediately!
    if onnx_val is not None and onnx_conf >= 0.40:
        ver_str = getattr(onnx_ocr_engine, "model_version", "v4").upper()
        logger.info("[OCR Engine] AI (ONNX PP-OCR%s) recognized reading: %s -> %.3f (conf: %.2f)", ver_str, onnx_text, onnx_val, onnx_conf)
        return {
            "text": onnx_text,
            "value": onnx_val,
            "status": "success",
            "confidence": round(onnx_conf, 3),
            "engine": "onnx_ai",
            "model_version": getattr(onnx_ocr_engine, "model_version", "v6_small"),
            "display_type": "onnx_ai",
            "crop_bytes": crop_bytes
        }

    # --- FALLBACK ENGINE: TESSERACT OCR ---
    # Triggered automatically if ONNX AI was missing or returned invalid / non-numeric text
    logger.info("[OCR Engine] AI ONNX returned '%s' (val=%s, conf=%.2f). Engaging Tesseract fallback...", onnx_text, onnx_val, onnx_conf)

    if not HAS_TESSERACT:
        return {
            "text": onnx_text or "OCR Engine N/A",
            "value": onnx_val,
            "status": "warning" if onnx_val is not None else "error",
            "engine": "onnx_ai" if onnx_val is not None else "none",
            "crop_bytes": crop_bytes
        }

    # Preprocess for 7-Segment / VFD
    cleaned_ocr_img, display_type = preprocess_for_7segment_ocr(cv_crop, crop, box=box)

    # Tesseract OCR invocation with specialized PSM modes and Whitelist
    tess_prefix_arg = ""
    custom_tess_dir = os.environ.get("TESSDATA_DIR")
    check_dirs = [custom_tess_dir, "/app/data/tessdata", str(Path(__file__).parent / "data" / "tessdata")]
    for cd in check_dirs:
        if cd and os.path.isdir(cd):
            try:
                if any(f.endswith(".traineddata") for f in os.listdir(cd)):
                    tess_prefix_arg = f'--tessdata-dir "{cd}" '
                    break
            except Exception:
                pass

    ocr_configs = [
        f"{tess_prefix_arg}--psm 7 -c tessedit_char_whitelist=0123456789.-C% ",
        f"{tess_prefix_arg}--psm 8 -c tessedit_char_whitelist=0123456789.-C% ",
        f"{tess_prefix_arg}--psm 6 -c tessedit_char_whitelist=0123456789.-C% ",
        f"{tess_prefix_arg}--psm 13 -c tessedit_char_whitelist=0123456789.-C% ",
        f"{tess_prefix_arg}--psm 7 "
    ]

    tess_text = ""
    tess_val = None

    for cfg in ocr_configs:
        try:
            res = pytesseract.image_to_string(cleaned_ocr_img, config=cfg).strip()
            if res:
                v = extract_thermometer_value(res, box=box)
                if v is not None:
                    tess_text = res
                    tess_val = v
                    break
                elif not tess_text:
                    tess_text = res
        except Exception:
            pass

    if tess_val is None and tess_text:
        tess_val = extract_thermometer_value(tess_text, box=box)

    final_val = tess_val if tess_val is not None else onnx_val
    final_text = tess_text if tess_val is not None else (onnx_text or tess_text or "N/A")
    engine_used = "tesseract_fallback" if tess_val is not None else ("onnx_ai" if onnx_val is not None else "failed")

    status = "error"
    if final_val is not None:
        status = "success"
    elif final_text and final_text != "N/A":
        status = "warning"
    else:
        final_text = "N/A"

    return {
        "text": final_text,
        "value": final_val,
        "status": status,
        "engine": engine_used,
        "confidence": round(onnx_conf, 3),
        "display_type": display_type,
        "crop_bytes": crop_bytes
    }


def generate_project_excel(project_dir: Path, arg2: Any, arg3: Any = None) -> bytes:
    """
    Processes all snapshots in a project directory and builds an Excel calibration sheet
    with embedded thumbnail images and OCR readings.
    Supports both signatures: (project_dir, boxes, project_name) or (project_dir, project_name, boxes).
    """
    if isinstance(arg2, str):
        project_name = arg2
        boxes_config = arg3 or []
    else:
        boxes_config = arg2 or []
        project_name = str(arg3) if arg3 else project_dir.name

    if not HAS_OPENPYXL:
        raise RuntimeError("openpyxl library is required to generate Excel reports.")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Calibration OCR Results"
    ws.views.sheetView[0].showGridLines = True

    # Styling Palettes
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid") # Dark Slate
    sub_fill = PatternFill(start_color="0284C7", end_color="0284C7", fill_type="solid")   # Vibrant Cyan Blue
    
    cell_font = Font(name="Calibri", size=10)
    num_font = Font(name="Consolas", size=13, bold=True)
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center")
    
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1")
    )

    # Title Banner Row
    ws.merge_cells("A1:G1")
    title_cell = ws["A1"]
    title_cell.value = f"LUMENLUX CALIBRATION STUDIO — PROJECT: {project_name.upper()}"
    title_cell.font = Font(name="Calibri", size=14, bold=True, color="0284C7")
    title_cell.alignment = left_align
    ws.row_dimensions[1].height = 28

    # Table Header Definition
    # Structure: No | Timestamp | Filename | [Img Columns for each box] | [OCR Columns for each box]
    headers = ["No", "Timestamp", "Capture Filename"]
    box_labels = [b.get("label", f"Thermo {i+1}") for i, b in enumerate(boxes_config)]
    
    for lbl in box_labels:
        headers.append(f"{lbl} (Image)")
    for lbl in box_labels:
        headers.append(f"{lbl} (OCR °C)")

    # Write Headers at Row 3
    for col_idx, header_text in enumerate(headers, start=1):
        cell = ws.cell(row=3, column=col_idx, value=header_text)
        cell.font = header_font
        cell.fill = sub_fill if "(Image)" in header_text else header_fill
        cell.alignment = center_align
        cell.border = thin_border

    ws.row_dimensions[3].height = 26

    # Discover snapshot files in project directory
    image_files = sorted(
        list(project_dir.glob("*.jpg")) + list(project_dir.glob("*.png")) + list(project_dir.glob("*.jpeg")),
        key=lambda p: p.stat().st_mtime
    )

    current_row = 4
    temp_dir = project_dir / ".excel_thumbs"
    temp_dir.mkdir(parents=True, exist_ok=True)

    for snap_idx, img_path in enumerate(image_files, start=1):
        ws.row_dimensions[current_row].height = 130  # Generous height to clearly show thermometer screen display

        # Read image
        try:
            pil_snap = Image.open(img_path).convert("RGB")
        except Exception as e:
            logger.error("Could not open snapshot %s: %s", img_path.name, e)
            continue

        timestamp_str = img_path.stem.replace("LumenLux_", "").replace("_", " ")

        # 1. Meta columns
        c_no = ws.cell(row=current_row, column=1, value=snap_idx)
        c_no.alignment = center_align
        c_no.border = thin_border
        c_no.font = cell_font

        c_time = ws.cell(row=current_row, column=2, value=timestamp_str)
        c_time.alignment = center_align
        c_time.border = thin_border
        c_time.font = cell_font

        c_file = ws.cell(row=current_row, column=3, value=img_path.name)
        c_file.alignment = left_align
        c_file.border = thin_border
        c_file.font = cell_font

        # Process each designated bounding box
        ocr_results = []
        col_offset = 4

        for b_idx, box in enumerate(boxes_config):
            res = perform_display_ocr(pil_snap, box)
            ocr_results.append(res)

            # Insert Image thumbnail into cell
            thumb_col = col_offset + b_idx
            thumb_cell = ws.cell(row=current_row, column=thumb_col)
            thumb_cell.border = thin_border
            thumb_cell.alignment = center_align

            if res.get("crop_bytes"):
                thumb_file_path = temp_dir / f"r{current_row}_b{b_idx}.jpg"
                with open(thumb_file_path, "wb") as f:
                    f.write(res["crop_bytes"])
                
                # Add to worksheet
                try:
                    xl_img = OpenpyxlImage(str(thumb_file_path))
                    # Scale image proportionally to clearly show thermometer screen (up to 260x125 px)
                    target_w, target_h = 260, 125
                    orig_w, orig_h = xl_img.width, xl_img.height
                    if orig_w > 0 and orig_h > 0:
                        aspect_ratio = orig_w / float(orig_h)
                        if aspect_ratio >= (target_w / target_h):
                            xl_img.width = target_w
                            xl_img.height = max(30, int(target_w / aspect_ratio))
                        else:
                            xl_img.height = target_h
                            xl_img.width = max(30, int(target_h * aspect_ratio))
                    else:
                        xl_img.width = target_w
                        xl_img.height = target_h

                    col_letter = get_column_letter(thumb_col)
                    ws.add_image(xl_img, f"{col_letter}{current_row}")
                except Exception as ex:
                    logger.warning("Could not embed image: %s", ex)

        # Write OCR Values in following columns
        text_col_offset = col_offset + len(boxes_config)
        for b_idx, res in enumerate(ocr_results):
            txt_col = text_col_offset + b_idx
            txt_cell = ws.cell(row=current_row, column=txt_col)
            txt_cell.value = res.get("value") if res.get("value") is not None else res.get("text", "N/A")
            txt_cell.font = num_font
            txt_cell.alignment = center_align
            txt_cell.border = thin_border

            # Subtle color indication in Excel
            st = res.get("status")
            if st == "success":
                txt_cell.fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid") # Soft Green
            elif st == "warning":
                txt_cell.fill = PatternFill(start_color="FEF9C3", end_color="FEF9C3", fill_type="solid") # Soft Yellow
            else:
                txt_cell.fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid") # Soft Red

        current_row += 1

    # Adjust Column Widths
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 30

    for i in range(len(boxes_config)):
        img_col = get_column_letter(4 + i)
        ocr_col = get_column_letter(4 + len(boxes_config) + i)
        ws.column_dimensions[img_col].width = 38
        ws.column_dimensions[ocr_col].width = 20

    # Save to buffer
    output_stream = io.BytesIO()
    wb.save(output_stream)
    
    # Cleanup temp thumbnail cache
    try:
        for p in temp_dir.glob("*.jpg"):
            p.unlink(missing_ok=True)
        temp_dir.rmdir()
    except Exception:
        pass

    return output_stream.getvalue()
