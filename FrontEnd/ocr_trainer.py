from __future__ import annotations
"""
LumenLux PyTorch CPU 7-Segment Digit Classifier & Training Studio.
Provides:
- ROI crop digit segmentation & 2-channel preprocessing (Green/Normalized + CLAHE)
  with Flat-Field illumination correction, Glare Inpainting, and Light Denoising.
- Image Quality Gate (Laplacian variance blur check + glare saturation check).
- Auto-learned multi-decimal formatting per device/box & digit count from training labels.
- Synthetic 7-segment digit generator (lit segments + faint ghost segments + glare + blur/noise)
  mixed 50/50 with real crops during training while validating strictly on real held-out frames.
- CPU Training loop with class-balanced weighting, label smoothing, early stopping,
  whole-reading accuracy tracking, and 7-segment confusion matrix diagnostics (1<->7, 3<->9, 5<->6, 0<->8).
- CPU Inference with Test-Time Augmentation (TTA), intra_op_num_threads=1, and Hard-Example Mining (< 0.90 conf).
"""
import io
import os
import re
import json
import time
import math
import uuid
import base64
import random
import logging
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageOps, ImageDraw, ImageFilter

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path: Any = None, override: bool = False) -> bool:
        if dotenv_path and Path(dotenv_path).is_file():
            for line in Path(dotenv_path).read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if override or k not in os.environ:
                    os.environ[k] = v
            return True
        return False

import numpy as np

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    cv2 = None
    HAS_CV2 = False

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.utils.data import Dataset, DataLoader
    from torchvision import datasets, transforms
    HAS_TORCH = True
except ImportError:
    torch = None
    nn = None
    F = None
    Dataset = object
    DataLoader = None
    datasets = None
    transforms = None
    HAS_TORCH = False

logger = logging.getLogger("ocr_trainer")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def _resolve_env_path(env_key: str, default_rel: str) -> Path:
    raw = os.environ.get(env_key, "").strip()
    if raw:
        if os.name == "nt" and (raw.startswith("/app") or raw.startswith("\\app")):
            rel_part = raw.replace("/app/", "").replace("/app", "").replace("\\app\\", "").replace("\\app", "").strip("/\\")
            return (BASE_DIR / rel_part) if rel_part else (BASE_DIR / default_rel)
        return Path(raw)
    return BASE_DIR / default_rel


def get_train_config() -> Dict[str, Any]:
    """Read all training, filtering, and inference parameters dynamically from .env."""
    load_dotenv(BASE_DIR / ".env", override=True)
    train_data_dir = _resolve_env_path("TRAIN_DATA_DIR", "data/models/training_data")
    custom_model_path = _resolve_env_path("CUSTOM_MODEL_PATH", "data/models/custom_7seg_cnn.pt")
    return {
        "enable_custom_cnn": os.environ.get("ENABLE_CUSTOM_CNN", "true").strip().lower() in ("true", "1", "yes"),
        "train_data_dir": str(train_data_dir),
        "custom_model_path": str(custom_model_path),
        "train_cpu_threads": max(1, min(16, int(os.environ.get("TRAIN_CPU_THREADS", "6")))),
        "inference_intra_op_threads": max(1, int(os.environ.get("INFERENCE_INTRA_OP_THREADS", "1"))),
        "train_epochs": max(5, min(200, int(os.environ.get("TRAIN_EPOCHS", "35")))),
        "train_batch_size": max(4, min(128, int(os.environ.get("TRAIN_BATCH_SIZE", "32")))),
        "train_lr": float(os.environ.get("TRAIN_LR", "0.001")),
        "train_val_split": max(0.05, min(0.5, float(os.environ.get("TRAIN_VAL_SPLIT", "0.20")))),
        "train_hold_out_frames": os.environ.get("TRAIN_HOLD_OUT_FRAMES", "true").strip().lower() in ("true", "1", "yes"),
        "augment_brightness": float(os.environ.get("TRAIN_AUGMENT_BRIGHTNESS", "0.30")),
        "augment_contrast": float(os.environ.get("TRAIN_AUGMENT_CONTRAST", "0.35")),
        "augment_shift": float(os.environ.get("TRAIN_AUGMENT_SHIFT", "0.06")),
        "augment_shear": float(os.environ.get("TRAIN_AUGMENT_SHEAR", "8.0")),
        "augment_warp": float(os.environ.get("TRAIN_AUGMENT_WARP", "0.08")),
        "label_smoothing": float(os.environ.get("TRAIN_LABEL_SMOOTHING", "0.08")),
        "early_stop_patience": max(4, int(os.environ.get("TRAIN_EARLY_STOP_PATIENCE", "12"))),
        "use_synthetic": os.environ.get("TRAIN_USE_SYNTHETIC", "true").strip().lower() in ("true", "1", "yes"),
        "synthetic_mix_ratio": max(0.0, min(0.8, float(os.environ.get("TRAIN_SYNTHETIC_MIX_RATIO", "0.50")))),
        "two_channel_clahe": os.environ.get("TRAIN_TWO_CHANNEL_CLAHE", "true").strip().lower() in ("true", "1", "yes"),
        "enable_flat_field": os.environ.get("TRAIN_ENABLE_FLAT_FIELD", "true").strip().lower() in ("true", "1", "yes"),
        "enable_glare_inpaint": os.environ.get("TRAIN_ENABLE_GLARE_INPAINT", "true").strip().lower() in ("true", "1", "yes"),
        "glare_threshold": int(os.environ.get("GLARE_SATURATION_THRESHOLD", "250")),
        "laplacian_min_var": float(os.environ.get("LAPLACIAN_MIN_VAR", "15.0")),
        "max_glare_ratio": float(os.environ.get("MAX_GLARE_RATIO", "0.18")),
        "auto_learn_decimals": os.environ.get("AUTO_LEARN_DECIMALS", "true").strip().lower() in ("true", "1", "yes"),
        "chamber_fixed_decimals": int(os.environ.get("CHAMBER_FIXED_DECIMALS", "1")),
        "standard_fixed_decimals": int(os.environ.get("STANDARD_FIXED_DECIMALS", "3")),
        "conf_threshold": float(os.environ.get("CUSTOM_CNN_CONF_THRESHOLD", "0.88")),
        "hard_example_conf_threshold": float(os.environ.get("HARD_EXAMPLE_CONF_THRESHOLD", "0.90")),
        "tta_shifts": max(1, min(9, int(os.environ.get("TTA_SHIFTS", "5")))),
        "cross_check_max_delta_c": float(os.environ.get("CROSS_CHECK_MAX_DELTA_C", "2.5")),
        "temporal_max_step_c": float(os.environ.get("TEMPORAL_MAX_STEP_C", "5.0")),
    }


DEFAULT_CLASSES = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "blank"]

_CATALOG_LOCK = threading.RLock()


def _load_catalog(catalog_path: Path) -> List[Dict[str, Any]]:
    if catalog_path.is_file():
        try:
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _write_catalog(catalog_path: Path, catalog: List[Dict[str, Any]]) -> None:
    """Atomic write so a crash / concurrent reader never sees a half-written catalog."""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = catalog_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(catalog), encoding="utf-8")
    os.replace(tmp, catalog_path)
DIGIT_IMG_H = 48
DIGIT_IMG_W = 32


# ==============================================================================
# 0. IMAGE QUALITY GATE, GLARE INPAINTING & FLAT-FIELD ILLUMINATION FILTERING
# ==============================================================================

def assess_crop_quality(pil_crop: Image.Image) -> Dict[str, Any]:
    """
    Quality gate: checks if an ROI crop is blurry (low Laplacian variance)
    or over-exposed (excessive saturated glare pixels > 250).
    """
    cfg = get_train_config()
    rgb = np.array(pil_crop.convert("RGB"))
    h, w = rgb.shape[:2]
    if h < 10 or w < 15:
        return {"is_ok": False, "sharpness": 0.0, "glare_ratio": 0.0, "warnings": ["ROI crop too small"]}

    gray = np.mean(rgb, axis=2).astype(np.uint8)
    if HAS_CV2 and cv2 is not None:
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        sharpness = float(lap.var())
    else:
        gx = np.diff(gray.astype(np.float32), axis=1)
        gy = np.diff(gray.astype(np.float32), axis=0)
        sharpness = float(np.var(gx) + np.var(gy))

    sat_thr = int(cfg["glare_threshold"])
    glare_px = int(np.count_nonzero(np.max(rgb, axis=2) >= sat_thr))
    glare_ratio = float(glare_px) / float(max(1, h * w))

    warnings = []
    if sharpness < float(cfg["laplacian_min_var"]):
        warnings.append(f"Low sharpness ({sharpness:.1f} < {cfg['laplacian_min_var']}) — check focus")
    if glare_ratio > float(cfg["max_glare_ratio"]):
        warnings.append(f"High glare saturation ({glare_ratio * 100:.1f}%) — adjust LED angle")

    return {
        "is_ok": len(warnings) == 0,
        "sharpness": round(sharpness, 1),
        "glare_ratio": round(glare_ratio, 4),
        "warnings": warnings
    }


def apply_glare_and_flat_field_filter(gray_img: np.ndarray, is_emissive: bool = False) -> np.ndarray:
    """
    1. Glare handling: detects saturated specular glare pixels (> 250) on reflective LCDs
       and inpaints them via cv2.inpaint (or clips them) so glare doesn't count as segments.
    2. Flat-field correction: divides the ROI by a heavily blurred background copy of itself
       to remove lighting gradients and soft bezel shadows while keeping digits crisp.
    3. Light denoising only: small bilateral/median filter without harsh sharpening.
    """
    cfg = get_train_config()
    work = gray_img.copy()
    h, w = work.shape[:2]
    if h < 8 or w < 8:
        return work

    # 1. Glare inpainting on reflective LCDs (skip on emissive VFDs where bright pixels ARE the segments)
    if not is_emissive and cfg["enable_glare_inpaint"]:
        sat_thr = int(cfg["glare_threshold"])
        glare_mask = (work >= sat_thr).astype(np.uint8) * 255
        glare_cnt = int(np.count_nonzero(glare_mask))
        if 0 < glare_cnt < int(0.30 * h * w):
            if HAS_CV2 and cv2 is not None:
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
                glare_mask = cv2.dilate(glare_mask, kernel, iterations=1)
                work = cv2.inpaint(work, glare_mask, 3, cv2.INPAINT_TELEA)
            else:
                med_val = int(np.median(work))
                work[glare_mask > 0] = med_val

    # 2. Flat-field illumination correction on reflective LCDs (broad illumination gradient removal)
    if not is_emissive and cfg["enable_flat_field"] and HAS_CV2 and cv2 is not None:
        sigma = max(28.0, max(h, w) / 3.0)
        bg_blur = cv2.GaussianBlur(work, (0, 0), sigmaX=sigma, sigmaY=sigma)
        bg_blur = np.maximum(bg_blur, 45)
        flat = cv2.divide(work, bg_blur, scale=225)
        # Light bilateral filter to remove sensor grain while keeping segment edges sharp
        work = cv2.bilateralFilter(flat, 5, 20, 20)

    return work


def _make_two_channel_tensor(pil_gray: Image.Image) -> "torch.Tensor":
    """
    Constructs a 2-channel tensor (2, 48, 32):
      - Channel 0: Normalized grayscale/green-channel digit patch
      - Channel 1: CLAHE local-contrast enhanced version of the digit patch
    """
    arr0 = np.array(pil_gray.convert("L").resize((DIGIT_IMG_W, DIGIT_IMG_H), Image.Resampling.BILINEAR), dtype=np.uint8)
    if HAS_CV2 and cv2 is not None:
        clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(4, 4))
        arr1 = clahe.apply(arr0)
    else:
        arr1 = np.array(ImageOps.autocontrast(Image.fromarray(arr0), cutoff=2), dtype=np.uint8)

    ch0 = (arr0.astype(np.float32) / 255.0 - 0.5) / 0.5
    ch1 = (arr1.astype(np.float32) / 255.0 - 0.5) / 0.5
    stacked = np.stack([ch0, ch1], axis=0)
    return torch.from_numpy(stacked)


# ==============================================================================
# 1. AUTO-LEARNED MULTIPLE DECIMAL PROFILES & DIGIT SEGMENTATION
# ==============================================================================

def _parse_label_decimal_places(raw_label_str: str) -> Tuple[str, int, bool]:
    """
    Extracts (clean_digits_str, decimal_places, is_negative) from a user label string.
    Examples:
      '14.6'   -> ('146', 1, False)
      '14.954' -> ('14954', 3, False)
      '25.42'  -> ('2542', 2, False)
      '28'     -> ('28', 0, False)
      '-5.2'   -> ('52', 1, True)
    """
    s = str(raw_label_str).strip().replace(",", ".")
    is_neg = s.startswith("-")
    m = re.search(r"(\d+)\.(\d+)", s)
    if m:
        int_part, dec_part = m.group(1), m.group(2)
        return (int_part + dec_part), len(dec_part), is_neg
    clean = re.sub(r"[^0-9]", "", s)
    return clean, 0, is_neg


def compute_learned_format_profiles(catalog: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Analyzes all user-labeled frames to automatically learn the number of digits and
    decimal places per ROI box (`by_box_id`, `by_box_label`) and per digit count (`by_digit_count`).
    """
    if catalog is None:
        cfg = get_train_config()
        cat_path = Path(cfg["train_data_dir"]) / "frames_catalog.json"
        if cat_path.is_file():
            try:
                catalog = json.loads(cat_path.read_text(encoding="utf-8"))
            except Exception:
                catalog = []
        else:
            catalog = []

    box_id_votes: Dict[str, List[Tuple[int, int]]] = {}
    box_label_votes: Dict[str, List[Tuple[int, int]]] = {}
    digit_count_dec_votes: Dict[str, List[int]] = {}

    for entry in catalog:
        raw_lbl = str(entry.get("label_reading", ""))
        clean_d, dec_places, _ = _parse_label_decimal_places(raw_lbl)
        if not clean_d:
            continue
        n_dig = len(clean_d)
        if entry.get("decimal_places") is not None:
            dec_places = int(entry["decimal_places"])

        b_id = str(entry.get("box_id", "")).strip()
        b_lbl = str(entry.get("box_label", "")).strip().lower()
        if b_id:
            box_id_votes.setdefault(b_id, []).append((n_dig, dec_places))
        if b_lbl:
            box_label_votes.setdefault(b_lbl, []).append((n_dig, dec_places))
        digit_count_dec_votes.setdefault(str(n_dig), []).append(dec_places)

    def _majority_pair(pairs: List[Tuple[int, int]]) -> Dict[str, int]:
        counts: Dict[Tuple[int, int], int] = {}
        for p in pairs:
            counts[p] = counts.get(p, 0) + 1
        best_p = max(counts.items(), key=lambda kv: kv[1])[0]
        return {"num_digits": best_p[0], "decimals": best_p[1]}

    def _majority_int(vals: List[int]) -> int:
        counts: Dict[int, int] = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0]

    by_box_id = {k: _majority_pair(v) for k, v in box_id_votes.items() if v}
    by_box_label = {k: _majority_pair(v) for k, v in box_label_votes.items() if v}
    by_digit_count = {k: _majority_int(v) for k, v in digit_count_dec_votes.items() if v}

    return {
        "by_box_id": by_box_id,
        "by_box_label": by_box_label,
        "by_digit_count": by_digit_count
    }


def _determine_expected_digits(
    box: Optional[Dict[str, Any]] = None,
    clean_digits_str: Optional[str] = None,
    learned_profiles: Optional[Dict[str, Any]] = None
) -> Tuple[int, int, bool]:
    """
    Returns (num_digits, fixed_decimals, is_standard) for a given ROI box or label string.
    Automatically uses learned decimal formats from training data when AUTO_LEARN_DECIMALS=true!
    """
    cfg = get_train_config()
    cam_type = (box.get("camera_type") or box.get("target") or "").lower() if box else ""
    lbl = str(box.get("label", "")).lower().strip() if box else ""
    box_id = str(box.get("id", "")).strip() if box else ""
    is_standard = (cam_type == "device" or "standard" in lbl or "fluke" in lbl or "1502" in lbl)

    # 1. Explicit box configuration if set on the ROI box object
    explicit_decimals = None
    if box and box.get("decimals") is not None:
        try:
            explicit_decimals = int(box["decimals"])
        except (ValueError, TypeError):
            explicit_decimals = None

    # 2. Check Auto-Learned Format Profiles from user's training data
    learned_num_digits = None
    learned_decimals = None
    if cfg.get("auto_learn_decimals", True):
        if learned_profiles is None:
            learned_profiles = compute_learned_format_profiles()
        by_id = learned_profiles.get("by_box_id", {})
        by_lbl = learned_profiles.get("by_box_label", {})
        by_cnt = learned_profiles.get("by_digit_count", {})

        if box_id and box_id in by_id:
            learned_num_digits = int(by_id[box_id]["num_digits"])
            learned_decimals = int(by_id[box_id]["decimals"])
        elif lbl and lbl in by_lbl:
            learned_num_digits = int(by_lbl[lbl]["num_digits"])
            learned_decimals = int(by_lbl[lbl]["decimals"])

        if clean_digits_str and str(len(clean_digits_str)) in by_cnt and learned_decimals is None:
            learned_decimals = int(by_cnt[str(len(clean_digits_str))])

    if clean_digits_str:
        num_digits = len(clean_digits_str)
    elif learned_num_digits is not None and learned_num_digits > 0:
        num_digits = learned_num_digits
    else:
        num_digits = 5 if is_standard else 3

    if explicit_decimals is not None:
        fixed_decimals = explicit_decimals
    elif learned_decimals is not None:
        fixed_decimals = learned_decimals
    else:
        # Check by_digit_count if available
        if cfg.get("auto_learn_decimals", True) and learned_profiles and str(num_digits) in learned_profiles.get("by_digit_count", {}):
            fixed_decimals = int(learned_profiles["by_digit_count"][str(num_digits)])
        else:
            fixed_decimals = cfg["standard_fixed_decimals"] if is_standard else cfg["chamber_fixed_decimals"]

    return num_digits, fixed_decimals, is_standard


def _normalize_digit_patch(gray_slot: np.ndarray, stroke_slot: np.ndarray) -> Image.Image:
    """
    Centers the active digit glyph within a standardized (32x48) patch while preserving aspect ratio,
    eliminating sensitivity to horizontal jitter in ROI box alignment.
    """
    sh, sw = gray_slot.shape[:2]
    if sh < 4 or sw < 3:
        return Image.fromarray(gray_slot).resize((DIGIT_IMG_W, DIGIT_IMG_H), Image.Resampling.BILINEAR)

    ys, xs = np.where(stroke_slot > 0)
    if len(xs) >= 8:
        x1 = max(0, int(np.percentile(xs, 2)) - 2)
        x2 = min(sw, int(np.percentile(xs, 98)) + 3)
        y1 = max(0, int(np.percentile(ys, 2)) - 2)
        y2 = min(sh, int(np.percentile(ys, 98)) + 3)
        if (x2 - x1) >= 3 and (y2 - y1) >= 6:
            gray_slot = gray_slot[y1:y2, x1:x2]

    gh, gw = gray_slot.shape[:2]
    bg_val = int(np.percentile(gray_slot, 85))
    canvas = Image.new("L", (DIGIT_IMG_W, DIGIT_IMG_H), color=bg_val)

    scale = min((DIGIT_IMG_W - 6) / float(max(1, gw)), (DIGIT_IMG_H - 6) / float(max(1, gh)))
    nw = max(4, min(DIGIT_IMG_W - 2, int(round(gw * scale))))
    nh = max(8, min(DIGIT_IMG_H - 2, int(round(gh * scale))))

    pil_glyph = Image.fromarray(gray_slot).resize((nw, nh), Image.Resampling.BILINEAR)
    pil_glyph = ImageOps.autocontrast(pil_glyph, cutoff=1)
    paste_x = (DIGIT_IMG_W - nw) // 2
    paste_y = (DIGIT_IMG_H - nh) // 2
    canvas.paste(pil_glyph, (paste_x, paste_y))
    return canvas


def segment_roi_into_digits(
    pil_crop: Image.Image,
    box: Optional[Dict[str, Any]] = None,
    expected_digits: Optional[int] = None,
    learned_profiles: Optional[Dict[str, Any]] = None
) -> Tuple[List[Image.Image], Optional[Image.Image]]:
    """
    Segments a full-resolution straightened ROI display crop into individual digit crops (48x32)
    plus an optional 'blank' background/unit patch.
    Applies glare inpainting and flat-field illumination filtering on reflective LCDs.
    """
    num_digits, _, is_standard = _determine_expected_digits(box, learned_profiles=learned_profiles)
    if expected_digits is not None and expected_digits > 0:
        num_digits = expected_digits

    rgb = np.array(pil_crop.convert("RGB"))
    h, w = rgb.shape[:2]
    if h < 10 or w < 15:
        return [], None

    # 1. If tall dual-row LCD (e.g. HTC-1 full screen containing both Temp and Clock/Humidity),
    # isolate the top temperature row
    if not is_standard and (float(w) / float(h) < 2.15) and h > 160:
        gray_full = np.mean(rgb, axis=2)
        sub_s = int(h * 0.45)
        sub_e = int(h * 0.65)
        if sub_e > sub_s:
            row_std = np.std(gray_full[sub_s:sub_e, :], axis=1)
            div_y = sub_s + int(np.argmin(row_std))
            if div_y >= 24:
                rgb = rgb[:div_y, :]
                h, w = rgb.shape[:2]

    # 2. Check if emissive VFD/LED (Green, Red, Blue, Amber)
    r_c = rgb[:, :, 0].astype(int)
    g_c = rgb[:, :, 1].astype(int)
    b_c = rgb[:, :, 2].astype(int)
    tot_px = float(h * w)
    min_pct = 0.004 if is_standard else 0.018

    emissive_masks = [
        ("green", (g_c > 85) & (g_c > r_c + 18) & (g_c > b_c + 18), g_c - np.maximum(r_c, b_c)),
        ("red",   (r_c > 95) & (r_c > g_c + 25) & (r_c > b_c + 25), r_c - np.maximum(g_c, b_c)),
        ("blue",  (b_c > 95) & (b_c > r_c + 25) & (b_c > g_c + 25), b_c - np.maximum(r_c, g_c)),
        ("amber", (np.minimum(r_c, g_c) > 95) & (np.minimum(r_c, g_c) > b_c + 25), np.minimum(r_c, g_c) - b_c),
    ]

    active_mask = None
    active_diff = None
    for _, em_m, em_d in emissive_masks:
        cnt = int(np.count_nonzero(em_m))
        if (min_pct <= cnt / tot_px <= 0.40) and cnt >= 25:
            ys, xs = np.where(em_m)
            if len(xs) >= 25 and (np.percentile(xs, 97) - np.percentile(xs, 3)) >= 0.25 * w:
                active_mask = em_m
                active_diff = np.clip(em_d, 0, 255).astype(np.uint8)
                break

    blank_patch: Optional[Image.Image] = None

    if active_mask is not None and active_diff is not None:
        # Emissive display (e.g. Fluke 1502A Green VFD):
        ys, xs = np.where(active_mask)
        x1 = max(0, int(np.percentile(xs, 1)) - 3)
        x2 = min(w, int(np.percentile(xs, 99)) + 3)
        y1 = max(0, int(np.percentile(ys, 2)) - 3)
        y2 = min(h, int(np.percentile(ys, 98)) + 3)

        inv_em = 255 - np.clip(active_diff * 2, 0, 255).astype(np.uint8)
        if x1 > 12:
            blank_patch = Image.fromarray(inv_em[y1:y2, :x1]).resize((DIGIT_IMG_W, DIGIT_IMG_H), Image.Resampling.BILINEAR)
        else:
            blank_patch = Image.new("L", (DIGIT_IMG_W, DIGIT_IMG_H), color=250)

        span_w = x2 - x1
        if is_standard and num_digits == 5 and span_w > 60:
            full_col = np.sum(active_mask[y1:y2, x1:x2], axis=0)
            s_gap = int(span_w * 0.64)
            e_gap = int(span_w * 0.86)
            sub_gap = full_col[s_gap:e_gap]
            zero_idxs = np.where(sub_gap <= 1)[0]
            digits_span_w = (s_gap + int(zero_idxs[0]) + 4) if len(zero_idxs) > 0 else int(span_w * 0.70)
        else:
            digits_span_w = span_w

        work_gray = inv_em[y1:y2, x1:x1 + max(15, digits_span_w)]
        work_stroke = active_mask[y1:y2, x1:x1 + max(15, digits_span_w)].astype(np.uint8)
        work_stroke_upper = work_stroke[:max(4, int(work_stroke.shape[0] * 0.72)), :]
    else:
        # Standard reflective 7-segment LCD (e.g. HTC-1)
        # Prefer Green channel (highest contrast on grey/green LCD glass) + Glare Inpaint + Flat-Field
        green_ch = rgb[:, :, 1].astype(np.uint8)
        filtered_gray = apply_glare_and_flat_field_filter(green_ch, is_emissive=False)

        if HAS_CV2 and cv2 is not None:
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(6, 6))
            gray_eq = clahe.apply(filtered_gray)
            sigma = max(5.0, min(h, w) / 7.0)
            bg = cv2.GaussianBlur(gray_eq, (0, 0), sigmaX=sigma, sigmaY=sigma)
            local_dark = cv2.subtract(bg, gray_eq)
        else:
            gray_eq = np.array(ImageOps.autocontrast(Image.fromarray(filtered_gray), cutoff=2))
            local_dark = np.clip(int(np.median(gray_eq)) - gray_eq.astype(int), 0, 255).astype(np.uint8)

        mx_l = max(4, int(w * 0.08))
        mx_r = max(4, int(w * 0.05))
        my_t = max(4, int(h * 0.10))
        my_b = max(4, int(h * 0.10))
        inner_dark = np.zeros_like(local_dark)
        inner_dark[my_t:h - my_b, mx_l:w - mx_r] = local_dark[my_t:h - my_b, mx_l:w - mx_r]

        pos_vals = inner_dark[inner_dark > 0]
        thr = max(12, int(np.percentile(pos_vals, 72))) if len(pos_vals) > 20 else 15
        stroke_mask = (inner_dark >= thr).astype(np.uint8)

        ys, xs = np.where(stroke_mask > 0)
        if len(xs) >= 30:
            x1 = max(0, int(np.percentile(xs, 2)) - 4)
            x2 = min(w, int(np.percentile(xs, 98)) + 4)
            y1 = max(0, int(np.percentile(ys, 3)) - 4)
            y2 = min(h, int(np.percentile(ys, 97)) + 4)
        else:
            x1, y1, x2, y2 = mx_l, my_t, w - mx_r, h - my_b

        if x1 > 12:
            blank_patch = Image.fromarray(gray_eq[y1:y2, :x1]).resize((DIGIT_IMG_W, DIGIT_IMG_H), Image.Resampling.BILINEAR)
        else:
            blank_patch = Image.fromarray(gray_eq[:max(8, int(h * 0.12)), :max(8, int(w * 0.15))]).resize((DIGIT_IMG_W, DIGIT_IMG_H), Image.Resampling.BILINEAR)

        span_w = x2 - x1
        if not is_standard and num_digits == 3 and span_w > 50:
            # Check lower-half stroke extent: on HTC-1 LCDs, the '°C' unit sits in the upper-right half,
            # whereas all 7-segment digits (0-9) have segments (c, d, e) in the lower half!
            y_mid = y1 + int((y2 - y1) * 0.55)
            lower_ys, lower_xs = np.where(stroke_mask[y_mid:y2, x1:x2] > 0)
            if len(lower_xs) >= 15:
                lower_span = int(np.percentile(lower_xs, 98)) + 4
                if int(span_w * 0.68) <= lower_span <= int(span_w * 0.88):
                    digits_span_w = lower_span
                else:
                    digits_span_w = span_w
            else:
                digits_span_w = int(span_w * 0.785)
        else:
            digits_span_w = span_w

        work_gray = gray_eq[y1:y2, x1:x1 + max(15, digits_span_w)]
        work_stroke = stroke_mask[y1:y2, x1:x1 + max(15, digits_span_w)]
        work_stroke_upper = work_stroke

    # 3. Slice `work_gray` into `num_digits` individual digit slots using stroke column valleys
    gh, gw = work_gray.shape[:2]
    col_stroke = np.sum(work_stroke_upper > 0, axis=0).astype(np.float32)
    if gw >= 7:
        col_stroke = np.convolve(col_stroke, np.ones(3, dtype=np.float32) / 3.0, mode="same")

    slot_w = gw / float(num_digits)
    split_points = [0]

    vfd_narrow_lead = False
    if active_mask is not None and num_digits == 5 and gw >= 50:
        v1_s, v1_e = int(gw * 0.05), int(gw * 0.16)
        if v1_e > v1_s and float(np.min(col_stroke[v1_s:v1_e])) <= 2.0:
            sp1 = v1_s + int(np.argmin(col_stroke[v1_s:v1_e]))
            split_points.append(sp1)
            rem_w = (gw - sp1) / float(num_digits - 1)
            for i in range(1, num_digits - 1):
                nominal = int(round(sp1 + i * rem_w))
                search_r = max(4, int(rem_w * 0.22))
                s_left = max(split_points[-1] + 8, nominal - search_r)
                s_right = min(gw - 8, nominal + search_r)
                valley_idx = (s_left + int(np.argmin(col_stroke[s_left:s_right]))) if s_right > s_left else nominal
                split_points.append(valley_idx)
            vfd_narrow_lead = True

    if not vfd_narrow_lead:
        for i in range(1, num_digits):
            if i == 1 and num_digits in (3, 5):
                nominal = int(round(0.84 * slot_w))
            else:
                nominal = int(round(i * slot_w))
            search_r = max(3, int(slot_w * 0.24))
            s_left = max(split_points[-1] + 5, nominal - search_r)
            s_right = min(gw - 5, nominal + search_r)
            if s_right > s_left:
                valley_idx = s_left + int(np.argmin(col_stroke[s_left:s_right]))
                split_points.append(valley_idx)
            else:
                split_points.append(nominal)
    split_points.append(gw)

    digit_crops: List[Image.Image] = []
    for i in range(num_digits):
        sx1 = max(0, split_points[i])
        sx2 = min(gw, split_points[i + 1])
        if sx2 <= sx1 + 2:
            sx1 = int(i * slot_w)
            sx2 = int((i + 1) * slot_w)
        patch = _normalize_digit_patch(work_gray[:, sx1:sx2], work_stroke[:, sx1:sx2])
        digit_crops.append(patch)

    return digit_crops, blank_patch


# ==============================================================================
# 2. SYNTHETIC 7-SEGMENT DIGIT GENERATOR (GHOST SEGMENTS + GLARE + NOISE)
# ==============================================================================

SEVEN_SEG_MAP = {
    "0": ("a", "b", "c", "d", "e", "f"),
    "1": ("b", "c"),
    "2": ("a", "b", "g", "e", "d"),
    "3": ("a", "b", "g", "c", "d"),
    "4": ("f", "g", "b", "c"),
    "5": ("a", "f", "g", "c", "d"),
    "6": ("a", "f", "e", "d", "c", "g"),
    "7": ("a", "b", "c"),
    "8": ("a", "b", "c", "d", "e", "f", "g"),
    "9": ("a", "b", "c", "d", "f", "g"),
    "blank": ()
}


def generate_synthetic_7seg_patch(cls_name: str, rng: Optional[random.Random] = None) -> Image.Image:
    """
    Renders a synthetic 7-segment digit (32x48) with:
    - Active lit segments + faint 'ghost' unlit segments (10-22% contrast)
    - Random background gradient, specular glare blob, slight slant, blur, and sensor noise.
    Used to supplement rare digits (0, 3, 7, 8) and teach the CNN to ignore ghost segments.
    """
    if rng is None:
        rng = random
    w, h = DIGIT_IMG_W, DIGIT_IMG_H
    bg_base = rng.randint(195, 248)
    # Create subtle gradient background
    y_grad = np.linspace(-rng.randint(-12, 12), rng.randint(-12, 12), h).reshape(h, 1)
    x_grad = np.linspace(-rng.randint(-8, 8), rng.randint(-8, 8), w).reshape(1, w)
    bg_arr = np.clip(bg_base + y_grad + x_grad, 140, 254).astype(np.uint8)
    img = Image.fromarray(bg_arr, mode="L")
    draw = ImageDraw.Draw(img)

    lit_segs = set(SEVEN_SEG_MAP.get(cls_name, ()))
    all_segs = ("a", "b", "c", "d", "e", "f", "g")

    # Geometry inside 32x48 patch (matches _normalize_digit_patch 3px border margin)
    pad_x = rng.randint(3, 5)
    pad_y = rng.randint(3, 5)
    thick = rng.randint(3, 5)
    x_l = pad_x
    x_r = w - pad_x - 1
    y_t = pad_y
    y_m = h // 2
    y_b = h - pad_y - 1

    # For digit '1', center the vertical right-hand bars so it matches normalized patches
    if cls_name == "1":
        cx = w // 2 + rng.randint(-2, 2)
        seg_coords = {
            "b": (cx, y_t + 1, cx, y_m - 1),
            "c": (cx, y_m + 1, cx, y_b - 1),
            "a": (cx - 6, y_t, cx, y_t),
            "d": (cx - 6, y_b, cx, y_b),
            "e": (cx - 6, y_m + 1, cx - 6, y_b - 1),
            "f": (cx - 6, y_t + 1, cx - 6, y_m - 1),
            "g": (cx - 6, y_m, cx, y_m),
        }
    else:
        seg_coords = {
            "a": (x_l + 2, y_t, x_r - 2, y_t),
            "b": (x_r, y_t + 2, x_r, y_m - 1),
            "c": (x_r, y_m + 1, x_r, y_b - 2),
            "d": (x_l + 2, y_b, x_r - 2, y_b),
            "e": (x_l, y_m + 1, x_l, y_b - 2),
            "f": (x_l, y_t + 2, x_l, y_m - 1),
            "g": (x_l + 2, y_m, x_r - 2, y_m),
        }

    ghost_val = max(110, bg_base - rng.randint(14, 34))
    lit_val = rng.randint(12, 65)

    # Draw faint unlit ghost segments first
    for s_name in all_segs:
        if s_name not in lit_segs and rng.random() < 0.75:
            c = seg_coords[s_name]
            draw.line(c, fill=ghost_val, width=max(2, thick - 1))

    # Draw active lit segments
    for s_name in lit_segs:
        c = seg_coords[s_name]
        draw.line(c, fill=lit_val, width=thick)

    # Occasional specular glare blob
    if rng.random() < 0.30:
        gx = rng.randint(4, w - 6)
        gy = rng.randint(4, h - 6)
        gr = rng.randint(2, 5)
        draw.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), fill=min(255, bg_base + 30))

    # Slight blur and sensor noise
    if rng.random() < 0.65:
        img = img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.3, 0.85)))
    arr = np.array(img, dtype=np.float32)
    noise = np.random.normal(0.0, rng.uniform(1.5, 4.5), size=arr.shape)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    if cls_name != "blank":
        stroke_m = (arr < (bg_base - 40)).astype(np.uint8)
        return _normalize_digit_patch(arr, stroke_m)
    return Image.fromarray(arr, mode="L")


# ==============================================================================
# 3. PYTORCH 2-CHANNEL 7-SEGMENT CNN & HYBRID REAL+SYNTHETIC DATASET
# ==============================================================================

if HAS_TORCH:
    class SevenSegmentCNN(nn.Module):
        """
        Compact, high-accuracy CPU CNN for 7-segment LCD & VFD digit classification.
        Supports 2-channel input: (B, 2, 48, 32) [Flat-Field/Green + CLAHE].
        Output: (B, num_classes) logits mapped strictly to `full.classes`.
        """
        def __init__(self, num_classes: int = 11, in_channels: int = 2):
            super().__init__()
            self.in_channels = in_channels
            self.features = nn.Sequential(
                nn.Conv2d(in_channels, 24, kernel_size=3, padding=1),
                nn.BatchNorm2d(24),
                nn.ReLU(inplace=True),
                nn.Conv2d(24, 24, kernel_size=3, padding=1),
                nn.BatchNorm2d(24),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2, 2),  # 24x16

                nn.Conv2d(24, 48, kernel_size=3, padding=1),
                nn.BatchNorm2d(48),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2, 2),  # 12x8

                nn.Conv2d(48, 64, kernel_size=3, padding=1),
                nn.BatchNorm2d(64),
                nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool2d((4, 3))  # 64x4x3 = 768
            )
            self.classifier = nn.Sequential(
                nn.Flatten(),
                nn.Dropout(0.20),
                nn.Linear(64 * 4 * 3, 128),
                nn.ReLU(inplace=True),
                nn.Dropout(0.10),
                nn.Linear(128, num_classes)
            )

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            if x.dim() == 4 and x.size(1) == 1 and self.in_channels == 2:
                x = x.repeat(1, 2, 1, 1)
            elif x.dim() == 4 and x.size(1) == 2 and self.in_channels == 1:
                x = x[:, :1, :, :]
            x = self.features(x)
            return self.classifier(x)

    class BalancedAugmentedDataset(Dataset):
        """
        Balances all training classes to at least `target_per_class` samples per epoch,
        mixes synthetic 7-segment digits for rare classes (up to `synthetic_ratio`),
        applies stochastic augmentations (brightness/contrast jitter, panning shifts,
        shear, and perspective warp), and produces 2-channel (Gray + CLAHE) tensors.
        """
        def __init__(
            self,
            base_dataset: Any,
            indices: List[int],
            pil_transform: Any,
            classes_list: List[str],
            target_per_class: int = 36,
            use_synthetic: bool = True,
            synthetic_ratio: float = 0.50
        ):
            self.base_dataset = base_dataset
            self.pil_transform = pil_transform
            self.classes_list = classes_list
            self.use_synthetic = use_synthetic
            self.synthetic_ratio = synthetic_ratio

            by_class: Dict[int, List[int]] = {}
            for idx in indices:
                _, cls_idx = base_dataset.samples[idx]
                by_class.setdefault(cls_idx, []).append(idx)

            max_count = max([len(v) for v in by_class.values()] + [target_per_class])
            per_cls = max(target_per_class, min(64, max_count))

            # Each item is (real_sample_idx_or_None, cls_idx, cls_name)
            self.items: List[Tuple[Optional[int], int, str]] = []
            rng = random.Random(123)
            for cls_idx, cls_name in enumerate(classes_list):
                cls_idxs = by_class.get(cls_idx, [])
                n_real = len(cls_idxs)
                for i in range(per_cls):
                    # If class has few real samples or synthetic mix coin flip triggers, use synthetic sample
                    use_syn_here = use_synthetic and (
                        n_real == 0 or (n_real < 10 and (i % 2 == 1)) or (rng.random() < synthetic_ratio * 0.45)
                    )
                    if use_syn_here or n_real == 0:
                        self.items.append((None, cls_idx, cls_name))
                    else:
                        self.items.append((cls_idxs[i % n_real], cls_idx, cls_name))
            rng.shuffle(self.items)

        def __len__(self) -> int:
            return len(self.items)

        def __getitem__(self, idx: int) -> Tuple[Any, int, str]:
            real_idx, target, cls_name = self.items[idx]
            if real_idx is None:
                gray = generate_synthetic_7seg_patch(cls_name)
                img_path = f"synthetic_{cls_name}_{idx}.png"
            else:
                img_path, target = self.base_dataset.samples[real_idx]
                with Image.open(img_path) as img:
                    gray = img.convert("L")
            if self.pil_transform is not None:
                gray = self.pil_transform(gray)
            tensor = _make_two_channel_tensor(gray)
            return tensor, target, img_path

    class EvalSubset(Dataset):
        """Validation dataset — strictly evaluates on 100% REAL held-out frames only."""
        def __init__(self, base_dataset: Any, indices: List[int]):
            self.base_dataset = base_dataset
            self.indices = indices

        def __len__(self) -> int:
            return len(self.indices)

        def __getitem__(self, idx: int) -> Tuple[Any, int, str]:
            real_idx = self.indices[idx]
            img_path, target = self.base_dataset.samples[real_idx]
            with Image.open(img_path) as img:
                gray = img.convert("L")
            tensor = _make_two_channel_tensor(gray)
            return tensor, target, img_path


# ==============================================================================
# 4. DATA COLLECTION, BATCH LABELING & HARD-EXAMPLE MINING PIPELINE
# ==============================================================================

def save_labeled_roi_crop(
    pil_crop: Image.Image,
    label_value_str: str,
    box: Optional[Dict[str, Any]] = None,
    project_id: str = "manual",
    snapshot_name: str = ""
) -> Dict[str, Any]:
    """
    Takes a cropped ROI image and the user-provided reading (e.g. '14.6', '25.42', '14.954'),
    learns its decimal position, segments it into individual digits, saves the full frame
    + individual digit crops into `<TRAIN_DATA_DIR>/digits/<class_name>/<frame_id>_d<pos>.png`,
    and returns base64 previews + image quality metrics.
    """
    cfg = get_train_config()
    train_dir = Path(cfg["train_data_dir"])
    digits_dir = train_dir / "digits"
    frames_dir = train_dir / "frames"

    for c in DEFAULT_CLASSES:
        (digits_dir / c).mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    raw_str = str(label_value_str).strip()
    clean_digits, decimal_places, is_neg = _parse_label_decimal_places(raw_str)
    if not clean_digits:
        raise ValueError(f"Label '{raw_str}' does not contain any digits (0-9).")

    quality = assess_crop_quality(pil_crop)
    frame_id = f"frm_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    digit_crops, blank_patch = segment_roi_into_digits(pil_crop, box=box, expected_digits=len(clean_digits))
    if len(digit_crops) != len(clean_digits):
        raise ValueError(f"Could not segment crop into {len(clean_digits)} digits (got {len(digit_crops)}).")

    frame_img_path = frames_dir / f"{frame_id}.jpg"
    pil_crop.convert("RGB").save(frame_img_path, format="JPEG", quality=95)

    saved_digits_info = []
    for idx, (d_img, d_char) in enumerate(zip(digit_crops, clean_digits)):
        cls_folder = digits_dir / d_char
        cls_folder.mkdir(parents=True, exist_ok=True)
        out_path = cls_folder / f"{frame_id}_d{idx}.png"
        d_img.save(out_path, format="PNG")

        buf = io.BytesIO()
        d_img.save(buf, format="PNG")
        b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        saved_digits_info.append({
            "position": idx,
            "digit": d_char,
            "path": str(out_path.name),
            "crop_base64": b64
        })

    if blank_patch is not None:
        blank_path = digits_dir / "blank" / f"{frame_id}_blank.png"
        blank_patch.save(blank_path, format="PNG")

    catalog_path = train_dir / "frames_catalog.json"

    # Catalog keeps lightweight metadata only (images are served from frames/<id>.jpg)
    entry = {
        "frame_id": frame_id,
        "timestamp": time.time(),
        "project_id": project_id,
        "snapshot_name": snapshot_name,
        "box_id": box.get("id", "") if box else "",
        "box_label": box.get("label", "Display") if box else "Display",
        "camera_type": (box.get("camera_type") or box.get("target") or "chamber") if box else "chamber",
        "label_reading": raw_str,
        "num_digits": len(clean_digits),
        "decimal_places": decimal_places,
        "is_negative": is_neg,
        "digits": list(clean_digits),
        "quality": quality,
    }
    with _CATALOG_LOCK:
        catalog = _load_catalog(catalog_path)
        catalog.insert(0, entry)
        catalog = catalog[:int(os.environ.get("TRAIN_CATALOG_MAX", "5000"))]
        _write_catalog(catalog_path, catalog)

    return {
        "status": "ok",
        "frame_id": frame_id,
        "label_reading": raw_str,
        "decimal_places": decimal_places,
        "digits_saved": len(saved_digits_info),
        "digit_previews": saved_digits_info,
        "quality": quality,
        "dataset_stats": get_training_dataset_stats()
    }


def log_hard_example(
    pil_crop: Image.Image,
    box: Optional[Dict[str, Any]],
    pred_text: str,
    pred_conf: float,
    reason: str = "low_confidence"
) -> None:
    """
    Logs predictions with confidence < HARD_EXAMPLE_CONF_THRESHOLD (0.90) or reader disagreement
    into `hard_examples.json` so the user can review & label them in 1 click.
    """
    try:
        cfg = get_train_config()
        train_dir = Path(cfg["train_data_dir"])
        train_dir.mkdir(parents=True, exist_ok=True)
        hard_path = train_dir / "hard_examples.json"

        items = []
        if hard_path.is_file():
            try:
                items = json.loads(hard_path.read_text(encoding="utf-8"))
            except Exception:
                items = []

        buf = io.BytesIO()
        thumb = pil_crop.convert("RGB")
        thumb.thumbnail((320, 140), Image.Resampling.BILINEAR)
        thumb.save(buf, format="JPEG", quality=88)
        b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        box_lbl = box.get("label", "Display") if box else "Display"
        # Avoid spamming identical consecutive predictions for the same box within 15 seconds
        now = time.time()
        for existing in items[:6]:
            if existing.get("box_label") == box_lbl and existing.get("pred_text") == pred_text and (now - existing.get("timestamp", 0)) < 15.0:
                return

        entry = {
            "hard_id": f"hard_{int(now * 1000)}_{uuid.uuid4().hex[:5]}",
            "timestamp": now,
            "box": box or {},
            "box_label": box_lbl,
            "pred_text": pred_text,
            "confidence": round(float(pred_conf), 3),
            "reason": reason,
            "crop_base64": b64
        }
        items.insert(0, entry)
        items = items[:50]
        hard_path.write_text(json.dumps(items, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("[Hard Example Logger] Notice: %s", e)


def delete_hard_example(hard_id: str) -> Dict[str, Any]:
    cfg = get_train_config()
    hard_path = Path(cfg["train_data_dir"]) / "hard_examples.json"
    if hard_path.is_file():
        try:
            items = json.loads(hard_path.read_text(encoding="utf-8"))
            items = [x for x in items if x.get("hard_id") != hard_id]
            hard_path.write_text(json.dumps(items, indent=2), encoding="utf-8")
        except Exception:
            pass
    return {"status": "ok"}


def delete_labeled_frame(frame_id: str) -> Dict[str, Any]:
    """Remove a labeled frame and all its derived digit crops if the user spots a bad ROI alignment."""
    if not re.fullmatch(r"frm_[A-Za-z0-9_]+", str(frame_id or "")):
        raise ValueError("Invalid frame id.")
    cfg = get_train_config()
    train_dir = Path(cfg["train_data_dir"])
    digits_dir = train_dir / "digits"
    frames_dir = train_dir / "frames"

    removed_files = 0
    if digits_dir.is_dir():
        for p in digits_dir.rglob(f"{frame_id}_*.png"):
            try:
                p.unlink()
                removed_files += 1
            except Exception:
                pass
    frame_jpg = frames_dir / f"{frame_id}.jpg"
    if frame_jpg.is_file():
        try:
            frame_jpg.unlink()
        except Exception:
            pass

    catalog_path = train_dir / "frames_catalog.json"
    with _CATALOG_LOCK:
        if catalog_path.is_file():
            catalog = _load_catalog(catalog_path)
            catalog = [c for c in catalog if c.get("frame_id") != frame_id]
            _write_catalog(catalog_path, catalog)

    return {
        "status": "ok",
        "removed_files": removed_files,
        "dataset_stats": get_training_dataset_stats()
    }


def delete_labeled_frames_bulk(frame_ids: List[str]) -> Dict[str, Any]:
    """Delete many labeled frames at once (bad ROI alignment cleanup)."""
    removed = 0
    for fid in frame_ids:
        fid = str(fid).strip()
        if not re.fullmatch(r"frm_[A-Za-z0-9_]+", fid):
            continue
        r = _delete_frame_files(fid)
        removed += 1 if r else 0
    cfg = get_train_config()
    catalog_path = Path(cfg["train_data_dir"]) / "frames_catalog.json"
    idset = set(str(x) for x in frame_ids)
    with _CATALOG_LOCK:
        if catalog_path.is_file():
            catalog = [c for c in _load_catalog(catalog_path) if c.get("frame_id") not in idset]
            _write_catalog(catalog_path, catalog)
    return {"status": "ok", "deleted": removed, "dataset_stats": get_training_dataset_stats()}


def _delete_frame_files(frame_id: str) -> bool:
    cfg = get_train_config()
    train_dir = Path(cfg["train_data_dir"])
    ok = False
    digits_dir = train_dir / "digits"
    if digits_dir.is_dir():
        for p in digits_dir.rglob(f"{frame_id}_*.png"):
            try:
                p.unlink()
                ok = True
            except Exception:
                pass
    frame_jpg = train_dir / "frames" / f"{frame_id}.jpg"
    if frame_jpg.is_file():
        try:
            frame_jpg.unlink()
            ok = True
        except Exception:
            pass
    return ok


def list_labeled_frames(offset: int = 0, limit: int = 60, query: str = "", box_label: str = "",
                        project_id: str = "") -> Dict[str, Any]:
    """Browse the full labeled-frame catalog (lightweight rows; images via /api/ocr/train/frame_image/<id>)."""
    cfg = get_train_config()
    train_dir = Path(cfg["train_data_dir"])
    catalog = _load_catalog(train_dir / "frames_catalog.json")
    q = (query or "").strip().lower()

    box_labels = sorted({str(c.get("box_label", "")) for c in catalog if c.get("box_label")})
    projects = sorted({str(c.get("project_id", "")) for c in catalog if c.get("project_id")})

    def match(c: Dict[str, Any]) -> bool:
        if box_label and c.get("box_label") != box_label:
            return False
        if project_id and c.get("project_id") != project_id:
            return False
        if q:
            hay = f"{c.get('label_reading','')} {c.get('snapshot_name','')} {c.get('box_label','')} {c.get('project_id','')}".lower()
            return q in hay
        return True

    rows = [c for c in catalog if match(c)]
    total = len(rows)
    page = rows[max(0, offset): max(0, offset) + max(1, min(limit, 300))]
    items = []
    for c in page:
        items.append({
            "frame_id": c.get("frame_id"),
            "timestamp": c.get("timestamp"),
            "project_id": c.get("project_id"),
            "snapshot_name": c.get("snapshot_name", ""),
            "box_label": c.get("box_label"),
            "camera_type": c.get("camera_type"),
            "label_reading": c.get("label_reading"),
            "num_digits": c.get("num_digits"),
            "decimal_places": c.get("decimal_places"),
            "quality": c.get("quality"),
            "crop_url": f"/api/ocr/train/frame_image/{c.get('frame_id')}",
        })
    return {"status": "ok", "total": total, "offset": offset, "items": items,
            "box_labels": box_labels, "projects": projects}


def get_labeled_frame_path(frame_id: str) -> Optional[Path]:
    if not re.fullmatch(r"frm_[A-Za-z0-9_]+", frame_id or ""):
        return None
    p = Path(get_train_config()["train_data_dir"]) / "frames" / f"{frame_id}.jpg"
    return p if p.is_file() else None


def update_labeled_frame(frame_id: str, new_label: str) -> Dict[str, Any]:
    """
    Edit the ground-truth reading of an already-labeled frame: re-segments the stored crop with the
    new label, replaces its digit samples, and updates the catalog (decimal profile is re-learned).
    """
    frame_path = get_labeled_frame_path(frame_id)
    if frame_path is None:
        raise ValueError("Labeled frame image not found.")
    raw_str = str(new_label).strip()
    clean_digits, decimal_places, is_neg = _parse_label_decimal_places(raw_str)
    if not clean_digits:
        raise ValueError(f"Label '{raw_str}' does not contain any digits (0-9).")

    cfg = get_train_config()
    train_dir = Path(cfg["train_data_dir"])
    digits_dir = train_dir / "digits"
    catalog_path = train_dir / "frames_catalog.json"

    with _CATALOG_LOCK:
        catalog = _load_catalog(catalog_path)
        entry = next((c for c in catalog if c.get("frame_id") == frame_id), None)
        box_ctx = {
            "id": (entry or {}).get("box_id", ""),
            "label": (entry or {}).get("box_label", "Display"),
            "camera_type": (entry or {}).get("camera_type", "chamber"),
        }
        pil_crop = Image.open(frame_path).convert("RGB")
        digit_crops, blank_patch = segment_roi_into_digits(pil_crop, box=box_ctx, expected_digits=len(clean_digits))
        if len(digit_crops) != len(clean_digits):
            raise ValueError(f"Could not segment crop into {len(clean_digits)} digits (got {len(digit_crops)}).")

        # Replace old digit samples only after the new segmentation succeeded
        for p in digits_dir.rglob(f"{frame_id}_*.png"):
            try:
                p.unlink()
            except Exception:
                pass
        for idx, (d_img, d_char) in enumerate(zip(digit_crops, clean_digits)):
            cls_folder = digits_dir / d_char
            cls_folder.mkdir(parents=True, exist_ok=True)
            d_img.save(cls_folder / f"{frame_id}_d{idx}.png", format="PNG")
        if blank_patch is not None:
            (digits_dir / "blank").mkdir(parents=True, exist_ok=True)
            blank_patch.save(digits_dir / "blank" / f"{frame_id}_blank.png", format="PNG")

        if entry is not None:
            entry.update({
                "label_reading": raw_str,
                "num_digits": len(clean_digits),
                "decimal_places": decimal_places,
                "is_negative": is_neg,
                "digits": list(clean_digits),
                "edited_at": time.time(),
            })
            _write_catalog(catalog_path, catalog)

    return {"status": "ok", "frame_id": frame_id, "label_reading": raw_str,
            "decimal_places": decimal_places, "digits_saved": len(clean_digits),
            "dataset_stats": get_training_dataset_stats()}


def get_training_dataset_stats() -> Dict[str, Any]:
    """Return per-class sample counts, learned decimal profiles, hard examples, and model status."""
    cfg = get_train_config()
    train_dir = Path(cfg["train_data_dir"])
    digits_dir = train_dir / "digits"
    model_path = Path(cfg["custom_model_path"])
    meta_path = model_path.with_suffix(".json")

    class_counts: Dict[str, int] = {c: 0 for c in DEFAULT_CLASSES}
    total_samples = 0
    frame_ids = set()

    if digits_dir.is_dir():
        for cls_dir in sorted(digits_dir.iterdir()):
            if cls_dir.is_dir():
                files = list(cls_dir.glob("*.png")) + list(cls_dir.glob("*.jpg"))
                class_counts[cls_dir.name] = len(files)
                total_samples += len(files)
                for f in files:
                    fid = f.stem.rsplit("_", 1)[0]
                    frame_ids.add(fid)

    catalog = []
    catalog_path = train_dir / "frames_catalog.json"
    if catalog_path.is_file():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except Exception:
            catalog = []

    hard_examples = []
    hard_path = train_dir / "hard_examples.json"
    if hard_path.is_file():
        try:
            hard_examples = json.loads(hard_path.read_text(encoding="utf-8"))
        except Exception:
            hard_examples = []

    learned_profiles = compute_learned_format_profiles(catalog)

    model_info = None
    if model_path.is_file() and meta_path.is_file():
        try:
            model_info = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "total_samples": total_samples,
        "total_frames": len(frame_ids),
        "class_counts": class_counts,
        "learned_profiles": learned_profiles,
        "recent_frames": [
            {**{k: v for k, v in c.items() if k not in ("crop_base64", "digit_previews")},
             "crop_url": f"/api/ocr/train/frame_image/{c.get('frame_id')}"}
            for c in catalog[:30]
        ],
        "hard_examples": hard_examples[:20],
        "model_trained": model_path.is_file(),
        "model_info": model_info,
        "config": cfg
    }


# ==============================================================================
# 5. CPU TRAINING LOOP (FRAME HOLDOUT + SYNTHETIC MIX + CONFUSION MATRIX + READING ACC)
# ==============================================================================

def _extract_frame_id(filepath: str) -> str:
    stem = Path(filepath).stem
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def train_custom_model(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Trains the custom 2-Channel PyTorch 7-Segment CNN on CPU using:
    - Whole-frame holdout validation split (validates strictly on 100% real crops).
    - 50/50 Synthetic + Real 7-segment data mixing during training to cover rare digits & ghost segments.
    - Brightness/contrast jitter, panning shifts, shear, and slight perspective warping.
    - Class-weighted CrossEntropyLoss with Label Smoothing (0.08) + AdamW weight decay + Early Stopping.
    - Tracks BOTH Per-Digit Validation Accuracy AND Whole-Reading Validation Accuracy (all digits right).
    - Computes 7-segment Confusion Matrix (highlighting 1<->7, 3<->9, 5<->6, 0<->8).
    - Persists `full.classes` and `learned_profiles` (multi-decimal formats per device).
    """
    if not HAS_TORCH:
        raise RuntimeError("PyTorch CPU build is not installed. Run `pip install torch torchvision`.")

    cfg = get_train_config()
    if overrides:
        for k, v in overrides.items():
            if v is not None and k in cfg:
                cfg[k] = v

    train_dir = Path(cfg["train_data_dir"])
    digits_dir = train_dir / "digits"
    model_path = Path(cfg["custom_model_path"])
    meta_path = model_path.with_suffix(".json")

    if not digits_dir.is_dir():
        raise ValueError("No training data directory found. Please label some ROI crops first.")

    non_empty_classes = []
    for d in sorted(digits_dir.iterdir()):
        if d.is_dir():
            imgs = list(d.glob("*.png")) + list(d.glob("*.jpg"))
            if imgs:
                non_empty_classes.append(d.name)
            else:
                try:
                    d.rmdir()
                except Exception:
                    pass

    if len(non_empty_classes) < 2:
        raise ValueError(
            f"Need labeled samples for at least 2 classes to train (currently have {non_empty_classes}). "
            "Select an ROI box and label a few snapshots first!"
        )

    train_threads = int(cfg["train_cpu_threads"])
    torch.set_num_threads(train_threads)

    full = datasets.ImageFolder(str(digits_dir))
    classes_list = list(full.classes)
    logger.info("[PyTorch Trainer] Training on CPU (%d threads) | full.classes = %s", train_threads, classes_list)
    print(f"[PyTorch Trainer] full.classes = {classes_list}")

    if len(full.samples) < 6:
        raise ValueError(f"Only {len(full.samples)} digit samples found. Please label at least 2-3 ROI readings before training.")

    val_split = float(cfg["train_val_split"])
    hold_out_frames = bool(cfg["train_hold_out_frames"])

    frame_to_indices: Dict[str, List[int]] = {}
    class_to_indices: Dict[int, List[int]] = {}
    for idx, (img_path, cls_idx) in enumerate(full.samples):
        fid = _extract_frame_id(img_path)
        frame_to_indices.setdefault(fid, []).append(idx)
        class_to_indices.setdefault(cls_idx, []).append(idx)

    unique_frames = list(frame_to_indices.keys())
    rng = random.Random(42)
    rng.shuffle(unique_frames)

    train_indices: List[int] = []
    val_indices: List[int] = []

    if hold_out_frames and len(unique_frames) >= 4:
        n_val_target = max(1, int(round(len(unique_frames) * val_split)))
        val_frame_set = set()
        class_remaining = {c: len(idxs) for c, idxs in class_to_indices.items()}

        for fid in unique_frames:
            if len(val_frame_set) >= n_val_target:
                break
            f_idxs = frame_to_indices[fid]
            can_hold_out = True
            for i in f_idxs:
                c_idx = full.samples[i][1]
                if class_remaining.get(c_idx, 0) <= 2:
                    can_hold_out = False
                    break
            if can_hold_out:
                val_frame_set.add(fid)
                for i in f_idxs:
                    c_idx = full.samples[i][1]
                    class_remaining[c_idx] -= 1

        for fid, idxs in frame_to_indices.items():
            if fid in val_frame_set:
                val_indices.extend(idxs)
            else:
                train_indices.extend(idxs)
    else:
        all_idxs = list(range(len(full.samples)))
        rng.shuffle(all_idxs)
        n_val = max(1, int(round(len(all_idxs) * val_split)))
        val_indices = all_idxs[:n_val]
        train_indices = all_idxs[n_val:]

    if not val_indices:
        val_indices = train_indices[:max(1, len(train_indices) // 5)]

    # PIL-stage augmentation: Brightness/Contrast jitter + Perspective Warping + Affine Panning & Shear
    warp_scale = float(cfg.get("augment_warp", 0.08))
    pil_train_transform = transforms.Compose([
        transforms.Resize((DIGIT_IMG_H, DIGIT_IMG_W)),
        transforms.ColorJitter(
            brightness=float(cfg["augment_brightness"]),
            contrast=float(cfg["augment_contrast"])
        ),
        transforms.RandomPerspective(distortion_scale=warp_scale, p=0.35, fill=235),
        transforms.RandomAffine(
            degrees=2.5,
            translate=(float(cfg["augment_shift"]), float(cfg["augment_shift"])),
            scale=(0.94, 1.06),
            shear=(-float(cfg["augment_shear"]), float(cfg["augment_shear"])),
            fill=235
        ),
    ])

    train_ds = BalancedAugmentedDataset(
        full,
        train_indices,
        pil_train_transform,
        classes_list=classes_list,
        target_per_class=36,
        use_synthetic=bool(cfg.get("use_synthetic", True)),
        synthetic_ratio=float(cfg.get("synthetic_mix_ratio", 0.50))
    )
    val_ds = EvalSubset(full, val_indices)

    batch_size = min(int(cfg["train_batch_size"]), max(4, len(train_ds)))
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False)

    device = torch.device("cpu")
    in_channels = 2 if cfg.get("two_channel_clahe", True) else 1
    model = SevenSegmentCNN(num_classes=len(classes_list), in_channels=in_channels).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(cfg["train_lr"]), weight_decay=1e-4)
    epochs = int(cfg["train_epochs"])
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    # Light regularization: Label smoothing (0.08)
    lbl_smooth = float(cfg.get("label_smoothing", 0.08))
    criterion = nn.CrossEntropyLoss(label_smoothing=lbl_smooth)

    start_t = time.time()
    best_score = -1.0
    best_val_acc = 0.0
    best_reading_acc = 0.0
    best_state = None
    patience = int(cfg.get("early_stop_patience", 12))
    epochs_no_improve = 0
    history = []

    for epoch in range(1, epochs + 1):
        model.train()
        train_loss = 0.0
        train_correct = 0
        train_total = 0

        for x, y, _ in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            logits = model(x)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()

            train_loss += float(loss.item()) * x.size(0)
            preds = logits.argmax(dim=1)
            train_correct += int((preds == y).sum().item())
            train_total += x.size(0)

        scheduler.step()

        model.eval()
        val_loss = 0.0
        val_correct = 0
        val_total = 0
        frame_correct_map: Dict[str, bool] = {}

        with torch.no_grad():
            for x, y, paths in val_loader:
                x, y = x.to(device), y.to(device)
                logits = model(x)
                loss = criterion(logits, y)
                val_loss += float(loss.item()) * x.size(0)
                preds = logits.argmax(dim=1)
                val_correct += int((preds == y).sum().item())
                val_total += x.size(0)

                for i in range(x.size(0)):
                    p_str = paths[i]
                    if p_str.endswith("_blank.png"):
                        continue
                    fid = _extract_frame_id(p_str)
                    is_ok = (int(preds[i].item()) == int(y[i].item()))
                    frame_correct_map[fid] = frame_correct_map.get(fid, True) and is_ok

        t_acc = (train_correct / max(1, train_total)) * 100.0
        v_acc = (val_correct / max(1, val_total)) * 100.0
        r_acc = (sum(1 for ok in frame_correct_map.values() if ok) / max(1, len(frame_correct_map))) * 100.0
        combined_score = 0.6 * v_acc + 0.4 * r_acc

        if combined_score >= best_score:
            best_score = combined_score
            best_val_acc = v_acc
            best_reading_acc = r_acc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            epochs_no_improve = 0
        else:
            epochs_no_improve += 1

        if epoch == 1 or epoch % 5 == 0 or epoch == epochs:
            history.append({
                "epoch": epoch,
                "train_loss": round(train_loss / max(1, train_total), 4),
                "train_acc": round(t_acc, 2),
                "val_loss": round(val_loss / max(1, val_total), 4),
                "val_acc": round(v_acc, 2),
                "val_reading_acc": round(r_acc, 2)
            })

        if epoch >= 18 and best_val_acc >= 99.9 and epochs_no_improve >= patience:
            break

    if best_state is not None:
        model.load_state_dict(best_state)

    # Collect misclassified validation crops & build 7-Segment Confusion Matrix
    model.eval()
    misclassified = []
    conf_matrix: Dict[str, Dict[str, int]] = {c: {c2: 0 for c2 in classes_list} for c in classes_list}
    pair_confusions: Dict[str, int] = {"1<->7": 0, "3<->9": 0, "5<->6": 0, "0<->8": 0}

    with torch.no_grad():
        for x, y, paths in val_loader:
            probs = F.softmax(model(x), dim=1)
            confs, preds = probs.max(dim=1)
            for i in range(x.size(0)):
                true_idx = int(y[i].item())
                pred_idx = int(preds[i].item())
                conf_val = float(confs[i].item())
                img_p = paths[i]
                t_cls = classes_list[true_idx]
                p_cls = classes_list[pred_idx]
                conf_matrix[t_cls][p_cls] = conf_matrix[t_cls].get(p_cls, 0) + 1

                if t_cls != p_cls:
                    pair_set = {t_cls, p_cls}
                    if pair_set == {"1", "7"}:
                        pair_confusions["1<->7"] += 1
                    elif pair_set == {"3", "9"}:
                        pair_confusions["3<->9"] += 1
                    elif pair_set == {"5", "6"}:
                        pair_confusions["5<->6"] += 1
                    elif pair_set == {"0", "8"}:
                        pair_confusions["0<->8"] += 1

                if pred_idx != true_idx or conf_val < 0.78:
                    try:
                        with open(img_p, "rb") as f:
                            b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
                    except Exception:
                        b64 = ""
                    misclassified.append({
                        "frame_id": _extract_frame_id(img_p),
                        "filename": Path(img_p).name,
                        "true_class": t_cls,
                        "pred_class": p_cls,
                        "confidence": round(conf_val, 3),
                        "crop_base64": b64
                    })

    elapsed = round(time.time() - start_t, 2)
    learned_profiles = compute_learned_format_profiles()

    model_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "state_dict": model.state_dict(),
        "classes": classes_list,
        "in_channels": in_channels,
        "digit_img_h": DIGIT_IMG_H,
        "digit_img_w": DIGIT_IMG_W,
        "val_acc": round(best_val_acc, 2),
        "val_reading_acc": round(best_reading_acc, 2),
        "learned_profiles": learned_profiles,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "train_samples": len(train_indices),
        "val_samples": len(val_indices),
        "epochs": epochs,
        "elapsed_sec": elapsed
    }
    torch.save(checkpoint, str(model_path))

    meta_info = {
        "classes": classes_list,
        "in_channels": in_channels,
        "val_accuracy": round(best_val_acc, 2),
        "val_reading_accuracy": round(best_reading_acc, 2),
        "learned_profiles": learned_profiles,
        "confusion_pairs": pair_confusions,
        "confusion_matrix": conf_matrix,
        "trained_at": checkpoint["trained_at"],
        "train_samples": len(train_indices),
        "val_samples": len(val_indices),
        "epochs": epochs,
        "elapsed_sec": elapsed,
        "cpu_threads": train_threads,
        "hold_out_frames": hold_out_frames,
        "use_synthetic": bool(cfg.get("use_synthetic", True)),
        "misclassified_count": len(misclassified),
        "misclassified": misclassified[:16],
        "history": history
    }
    meta_path.write_text(json.dumps(meta_info, indent=2), encoding="utf-8")

    custom_cnn_engine.reload()

    return {
        "status": "ok",
        "classes": classes_list,
        "val_accuracy": round(best_val_acc, 2),
        "val_reading_accuracy": round(best_reading_acc, 2),
        "learned_profiles": learned_profiles,
        "confusion_pairs": pair_confusions,
        "confusion_matrix": conf_matrix,
        "train_samples": len(train_indices),
        "val_samples": len(val_indices),
        "elapsed_sec": elapsed,
        "trained_at": checkpoint["trained_at"],
        "misclassified": misclassified[:20],
        "history": history,
        "dataset_stats": get_training_dataset_stats()
    }


# ==============================================================================
# 6. CUSTOM PYTORCH CNN INFERENCE ENGINE (TTA + AUTO-LEARNED DECIMALS + SAFETY)
# ==============================================================================

class CustomSevenSegInferenceEngine:
    """
    Lightweight CPU inference wrapper for the trained 2-Channel PyTorch 7-Segment CNN.
    - Runs with `intra_op_num_threads=1` per worker.
    - Always uses the `classes` list saved from `full.classes` during training.
    - Uses Test-Time Augmentation (TTA: 3-5 shifted copies per digit cell) and averages probabilities.
    - Inserts decimal points automatically based on `learned_profiles` from the training data.
    """
    def __init__(self):
        self.model: Optional[Any] = None
        self.classes: List[str] = []
        self.in_channels: int = 2
        self.learned_profiles: Dict[str, Any] = {}
        self.loaded_mtime: float = 0.0
        self.val_acc: float = 0.0
        self.val_reading_acc: float = 0.0
        self.last_readings_by_box: Dict[str, float] = {}

    def reload(self) -> bool:
        if not HAS_TORCH:
            return False
        cfg = get_train_config()
        if not cfg["enable_custom_cnn"]:
            self.model = None
            return False

        model_path = Path(cfg["custom_model_path"])
        if not model_path.is_file():
            self.model = None
            return False

        try:
            mtime = model_path.stat().st_mtime
            if self.model is not None and mtime == self.loaded_mtime:
                return True

            torch.set_num_threads(int(cfg["inference_intra_op_threads"]))

            ckpt = torch.load(str(model_path), map_location="cpu", weights_only=False)
            classes_list = list(ckpt.get("classes", []))
            if not classes_list:
                return False

            in_ch = int(ckpt.get("in_channels", 1))
            net = SevenSegmentCNN(num_classes=len(classes_list), in_channels=in_ch)
            net.load_state_dict(ckpt["state_dict"])
            net.eval()

            self.model = net
            self.classes = classes_list
            self.in_channels = in_ch
            self.val_acc = float(ckpt.get("val_acc", 0.0))
            self.val_reading_acc = float(ckpt.get("val_reading_acc", self.val_acc))
            self.learned_profiles = ckpt.get("learned_profiles") or compute_learned_format_profiles()
            self.loaded_mtime = mtime
            logger.info(
                "[Custom CNN] Loaded trained model (%d classes: %s, in_ch=%d, val_acc=%.1f%%, reading_acc=%.1f%%)",
                len(self.classes), self.classes, self.in_channels, self.val_acc, self.val_reading_acc
            )
            return True
        except Exception as e:
            logger.warning("[Custom CNN] Failed to load custom model: %s", e)
            self.model = None
            return False

    def _predict_digit_with_tta(self, d_img: Image.Image, n_shifts: int = 5) -> Tuple[str, float]:
        """
        Test-Time Augmentation (TTA): predicts on `n_shifts` slightly shifted copies
        of `d_img` ((0,0), (-1,0), (+1,0), (0,-1), (0,+1)) and averages softmax probabilities.
        """
        shifts = [(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)][:max(1, n_shifts)]
        tensors = []
        bg_val = int(np.percentile(np.array(d_img.convert("L")), 85))
        for dx, dy in shifts:
            if dx == 0 and dy == 0:
                shifted = d_img
            else:
                canvas = Image.new("L", d_img.size, color=bg_val)
                canvas.paste(d_img, (dx, dy))
                shifted = canvas
            if self.in_channels == 2:
                tensors.append(_make_two_channel_tensor(shifted))
            else:
                arr = (np.array(shifted.convert("L"), dtype=np.float32) / 255.0 - 0.5) / 0.5
                tensors.append(torch.from_numpy(arr).unsqueeze(0))

        batch = torch.stack(tensors, dim=0)
        with torch.no_grad():
            probs = F.softmax(self.model(batch), dim=1)
            avg_probs = probs.mean(dim=0)
            conf, pred_idx = avg_probs.max(dim=0)
        return self.classes[int(pred_idx.item())], float(conf.item())

    def recognize(self, pil_crop: Image.Image, box: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[float], float]:
        """
        Segments `pil_crop` into digits, classifies each slot with TTA using `self.classes`,
        and inserts the learned or configured decimal point.
        Returns (formatted_text, float_value, min_digit_confidence).
        """
        cfg = get_train_config()
        if not cfg["enable_custom_cnn"] or not HAS_TORCH:
            return "", None, 0.0

        if not self.reload() or self.model is None or not self.classes:
            return "", None, 0.0

        torch.set_num_threads(int(cfg["inference_intra_op_threads"]))

        # Refresh learned profiles dynamically so newly labeled boxes immediately use their decimal format
        learned = compute_learned_format_profiles() or self.learned_profiles
        num_digits, fixed_decimals, is_standard = _determine_expected_digits(box, learned_profiles=learned)
        digit_crops, _ = segment_roi_into_digits(pil_crop, box=box, expected_digits=num_digits, learned_profiles=learned)
        if not digit_crops:
            return "", None, 0.0

        n_tta = int(cfg.get("tta_shifts", 5))
        recognized_digits: List[str] = []
        slot_confs: List[float] = []

        for d_img in digit_crops:
            cls_name, c_val = self._predict_digit_with_tta(d_img, n_shifts=n_tta)
            if cls_name == "blank":
                continue
            if cls_name.isdigit():
                recognized_digits.append(cls_name)
                slot_confs.append(c_val)

        if not recognized_digits:
            return "", None, 0.0

        raw_digits_str = "".join(recognized_digits)
        min_conf = float(min(slot_confs))

        # Check if digit count maps to a specific learned decimal count
        if cfg.get("auto_learn_decimals", True) and learned:
            by_id = learned.get("by_box_id", {})
            by_lbl = learned.get("by_box_label", {})
            by_cnt = learned.get("by_digit_count", {})
            b_id = str(box.get("id", "")).strip() if box else ""
            b_lbl = str(box.get("label", "")).lower().strip() if box else ""
            if b_id not in by_id and b_lbl not in by_lbl and str(len(raw_digits_str)) in by_cnt:
                fixed_decimals = int(by_cnt[str(len(raw_digits_str))])

        if fixed_decimals > 0 and len(raw_digits_str) > fixed_decimals:
            int_part = raw_digits_str[:-fixed_decimals]
            dec_part = raw_digits_str[-fixed_decimals:]
            formatted = f"{int_part}.{dec_part}"
        else:
            formatted = raw_digits_str

        try:
            val = round(float(formatted), fixed_decimals) if fixed_decimals > 0 else float(int(formatted))
        except ValueError:
            val = None

        # Hard-example mining: if any digit confidence < HARD_EXAMPLE_CONF_THRESHOLD (0.90), log for review
        hard_thr = float(cfg.get("hard_example_conf_threshold", 0.90))
        if val is not None and min_conf < hard_thr:
            log_hard_example(pil_crop, box, formatted, min_conf, reason=f"digit_conf_{min_conf:.2f}_below_{hard_thr:.2f}")

        return formatted, val, min_conf


custom_cnn_engine = CustomSevenSegInferenceEngine()
