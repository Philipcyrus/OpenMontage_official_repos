"""Directory layout, path safety, media lookup, and brand profiles.

Every path the service touches is derived server-side from a validated
run_id/media_id — request bodies never carry filesystem paths.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from montage_svc.config import (
    AUDIO_EXTS,
    BRAND_DIR,
    DATA_DIR,
    FONTS_DIR,
    IMAGE_EXTS,
    MEDIA_EXTS,
    PROFILES_DIR,
    RUNS_DIR,
    SAFE_ID,
    VIDEO_EXTS,
)


class StorageError(ValueError):
    """Bad identifier, escape attempt, or missing asset."""


# --------------------------------------------------------------------------
# identifiers + path safety
# --------------------------------------------------------------------------

def check_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not SAFE_ID.match(value):
        raise StorageError(
            f"invalid {what} {value!r}: must match ^[a-zA-Z0-9_-]{{1,64}}$"
        )
    return value


def under_data(path: Path) -> Path:
    """Resolve `path` and assert it stays inside DATA_DIR (symlinks included)."""
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(DATA_DIR)
    except ValueError:
        raise StorageError(f"path escapes data dir: {path}") from None
    return resolved


# --------------------------------------------------------------------------
# run layout
# --------------------------------------------------------------------------

def run_dir(run_id: str) -> Path:
    return under_data(RUNS_DIR / check_id(run_id, "run_id"))


def media_dir(run_id: str) -> Path:
    return run_dir(run_id) / "media"


def out_dir(run_id: str) -> Path:
    return run_dir(run_id) / "out"


def work_dir(run_id: str, job_id: str) -> Path:
    """Scratch space for one job. Caller is responsible for cleanup."""
    return run_dir(run_id) / "_work" / check_id(job_id, "job_id")


def state_path(run_id: str) -> Path:
    return run_dir(run_id) / "state.json"


def ensure_run(run_id: str) -> Path:
    d = run_dir(run_id)
    (d / "media").mkdir(parents=True, exist_ok=True)
    (d / "out").mkdir(parents=True, exist_ok=True)
    return d


def ensure_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    FONTS_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------

def kind_for_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    raise StorageError(f"unsupported media extension {ext!r}")


def find_media(run_id: str, media_id: str) -> Path:
    """Locate runs/{run_id}/media/{media_id}.* — extension-agnostic."""
    check_id(media_id, "media_id")
    d = media_dir(run_id)
    for ext in sorted(MEDIA_EXTS):
        p = d / f"{media_id}{ext}"
        if p.is_file():
            return under_data(p)
    raise StorageError(f"media {media_id!r} not found in run {run_id!r}")


def media_exists(run_id: str, media_id: str) -> bool:
    try:
        find_media(run_id, media_id)
        return True
    except StorageError:
        return False


def missing_media(run_id: str, media_ids: list[str]) -> list[str]:
    return [m for m in dict.fromkeys(media_ids) if not media_exists(run_id, m)]


def save_media(run_id: str, label: str, ext: str, data: bytes) -> Path:
    """Write media bytes. Same label overwrites — a new version of that asset."""
    check_id(label, "label")
    kind_for_ext(ext)  # validates
    ensure_run(run_id)
    p = under_data(media_dir(run_id) / f"{label}{ext.lower()}")
    # Drop any other extension carrying this label, so find_media stays unambiguous.
    for other in MEDIA_EXTS:
        q = media_dir(run_id) / f"{label}{other}"
        if q.is_file() and q != p:
            q.unlink()
    p.write_bytes(data)
    return p


def files_url(path: Path) -> str:
    """Map an on-disk path under ./data to its public /files/ URL."""
    rel = under_data(path).relative_to(DATA_DIR).as_posix()
    return f"/files/{rel}"


def resolve_source(run_id: str, source: str) -> Path:
    """Resolve a `source` field like 'out/final_v1.mp4' inside a run.

    Only `out/` and `media/` are reachable, and only with a safe basename —
    this is the one place a client-supplied string becomes a path, so it is
    parsed structurally rather than joined.
    """
    parts = source.replace("\\", "/").split("/")
    if len(parts) != 2 or parts[0] not in ("out", "media"):
        raise StorageError(
            f"invalid source {source!r}: expected 'out/<file>' or 'media/<file>'"
        )
    folder, name = parts
    stem, dot, ext = name.rpartition(".")
    if not dot:
        raise StorageError(f"invalid source {source!r}: missing extension")
    check_id(stem, "source filename")
    kind_for_ext(f".{ext}")
    p = under_data(run_dir(run_id) / folder / f"{stem}.{ext.lower()}")
    if not p.is_file():
        raise StorageError(f"source {source!r} not found in run {run_id!r}")
    return p


# --------------------------------------------------------------------------
# brand profiles
# --------------------------------------------------------------------------

def brand_file(rel: str | None) -> Path | None:
    """Resolve a brand-relative asset path, confined to BRAND_DIR."""
    if not rel:
        return None
    p = (BRAND_DIR / rel).resolve()
    try:
        p.relative_to(BRAND_DIR)
    except ValueError:
        raise StorageError(f"brand asset escapes brand dir: {rel}") from None
    return p if p.is_file() else None


def list_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load_profile_raw(name: str) -> dict[str, Any]:
    check_id(name, "profile")
    p = PROFILES_DIR / f"{name}.json"
    if not p.is_file():
        raise StorageError(
            f"profile {name!r} not found. Available: {list_profiles() or 'none'}"
        )
    return json.loads(p.read_text(encoding="utf-8"))


def load_profile(name: str) -> dict[str, Any]:
    """Load a profile merged over the built-in defaults, so a partial
    hand-edited profile can't crash a render with a KeyError."""
    prof = _deep_merge(_default_profile(name), load_profile_raw(name))
    prof["name"] = name
    return prof


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Wordmark naming in brand/panda-mobile is inverted on purpose (see the repo's
# generate_overlays.py): "PandaLogoWhite" is the BLACK wordmark, for light
# backgrounds; "PandaLogoBlack" is the WHITE wordmark, for dark pills.
_BLACK_WORDMARK = "panda-mobile/logo/hires/PandaLogoWhite_6x.png"
_WHITE_WORDMARK = "panda-mobile/logo/hires/PandaLogoBlack_6x.png"

YELLOW = [253, 197, 13, 255]
BLACK = [17, 17, 17, 255]
WHITE = [255, 255, 255, 255]
GREY = [90, 90, 90, 255]


def _default_profile(name: str) -> dict[str, Any]:
    """Shared skeleton. Only the two shipped profiles get opinionated values."""
    return {
        "name": name,
        "description": "",
        "font_cjk": "msyhbd.ttc",
        "font_latin": "msyhbd.ttc",
        "grade_default": "none",
        "caption": {
            "zh_size": 56,
            "en_size": 40,
            "zh_color": WHITE,
            "en_color": WHITE,
            "gap": 14,
            "bottom_margin": 180,
            "max_width_frac": 0.93,
            "scrim": {"enabled": True, "color": [0, 0, 0, 150], "radius": 34,
                      "pad_x": 46, "pad_y": 34},
            "stroke": {"enabled": False, "color": [0, 0, 0, 255], "width": 0},
        },
        "bubble": {
            "title_size": 30,
            "value_size": 46,
            "accent": [216, 50, 50, 255],
            "card": WHITE,
            "title_color": GREY,
            "value_color": BLACK,
        },
        "callout": {
            "size": 40,
            "color": BLACK,
            "bg": YELLOW,
            "radius": 26,
            "pad_x": 40,
            "pad_y": 24,
        },
        "logo": {"enabled": False},
        "cards": {"enabled": False},
    }


def default_profiles() -> dict[str, dict[str, Any]]:
    """The two profiles the brief asks for, written on first run.

    Documented fields:
      font_cjk / font_latin   — filenames resolved against brand/fonts/ then system fonts
      grade_default           — preset name used when a request omits `grade`
      caption.*               — zh/en sizes + colors, scrim box, optional stroke, position
      bubble.* / callout.*    — styling for the extra per-scene overlay types
      logo.*                  — watermark image, size, corner, margins, pill, opacity;
                                `enabled: false` = no logo anywhere (UGC)
      cards.*                 — intro/outro card look + durations; `enabled: false` = ignore
                                the `cards` block of a compose request
    """
    bgc = _deep_merge(_default_profile("bgc"), {
        "description": "Brand-governed content: logo watermark, brand colors, official cards.",
        "grade_default": "warm",
        "caption": {
            "zh_color": YELLOW,
            "en_color": WHITE,
            "scrim": {"enabled": True, "color": [0, 0, 0, 150], "radius": 34,
                      "pad_x": 46, "pad_y": 34},
        },
        "logo": {
            "enabled": True,
            "image": _WHITE_WORDMARK,
            "width": 300,
            "position": "top-right",
            "margin_x": 40,
            "margin_y": 70,
            "opacity": 1.0,
            "pill": {"enabled": True, "color": [0, 0, 0, 120], "pad": 22},
        },
        "cards": {
            "enabled": True,
            "bg": WHITE,
            "logo_image": _BLACK_WORDMARK,
            "logo_width": 540,
            "accent": YELLOW,
            "title_size": 60,
            "subtitle_size": 42,
            "cta_size": 48,
            "title_color": BLACK,
            "subtitle_color": GREY,
            "cta_bg": BLACK,
            "cta_color": YELLOW,
            "intro_duration_s": 2.0,
            "outro_duration_s": 2.5,
        },
    })
    ugc = _deep_merge(_default_profile("ugc"), {
        "description": "Creator-native: no logo, no cards, plain white captions with a soft scrim.",
        "grade_default": "none",
        "caption": {
            "zh_size": 52,
            "en_size": 38,
            "zh_color": WHITE,
            "en_color": WHITE,
            "bottom_margin": 300,
            "scrim": {"enabled": True, "color": [0, 0, 0, 120], "radius": 22,
                      "pad_x": 34, "pad_y": 24},
            "stroke": {"enabled": True, "color": [0, 0, 0, 255], "width": 3},
        },
        "logo": {"enabled": False},
        "cards": {"enabled": False},
    })
    return {"bgc": bgc, "ugc": ugc}


def ensure_profiles() -> list[str]:
    """Write default profiles for any that don't exist. Returns names created."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for name, body in default_profiles().items():
        p = PROFILES_DIR / f"{name}.json"
        if not p.is_file():
            p.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
            created.append(name)
    return created


def ensure_default_logo() -> None:
    """The brief specifies ./brand/logo.png. Seed it from the panda wordmark if
    it isn't there, so a profile with no `logo.image` still has something."""
    dest = BRAND_DIR / "logo.png"
    if dest.is_file():
        return
    src = brand_file(_WHITE_WORDMARK)
    if src:
        shutil.copyfile(src, dest)
