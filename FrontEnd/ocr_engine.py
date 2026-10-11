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
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    cv2 = None
    np = None

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.drawing.image import Image as OpenpyxlImage
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

logger = logging.getLogger("ocr_engine")


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
    Detect whether the image contains digital thermometer displays vs unsupported non-digital meters
    (analog dials, mercury-in-glass, plastic strips, or missing display).
    Only returns valid bounding boxes for confirmed digital displays.
    """
    # 1. Load PIL Image
    if isinstance(img_input, (str, Path)):
        pil_img = Image.open(str(img_input)).convert("RGB")
    elif isinstance(img_input, bytes):
        pil_img = Image.open(io.BytesIO(img_input)).convert("RGB")
    else:
        pil_img = img_input.convert("RGB")

    width, height = pil_img.size

    if not HAS_CV2 or np is None or cv2 is None:
        return {
            "is_digital": True,
            "is_unsupported": False,
            "unsupported_type": None,
            "message": "OpenCV / NumPy not loaded, using fallback layout.",
            "boxes": _fallback_default_boxes()["boxes"],
            "detected_count": 5
        }

    cv_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)

    # 1. Check for known non-digital instruments (Analog Dials, Mercury Glass)
    non_digital_check = classify_non_digital_meter(cv_img)
    if non_digital_check is not None:
        return {
            "is_digital": False,
            "is_unsupported": True,
            "unsupported_type": non_digital_check["unsupported_type"],
            "message": non_digital_check["message"],
            "boxes": [],
            "detected_count": 0
        }

    # 2. Check for Rectangular Digital LCD / VFD screens (HTC-1/HTC-2 or Fluke 1502A)
    blurred = cv2.bilateralFilter(gray, 9, 75, 75)
    thresh = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 21, 5
    )

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []

    img_area = width * height
    min_box_area = img_area * 0.015  # At least 1.5% of total frame
    max_box_area = img_area * 0.50   # At most 50% of total frame

    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = w * h
        if area < min_box_area or area > max_box_area:
            continue
        aspect = float(w) / max(1, h)
        if 0.75 <= aspect <= 3.6:
            candidates.append((x, y, w, h, area))

    # Also check bright green Fluke VFD readout
    hsv = cv2.cvtColor(cv_img, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, np.array([35, 70, 70]), np.array([85, 255, 255]))
    green_cnts, _ = cv2.findContours(green_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in green_cnts:
        x, y, w, h = cv2.boundingRect(cnt)
        if (w * h) > (img_area * 0.01):
            pad_x = int(w * 0.15)
            pad_y = int(h * 0.25)
            x0 = max(0, x - pad_x)
            y0 = max(0, y - pad_y)
            w0 = min(width - x0, w + pad_x * 2)
            h0 = min(height - y0, h + pad_y * 2)
            candidates.append((x0, y0, w0, h0, w0 * h0))

    # Apply NMS
    candidate_boxes = _nms_filter(candidates, width, height)

    # 3. VERIFY EACH CANDIDATE BOX FOR TRUE DIGITAL DISPLAY CHARACTERISTICS
    verified_boxes = []
    for box in candidate_boxes:
        bx = int(box["x"] * width)
        by = int(box["y"] * height)
        bw = int(box["w"] * width)
        bh = int(box["h"] * height)

        crop_region = cv_img[by:by+bh, bx:bx+bw]
        is_dig, conf, reason = verify_is_digital_display(crop_region)
        if is_dig:
            box["status"] = "success" if conf > 0.8 else "candidate"
            box["verified_digital"] = True
            verified_boxes.append(box)

    # 4. Strict Rejection if No Digital Displays Were Found
    if not verified_boxes:
        return {
            "is_digital": False,
            "is_unsupported": True,
            "unsupported_type": "no_digital_display",
            "message": (
                "Tidak terdeteksi display termometer digital yang aktif pada frame kamera. "
                "LumenLux hanya mendukung termometer digital berlayar LCD/LED/VFD dengan angka 7-segmen aktif. "
                "Termometer air raksa (kaca), dial jarum, dan strip plastik tidak didukung."
            ),
            "boxes": [],
            "detected_count": 0
        }

    return {
        "is_digital": True,
        "is_unsupported": False,
        "unsupported_type": None,
        "message": f"Terdeteksi {len(verified_boxes)} display termometer digital aktif.",
        "boxes": verified_boxes,
        "detected_count": len(verified_boxes)
    }


def _nms_filter(candidates: List[Tuple[int, int, int, int, float]], img_w: int, img_h: int) -> List[Dict[str, Any]]:
    """Filter overlapping rectangles and sort from left to right, top to bottom."""
    if not candidates:
        return []

    # Sort by area descending
    candidates = sorted(candidates, key=lambda c: c[4], reverse=True)
    kept = []

    for x, y, w, h, area in candidates:
        overlap = False
        for kx, ky, kw, kh, _ in kept:
            # Compute intersection
            ix1 = max(x, kx)
            iy1 = max(y, ky)
            ix2 = min(x + w, kx + kw)
            iy2 = min(y + h, ky + kh)
            iw = max(0, ix2 - ix1)
            ih = max(0, iy2 - iy1)
            inter_area = iw * ih
            union_area = area + (kw * kh) - inter_area
            if union_area > 0 and (inter_area / union_area) > 0.35:
                overlap = True
                break
        if not overlap:
            kept.append((x, y, w, h, area))
        if len(kept) >= 6:
            break

    # Sort remaining boxes logically: top-to-bottom, left-to-right
    kept = sorted(kept, key=lambda c: (int(c[1] / (img_h * 0.35)), c[0]))

    result = []
    for idx, (x, y, w, h, _) in enumerate(kept):
        norm_x = round(x / img_w, 4)
        norm_y = round(y / img_h, 4)
        norm_w = round(w / img_w, 4)
        norm_h = round(h / img_h, 4)
        label = "Standard" if idx == 0 and norm_w > 0.35 else f"Thermo {idx + 1:02d}"
        result.append({
            "id": f"box_{idx + 1:02d}",
            "label": label,
            "x": norm_x,
            "y": norm_y,
            "w": norm_w,
            "h": norm_h,
            "target": "device" if label == "Standard" else "chamber",
            "status": "candidate"  # candidate (grey), success (green), warning (yellow), error (red)
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
        _, g_min = cv2.threshold(g, 65, 255, cv2.THRESH_BINARY)
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

    # A. Floating point numbers with decimal point (e.g. 20.953, 20.955, 20.947, 20.5, -5.2, 100.0)
    matches = re.findall(r"[-+]?\d{1,3}\.\d{1,4}", t)
    for m in matches:
        try:
            val = float(m)
            if -50.0 <= val <= 250.0:
                return val
        except ValueError:
            continue

    # B. If 5-digit number without dot (e.g. '20953' from Fluke 1502A which has 3 decimals)
    five_digit = re.findall(r"\b\d{5}\b", t)
    for m in five_digit:
        try:
            val = float(m) / 1000.0
            if -50.0 <= val <= 250.0:
                return round(val, 3)
        except ValueError:
            continue

    # C. If 4-digit number on standard thermometer (e.g. '2095' -> 20.95)
    if is_standard:
        four_digit = re.findall(r"\b\d{4}\b", t)
        for m in four_digit:
            try:
                val = float(m) / 100.0
                if -50.0 <= val <= 250.0:
                    return round(val, 2)
            except ValueError:
                continue

    # D. Standard integer fallback (e.g. '20')
    int_matches = re.findall(r"[-+]?\d{1,3}", t)
    for m in int_matches:
        try:
            val = float(m)
            if -50.0 <= val <= 250.0:
                return val
        except ValueError:
            continue

    return None


def perform_display_ocr(pil_img: Image.Image, box: Dict[str, Any], is_single_feed: bool = False) -> Dict[str, Any]:
    """
    Extracts the designated ROI display region with perspective unwarping,
    performs segment-gap bridging preprocessing, and extracts numeric temperature readout.
    """
    # 1. Extract crop with accurate slot coordinate mapping & perspective unwarping
    crop, cv_crop = extract_roi_region(pil_img, box, is_single_feed=is_single_feed)

    # 2. Save high-resolution thumbnail bytes for Excel inspection
    thumb_buf = io.BytesIO()
    thumb_img = crop.copy()
    thumb_img.thumbnail((720, 480), Image.Resampling.LANCZOS)
    thumb_img.save(thumb_buf, format="JPEG", quality=92)
    crop_bytes = thumb_buf.getvalue()

    if not HAS_TESSERACT:
        return {
            "text": "OCR Engine N/A",
            "value": None,
            "status": "warning",
            "crop_bytes": crop_bytes
        }

    # 3. Preprocess for 7-Segment / VFD
    cleaned_ocr_img, display_type = preprocess_for_7segment_ocr(cv_crop, crop, box=box)

    # 4. Tesseract OCR invocation with specialized PSM modes and Whitelist
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

    extracted_text = ""
    cleaned_val = None

    for cfg in ocr_configs:
        try:
            res = pytesseract.image_to_string(cleaned_ocr_img, config=cfg).strip()
            if res:
                v = extract_thermometer_value(res, box=box)
                if v is not None:
                    extracted_text = res
                    cleaned_val = v
                    break
                elif not extracted_text:
                    extracted_text = res
        except Exception:
            pass

    if cleaned_val is None and extracted_text:
        cleaned_val = extract_thermometer_value(extracted_text, box=box)

    status = "error"
    if cleaned_val is not None:
        status = "success"
    elif extracted_text and extracted_text != "N/A":
        status = "warning"
    else:
        extracted_text = "N/A"

    return {
        "text": extracted_text,
        "value": cleaned_val,
        "status": status,
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
