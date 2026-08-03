"""Paths, identifier safety, font resolution, grade presets."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("MONTAGE_DATA_DIR", ROOT / "data")).resolve()
BRAND_DIR = Path(os.environ.get("MONTAGE_BRAND_DIR", ROOT / "brand")).resolve()
FONTS_DIR = BRAND_DIR / "fonts"
PROFILES_DIR = BRAND_DIR / "profiles"
RUNS_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "jobs.sqlite"

AUTH_TOKEN = os.environ.get("PANDA_TOKEN", "")
AUTH_HEADER = "X-Panda-Token"

# run_id / media_id / label / output_label
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

STATE_MAX_BYTES = 256 * 1024
DOWNLOAD_TIMEOUT_S = 120

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov"}
AUDIO_EXTS = {".mp3", ".wav"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

CONTENT_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}

# Reused verbatim from tools/enhancement/color_grade.py PROFILES.
# Kept as a local copy so the service has no dependency on the tool registry.
GRADE_PRESETS: dict[str, str] = {
    "none": "",
    "warm": (
        "colorbalance=rs=0.08:gs=0.02:bs=-0.05:rh=0.06:gh=0.02:bh=-0.04,"
        "curves=all='0/0.03 0.25/0.22 0.5/0.50 0.75/0.78 1/0.97',"
        "eq=contrast=1.05:saturation=1.1"
    ),
    "cool": (
        "colorbalance=rs=-0.02:gs=-0.03:bs=0.08:rh=0.06:gh=-0.02:bh=-0.06,"
        "curves=all='0/0.02 0.25/0.20 0.5/0.48 0.75/0.78 1/0.98',"
        "eq=contrast=1.08:saturation=1.05"
    ),
    "moody": (
        "curves=all='0/0.05 0.15/0.12 0.5/0.45 0.85/0.82 1/0.95',"
        "eq=contrast=1.12:saturation=0.8:brightness=-0.03"
    ),
    "bright": (
        "curves=all='0/0.05 0.25/0.30 0.5/0.55 0.75/0.80 1/1.0',"
        "eq=contrast=1.0:saturation=1.15:brightness=0.02"
    ),
    "neutral": "eq=contrast=1.02:saturation=1.02:brightness=0.01",
}

# Fallback system font locations, tried when a profile's font isn't in brand/fonts/.
SYSTEM_FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
]


class ConfigError(RuntimeError):
    """Raised when the environment can't support rendering (missing ffmpeg/fonts)."""


def ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG_BIN", "ffmpeg")


def ffprobe_bin() -> str:
    return os.environ.get("FFPROBE_BIN", "ffprobe")


def has_ffmpeg() -> bool:
    return shutil.which(ffmpeg_bin()) is not None and shutil.which(ffprobe_bin()) is not None


def resolve_font(name: str) -> Path:
    """Find a font file by name: brand/fonts/ first, then system font dirs.

    Raises ConfigError with an actionable message when it can't be found — a
    missing CJK font silently renders Chinese captions as tofu boxes, so this
    must fail loudly rather than fall back to a Latin-only default.
    """
    candidate = FONTS_DIR / name
    if candidate.is_file():
        return candidate
    for d in SYSTEM_FONT_DIRS:
        p = d / name
        if p.is_file():
            return p
    raise ConfigError(
        f"font {name!r} not found. Drop it in {FONTS_DIR} "
        f"(a CJK-capable font is required for Chinese captions)."
    )


def fonts_ok() -> bool:
    """True when every font referenced by an installed profile resolves."""
    from montage_svc.storage import list_profiles, load_profile_raw

    names = list_profiles()
    if not names:
        return False
    try:
        for n in names:
            prof = load_profile_raw(n)
            resolve_font(prof["font_cjk"])
            resolve_font(prof["font_latin"])
    except (ConfigError, KeyError, ValueError):
        return False
    return True
