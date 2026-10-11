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
    green_mean = float(np.mean(g))
    red_mean = float(np.mean(r))
    if green_mean > (red_mean + 15) and green_mean > 35:
        # High green dominance with high contrast segments
        _, g_thresh = cv2.threshold(g, 60, 255, cv2.THRESH_BINARY)
        non_zero = cv2.countNonZero(g_thresh)
        fill_ratio = non_zero / float(w * h)
        if 0.02 <= fill_ratio <= 0.65:
            return True, 0.95, "Green VFD/LED digital display detected"

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


def perform_display_ocr(pil_img: Image.Image, box: Dict[str, Any]) -> Dict[str, Any]:
    """
    Crops the designated ROI bounding box, performs adaptive 7-segment preprocessing,
    and extracts numeric thermometer readout.
    """
    img_w, img_h = pil_img.size
    
    # Calculate pixel coordinates with safe boundaries
    x = max(0, int(box.get("x", 0) * img_w))
    y = max(0, int(box.get("y", 0) * img_h))
    w = max(10, int(box.get("w", 0.2) * img_w))
    h = max(10, int(box.get("h", 0.2) * img_h))

    crop = pil_img.crop((x, y, min(img_w, x + w), min(img_h, y + h)))
    
    # Save thumbnail bytes for Excel (high resolution so screen readout is clearly inspectable)
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

    # Preprocessing pipeline optimized for LCD segment displays and LED matrix readouts:
    # 1. Grayscale & Contrast Boost
    gray = ImageOps.grayscale(crop)
    enhancer = ImageEnhance.Contrast(gray)
    contrast_img = enhancer.enhance(2.0)

    # 2. Check if green VFD / LED readout (like Fluke 1502A) vs black LCD on gray
    # Convert crop to numpy for smart thresholding
    crop_np = np.array(crop) if np is not None else None
    
    # Verify that the cropped box actually contains an active digital display
    if HAS_CV2 and crop_np is not None:
        cv_crop = cv2.cvtColor(crop_np, cv2.COLOR_RGB2BGR)
        is_dig, conf, reason = verify_is_digital_display(cv_crop)
        if not is_dig:
            return {
                "text": "NON-DIGITAL (UNSUPPORTED)",
                "value": None,
                "status": "error",
                "is_digital": False,
                "message": f"Region does not contain an active digital display ({reason}).",
                "crop_bytes": crop_bytes
            }

    cleaned_ocr_img = contrast_img
    if crop_np is not None and HAS_CV2:
        cv_crop = cv2.cvtColor(crop_np, cv2.COLOR_RGB2BGR)
        # Check green channel dominance (Fluke readout)
        b, g, r = cv2.split(cv_crop)
        green_mean = float(np.mean(g))
        red_mean = float(np.mean(r))
        
        if green_mean > (red_mean + 15) and green_mean > 40:
            # Green LED readout: threshold green channel and invert to black text on white
            _, thresh = cv2.threshold(g, 70, 255, cv2.THRESH_BINARY)
            # Dilation to merge segment gaps
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
            dilated = cv2.dilate(thresh, kernel, iterations=1)
            # Invert for Tesseract (black text on white background)
            inverted = cv2.bitwise_not(dilated)
            cleaned_ocr_img = Image.fromarray(inverted)
        else:
            # Standard LCD (black segments on gray background)
            gray_cv = cv2.cvtColor(cv_crop, cv2.COLOR_BGR2GRAY)
            # Otsu's thresholding
            _, otsu = cv2.threshold(gray_cv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            cleaned_ocr_img = Image.fromarray(otsu)

    # Tesseract OCR invocation
    # If custom .traineddata models exist in TESSDATA_DIR, prioritize them via --tessdata-dir
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

    # PSM 7: Treat the image as a single text line. PSM 6: Single uniform block of text.
    ocr_configs = [
        f"{tess_prefix_arg}--psm 7 -c tessedit_char_whitelist=0123456789.-C% ",
        f"{tess_prefix_arg}--psm 6 -c tessedit_char_whitelist=0123456789.-C% ",
        f"{tess_prefix_arg}--psm 8 -c tessedit_char_whitelist=0123456789.- "
    ]

    extracted_text = ""
    for cfg in ocr_configs:
        try:
            res = pytesseract.image_to_string(cleaned_ocr_img, config=cfg).strip()
            if res:
                extracted_text = res
                break
        except Exception:
            pass

    # Clean & normalize extracted numeric temperature / humidity
    # Extract floating point numbers (e.g. 25.066, 15.082, 20.9, 19.9, 42)
    cleaned_val = None
    status = "error" # red, yellow, green
    
    # Regex for decimal number
    matches = re.findall(r"[-+]?\d+(?:[.,]\d+)?", extracted_text)
    if matches:
        first_num_str = matches[0].replace(",", ".")
        try:
            cleaned_val = float(first_num_str)
            # Temperature plausibility check (-50 to 150 C)
            if -50.0 <= cleaned_val <= 150.0:
                status = "success"  # green
            else:
                status = "warning"  # yellow
        except ValueError:
            status = "warning"
    elif extracted_text:
        status = "warning"  # yellow
    else:
        status = "error"    # red
        extracted_text = "N/A"

    return {
        "text": extracted_text,
        "value": cleaned_val,
        "status": status,
        "crop_bytes": crop_bytes
    }


def generate_project_excel(project_dir: Path, boxes_config: List[Dict[str, Any]], project_name: str) -> bytes:
    """
    Processes all snapshots in a project directory and builds an Excel calibration sheet
    with embedded thumbnail images and OCR readings.
    """
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
