from __future__ import annotations
"""
LumenLux Custom PyTorch CPU 7-Segment CNN Trainer & Inference Engine.
Features:
1. CPU-optimized PyTorch CNN classifying the 10 digits ('0'..'9') + 'blank'.
2. Interactive ROI crop labeling & automatic multi-digit segmentation.
3. Whole-frame holdout validation split to prevent train/val digit leakage from the same photo.
4. Training augmentation: brightness/contrast jitter, small shifts, and shear, with automatic class balancing.
5. Strict mapping via `full.classes` printed during training and saved in checkpoint metadata.
6. Fixed decimal point insertion by position (e.g. 1 decimal for Chamber LCD, 3 decimals for Standard Fluke VFD).
7. Configurable thread management via .env: 4-8 threads for training, intra_op_num_threads=1 for inference workers.
"""
import io
import os
import re
import json
import time
import uuid
import base64
import random
import shutil
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

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path=None, override=False):
        if dotenv_path and Path(dotenv_path).is_file():
            for line in Path(dotenv_path).read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.split("#", 1)[0].strip()
                    if override or k not in os.environ:
                        os.environ[k] = v

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

logger = logging.getLogger("ocr_trainer")


def _resolve_env_path(env_key: str, default_rel: str) -> Path:
    raw = os.environ.get(env_key, "").strip()
    if raw:
        if os.name == "nt" and (raw.startswith("/app") or raw.startswith("\\app")):
            rel_part = raw.replace("/app/", "").replace("/app", "").replace("\\app\\", "").replace("\\app", "").strip("/\\")
            return (BASE_DIR / rel_part) if rel_part else (BASE_DIR / default_rel)
        return Path(raw)
    return BASE_DIR / default_rel


def get_train_config() -> Dict[str, Any]:
    """Read all training and inference parameters dynamically from .env."""
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
        "conf_threshold": float(os.environ.get("CUSTOM_CNN_CONF_THRESHOLD", "0.88")),
        "chamber_fixed_decimals": int(os.environ.get("CHAMBER_FIXED_DECIMALS", "1")),
        "standard_fixed_decimals": int(os.environ.get("STANDARD_FIXED_DECIMALS", "3")),
    }


DEFAULT_CLASSES = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "blank"]
DIGIT_IMG_H = 48
DIGIT_IMG_W = 32


# ==============================================================================
# 1. DIGIT SEGMENTATION FROM ROI CROP (SHARED BY LABELING & INFERENCE)
# ==============================================================================

def _determine_expected_digits(box: Optional[Dict[str, Any]] = None, clean_digits_str: Optional[str] = None) -> Tuple[int, int, bool]:
    """
    Returns (num_digits, fixed_decimals, is_standard) for a given ROI box or label string.
    """
    cfg = get_train_config()
    cam_type = (box.get("camera_type") or box.get("target") or "").lower() if box else ""
    lbl = str(box.get("label", "")).lower() if box else ""
    is_standard = (cam_type == "device" or "standard" in lbl or "fluke" in lbl or "1502" in lbl)

    if box and box.get("decimals") is not None:
        try:
            fixed_decimals = int(box["decimals"])
        except (ValueError, TypeError):
            fixed_decimals = cfg["standard_fixed_decimals"] if is_standard else cfg["chamber_fixed_decimals"]
    else:
        fixed_decimals = cfg["standard_fixed_decimals"] if is_standard else cfg["chamber_fixed_decimals"]

    if clean_digits_str:
        num_digits = len(clean_digits_str)
    else:
        num_digits = 5 if is_standard else 3

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
    expected_digits: Optional[int] = None
) -> Tuple[List[Image.Image], Optional[Image.Image]]:
    """
    Segments a straightened ROI display crop into individual digit crops (48x32 grayscale)
    plus an optional 'blank' background/unit patch.
    Handles both emissive VFD/LED displays (Fluke 1502A) and reflective 7-segment LCDs (HTC-1).
    """
    num_digits, _, is_standard = _determine_expected_digits(box)
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
        # Convert glowing intensity into dark-on-white grayscale so VFD and LCD share identical polarity
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
            # Find the wide empty valley preceding the '°C' unit symbol (typically between 64% and 86% of span_w)
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
        # Mask out bottom-edge decimal point dot in upper-column valley profile so decimal dots never skew slot boundaries
        work_stroke_upper = work_stroke[:max(4, int(work_stroke.shape[0] * 0.72)), :]
    else:
        # Standard reflective 7-segment LCD (e.g. HTC-1)
        if HAS_CV2 and cv2 is not None:
            gray_np = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(6, 6))
            gray_eq = clahe.apply(gray_np)
            sigma = max(5.0, min(h, w) / 7.0)
            bg = cv2.GaussianBlur(gray_eq, (0, 0), sigmaX=sigma, sigmaY=sigma)
            local_dark = cv2.subtract(bg, gray_eq)
        else:
            gray_eq = np.array(ImageOps.autocontrast(ImageOps.grayscale(Image.fromarray(rgb)), cutoff=2))
            local_dark = np.clip(int(np.median(gray_eq)) - gray_eq.astype(int), 0, 255).astype(np.uint8)

        # Mask out outer bezel border so plastic frame shadows are ignored
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

    # On 5-digit emissive VFDs (e.g. Fluke 1502A '14.954'), check if leading digit is a narrow '1' (~6-15% of width)
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
# 2. PYTORCH 7-SEGMENT CNN ARCHITECTURE & CLASS-BALANCED DATASET
# ==============================================================================

if HAS_TORCH:
    class SevenSegmentCNN(nn.Module):
        """
        Compact, high-accuracy CPU CNN for 7-segment LCD & VFD digit classification.
        Input: (B, 1, 48, 32) grayscale tensor.
        Output: (B, num_classes) logits mapped strictly to `full.classes`.
        """
        def __init__(self, num_classes: int = 11):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(1, 24, kernel_size=3, padding=1),
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
            x = self.features(x)
            return self.classifier(x)

    class BalancedAugmentedDataset(Dataset):
        """
        Balances all training classes to at least `target_per_class` samples per epoch
        and applies stochastic augmentations (brightness/contrast jitter, small shifts, and shear)
        so rare digits (e.g. '3', '7', '8') are learned with equal accuracy to frequent digits ('1', '4').
        """
        def __init__(self, base_dataset: Any, indices: List[int], transform: Any, target_per_class: int = 32):
            self.base_dataset = base_dataset
            self.transform = transform
            by_class: Dict[int, List[int]] = {}
            for idx in indices:
                _, cls_idx = base_dataset.samples[idx]
                by_class.setdefault(cls_idx, []).append(idx)

            max_count = max([len(v) for v in by_class.values()] + [target_per_class])
            per_cls = max(target_per_class, min(64, max_count))
            self.balanced_indices: List[int] = []
            rng = random.Random(123)
            for cls_idx, cls_idxs in sorted(by_class.items()):
                for i in range(per_cls):
                    self.balanced_indices.append(cls_idxs[i % len(cls_idxs)])
            rng.shuffle(self.balanced_indices)

        def __len__(self) -> int:
            return len(self.balanced_indices)

        def __getitem__(self, idx: int) -> Tuple[Any, int, str]:
            real_idx = self.balanced_indices[idx]
            img_path, target = self.base_dataset.samples[real_idx]
            with Image.open(img_path) as img:
                gray = img.convert("L")
            tensor = self.transform(gray) if self.transform else gray
            return tensor, target, img_path

    class EvalSubset(Dataset):
        def __init__(self, base_dataset: Any, indices: List[int], transform: Any):
            self.base_dataset = base_dataset
            self.indices = indices
            self.transform = transform

        def __len__(self) -> int:
            return len(self.indices)

        def __getitem__(self, idx: int) -> Tuple[Any, int, str]:
            real_idx = self.indices[idx]
            img_path, target = self.base_dataset.samples[real_idx]
            with Image.open(img_path) as img:
                gray = img.convert("L")
            tensor = self.transform(gray) if self.transform else gray
            return tensor, target, img_path


# ==============================================================================
# 3. DATA COLLECTION & LABELING PIPELINE
# ==============================================================================

def save_labeled_roi_crop(
    pil_crop: Image.Image,
    label_value_str: str,
    box: Optional[Dict[str, Any]] = None,
    project_id: str = "manual"
) -> Dict[str, Any]:
    """
    Takes a cropped ROI image and the user-provided reading (e.g. '14.6' or '14.954'),
    segments it into individual digits, saves the full frame + individual digit crops
    into `<TRAIN_DATA_DIR>/digits/<class_name>/<frame_id>_d<pos>.png`, and returns
    base64 previews of each segmented digit for instant UI inspection.
    """
    cfg = get_train_config()
    train_dir = Path(cfg["train_data_dir"])
    digits_dir = train_dir / "digits"
    frames_dir = train_dir / "frames"

    for c in DEFAULT_CLASSES:
        (digits_dir / c).mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    raw_str = str(label_value_str).strip()
    clean_digits = re.sub(r"[^0-9]", "", raw_str)
    if not clean_digits:
        raise ValueError(f"Label '{raw_str}' does not contain any digits (0-9).")

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
    catalog = []
    if catalog_path.is_file():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except Exception:
            catalog = []

    crop_buf = io.BytesIO()
    pil_crop.convert("RGB").save(crop_buf, format="JPEG", quality=88)
    crop_b64 = "data:image/jpeg;base64," + base64.b64encode(crop_buf.getvalue()).decode("ascii")

    entry = {
        "frame_id": frame_id,
        "timestamp": time.time(),
        "project_id": project_id,
        "box_label": box.get("label", "Display") if box else "Display",
        "camera_type": (box.get("camera_type") or box.get("target") or "chamber") if box else "chamber",
        "label_reading": raw_str,
        "digits": list(clean_digits),
        "crop_base64": crop_b64,
        "digit_previews": saved_digits_info
    }
    catalog.insert(0, entry)
    catalog = catalog[:250]
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "frame_id": frame_id,
        "label_reading": raw_str,
        "digits_saved": len(saved_digits_info),
        "digit_previews": saved_digits_info,
        "dataset_stats": get_training_dataset_stats()
    }


def delete_labeled_frame(frame_id: str) -> Dict[str, Any]:
    """Remove a labeled frame and all its derived digit crops if the user spots a bad ROI alignment."""
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
    if catalog_path.is_file():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog = [c for c in catalog if c.get("frame_id") != frame_id]
            catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "status": "ok",
        "removed_files": removed_files,
        "dataset_stats": get_training_dataset_stats()
    }


def get_training_dataset_stats() -> Dict[str, Any]:
    """Return per-class sample counts, total labeled frames, and current model status."""
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
        "recent_frames": catalog[:30],
        "model_trained": model_path.is_file(),
        "model_info": model_info,
        "config": cfg
    }


# ==============================================================================
# 4. CPU TRAINING LOOP (AUGMENTATION + WHOLE-FRAME HOLDOUT + FULL.CLASSES)
# ==============================================================================

def _extract_frame_id(filepath: str) -> str:
    stem = Path(filepath).stem
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def train_custom_model(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Trains the custom PyTorch 7-Segment CNN on CPU using:
    - 4-8 CPU threads (from TRAIN_CPU_THREADS in .env).
    - Whole-frame holdout validation split to avoid train/val leakage.
    - Class-balanced sampling + brightness/contrast jitter, small shifts, and shear augmentation.
    - Full `.classes` list printed and persisted for exact output index mapping.
    - Misclassified crop capture so the user can inspect any ROI alignment issues.
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
            "Use the 'Test' button on ROI boxes to label a few readings first!"
        )

    # Set CPU training threads (4-8 threads is plenty on CPU)
    train_threads = int(cfg["train_cpu_threads"])
    torch.set_num_threads(train_threads)

    # Load full dataset via ImageFolder to obtain canonical `full.classes`
    full = datasets.ImageFolder(str(digits_dir))
    classes_list = list(full.classes)
    logger.info("[PyTorch Trainer] Training on CPU (%d threads) | full.classes = %s", train_threads, classes_list)
    print(f"[PyTorch Trainer] full.classes = {classes_list}")

    if len(full.samples) < 6:
        raise ValueError(f"Only {len(full.samples)} digit samples found. Please label at least 2-3 ROI readings before training.")

    # Whole-frame holdout split (holds out whole frames, not individual digits from the same photo)
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
        # Hold out whole frames ONLY when all digits in that frame belong to classes with >= 3 total samples
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

    # Augmentation pipeline (brightness/contrast jitter, small shifts, and shear)
    train_transform = transforms.Compose([
        transforms.Resize((DIGIT_IMG_H, DIGIT_IMG_W)),
        transforms.ColorJitter(
            brightness=float(cfg["augment_brightness"]),
            contrast=float(cfg["augment_contrast"])
        ),
        transforms.RandomAffine(
            degrees=2.5,
            translate=(float(cfg["augment_shift"]), float(cfg["augment_shift"])),
            scale=(0.94, 1.06),
            shear=(-float(cfg["augment_shear"]), float(cfg["augment_shear"])),
            fill=235
        ),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.5], std=[0.5])
    ])

    val_transform = transforms.Compose([
        transforms.Resize((DIGIT_IMG_H, DIGIT_IMG_W)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.5], std=[0.5])
    ])

    train_ds = BalancedAugmentedDataset(full, train_indices, train_transform, target_per_class=36)
    val_ds = EvalSubset(full, val_indices, val_transform)

    batch_size = min(int(cfg["train_batch_size"]), max(4, len(train_ds)))
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False)

    device = torch.device("cpu")
    model = SevenSegmentCNN(num_classes=len(classes_list)).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(cfg["train_lr"]), weight_decay=1e-4)
    epochs = int(cfg["train_epochs"])
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.CrossEntropyLoss()

    start_t = time.time()
    best_val_acc = -1.0
    best_state = None
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
        with torch.no_grad():
            for x, y, _ in val_loader:
                x, y = x.to(device), y.to(device)
                logits = model(x)
                loss = criterion(logits, y)
                val_loss += float(loss.item()) * x.size(0)
                preds = logits.argmax(dim=1)
                val_correct += int((preds == y).sum().item())
                val_total += x.size(0)

        t_acc = (train_correct / max(1, train_total)) * 100.0
        v_acc = (val_correct / max(1, val_total)) * 100.0
        if v_acc >= best_val_acc:
            best_val_acc = v_acc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        if epoch == 1 or epoch % 5 == 0 or epoch == epochs:
            history.append({
                "epoch": epoch,
                "train_loss": round(train_loss / max(1, train_total), 4),
                "train_acc": round(t_acc, 2),
                "val_loss": round(val_loss / max(1, val_total), 4),
                "val_acc": round(v_acc, 2)
            })

    if best_state is not None:
        model.load_state_dict(best_state)

    # Collect misclassified validation crops (for diagnostic inspection if accuracy < 98%)
    model.eval()
    misclassified = []
    with torch.no_grad():
        for x, y, paths in val_loader:
            probs = F.softmax(model(x), dim=1)
            confs, preds = probs.max(dim=1)
            for i in range(x.size(0)):
                true_idx = int(y[i].item())
                pred_idx = int(preds[i].item())
                conf_val = float(confs[i].item())
                img_p = paths[i]
                if pred_idx != true_idx or conf_val < 0.75:
                    try:
                        with open(img_p, "rb") as f:
                            b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
                    except Exception:
                        b64 = ""
                    misclassified.append({
                        "frame_id": _extract_frame_id(img_p),
                        "filename": Path(img_p).name,
                        "true_class": classes_list[true_idx],
                        "pred_class": classes_list[pred_idx],
                        "confidence": round(conf_val, 3),
                        "crop_base64": b64
                    })

    elapsed = round(time.time() - start_t, 2)

    for c in DEFAULT_CLASSES:
        (digits_dir / c).mkdir(parents=True, exist_ok=True)

    model_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "state_dict": model.state_dict(),
        "classes": classes_list,
        "img_h": DIGIT_IMG_H,
        "img_w": DIGIT_IMG_W,
        "val_acc": round(best_val_acc, 2),
        "trained_at": time.time(),
    }
    torch.save(checkpoint, str(model_path))

    meta = {
        "classes": classes_list,
        "val_accuracy": round(best_val_acc, 2),
        "train_samples": len(train_indices),
        "val_samples": len(val_indices),
        "unique_frames": len(unique_frames),
        "hold_out_frames": hold_out_frames,
        "epochs": epochs,
        "elapsed_sec": elapsed,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "misclassified_count": len(misclassified),
        "misclassified": misclassified[:20],
        "history": history
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    custom_cnn_engine.reload()

    return {
        "status": "ok",
        "classes": classes_list,
        "val_accuracy": round(best_val_acc, 2),
        "train_samples": len(train_indices),
        "val_samples": len(val_indices),
        "unique_frames": len(unique_frames),
        "elapsed_sec": elapsed,
        "misclassified": misclassified[:20],
        "history": history,
        "dataset_stats": get_training_dataset_stats()
    }


# ==============================================================================
# 5. CUSTOM PYTORCH CNN INFERENCE ENGINE (USES FULL.CLASSES + FIXED DECIMAL)
# ==============================================================================

class CustomSevenSegInferenceEngine:
    """
    Lightweight CPU inference wrapper for the trained PyTorch 7-Segment CNN.
    - Runs with `intra_op_num_threads=1` per worker as recommended.
    - Always uses the `classes` list saved from `full.classes` during training.
    - Inserts the fixed decimal point by position (1 decimal for Chamber, 3 for Standard).
    """
    def __init__(self):
        self.model: Optional[Any] = None
        self.classes: List[str] = []
        self.loaded_mtime: float = 0.0
        self.val_acc: float = 0.0

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

            net = SevenSegmentCNN(num_classes=len(classes_list))
            net.load_state_dict(ckpt["state_dict"])
            net.eval()

            self.model = net
            self.classes = classes_list
            self.val_acc = float(ckpt.get("val_acc", 0.0))
            self.loaded_mtime = mtime
            logger.info(
                "[Custom CNN] Loaded trained model (%d classes: %s, val_acc=%.1f%%, intra_op_threads=%d)",
                len(self.classes), self.classes, self.val_acc, int(cfg["inference_intra_op_threads"])
            )
            return True
        except Exception as e:
            logger.warning("[Custom CNN] Failed to load custom model: %s", e)
            self.model = None
            return False

    def recognize(self, pil_crop: Image.Image, box: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[float], float]:
        """
        Segments `pil_crop` into digits, classifies each slot using `self.classes`,
        and inserts the fixed decimal point by position.
        Returns (formatted_text, float_value, mean_confidence).
        """
        cfg = get_train_config()
        if not cfg["enable_custom_cnn"] or not HAS_TORCH:
            return "", None, 0.0

        if not self.reload() or self.model is None or not self.classes:
            return "", None, 0.0

        torch.set_num_threads(int(cfg["inference_intra_op_threads"]))

        num_digits, fixed_decimals, is_standard = _determine_expected_digits(box)
        digit_crops, _ = segment_roi_into_digits(pil_crop, box=box, expected_digits=num_digits)
        if not digit_crops:
            return "", None, 0.0

        tensors = []
        for d_img in digit_crops:
            arr = (np.array(d_img.convert("L"), dtype=np.float32) / 255.0 - 0.5) / 0.5
            tensors.append(torch.from_numpy(arr).unsqueeze(0))

        batch = torch.stack(tensors, dim=0)
        with torch.no_grad():
            probs = F.softmax(self.model(batch), dim=1)
            confs, pred_indices = probs.max(dim=1)

        recognized_digits: List[str] = []
        slot_confs: List[float] = []
        for idx in range(len(digit_crops)):
            cls_name = self.classes[int(pred_indices[idx].item())]
            c_val = float(confs[idx].item())
            if cls_name == "blank":
                continue
            if cls_name.isdigit():
                recognized_digits.append(cls_name)
                slot_confs.append(c_val)

        if not recognized_digits:
            return "", None, 0.0

        raw_digits_str = "".join(recognized_digits)
        mean_conf = float(min(slot_confs))  # Use minimum slot confidence so all digits must be confident

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

        return formatted, val, mean_conf


custom_cnn_engine = CustomSevenSegInferenceEngine()
