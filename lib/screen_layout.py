"""User screenshots laid over generated Panda shots — shared layout logic.

A user may attach screenshots to a panda-video, panda-carousel or panda-image brief and say
which one goes in which scene (a carousel slide, or the one image) and how to use it. The
scene-plan director writes a *layout* for each placement onto the scene's ``required_assets``
item (``source: "provided"``). That one layout drives the still / clip prompts (which area stays
empty), the gate boards, the launcher checks and the Remotion render — so they cannot disagree.

Video: the screenshots are laid over each clip at compose (timed steps). Carousel / image: the
launcher places them onto every approved still (settled state, no timing), so the stills the
user reviews and the brand pass stamps already carry them.

Used by ``tools/video/screen_overlay.py`` (renders) and ``dify_launcher/screens.py`` (intake,
checks, prompt facts). Stdlib only at import time.

Geometry mirrors ``remotion-composer/src/panda/screenGeometry.ts`` — keep the two in step.

Project files (all launcher-owned, never under ``assets/``):
    projects/<job>/inputs/in_01.png ...   normalised uploads
    projects/<job>/inputs/inputs.json     [{n, input_id, name, file, width, height, sha256}]
    projects/<job>/inputs/job.json        {pipeline, language} — the job as the launcher runs it
    projects/<job>/inputs/requests.json   the user's guidance, written by the idea director
    projects/<job>/overlay/               boards, composite clips / stills, work files
    projects/<job>/overlay/timeline.json  video: where every scene and screenshot lands in
                                          the assembled video (written by compose)
    projects/<job>/overlay/checks.log     why a check could not run (diagnostics only)
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Optional

INPUTS_DIR = "inputs"
OVERLAY_DIR = "overlay"
INPUTS_FILE = "inputs.json"
REQUESTS_FILE = "requests.json"
JOB_FILE = "job.json"

VIDEO_PIPELINE = "panda-video"
STILL_PIPELINES = ("panda-carousel", "panda-image")
PIPELINES = (VIDEO_PIPELINE, *STILL_PIPELINES)
DEFAULT_ASPECTS = {"panda-video": "9:16", "panda-carousel": "4:5", "panda-image": "1:1"}

FRAMES = ("phone", "browser", "card", "none", "held")
MOTIONS = ("none", "fade", "pop", "slide_left", "slide_right", "slide_up", "slide_down")
# Held-phone overlays stay axis-aligned on a fixed screen rect — no slide enter/exit.
HELD_FORBIDDEN_MOTIONS = ("slide_left", "slide_right", "slide_up", "slide_down")
STEP_KINDS = ("blur_region", "highlight_box", "cursor_move", "click_pulse", "zoom_to", "card")

# Chrome sizes as fractions of the zone's shorter side (mirrors CHROME in screenGeometry.ts).
# "held" = screenshot fills the generated phone's blank screen (no Remotion chrome).
CHROME: dict[str, dict[str, float]] = {
    "phone": {"side": 0.035, "top": 0.07, "bottom": 0.05},
    "browser": {"side": 0.012, "top": 0.085, "bottom": 0.012},
    "card": {"side": 0.03, "top": 0.03, "bottom": 0.03},
    "none": {"side": 0.0, "top": 0.0, "bottom": 0.0},
    "held": {"side": 0.0, "top": 0.0, "bottom": 0.0},
}

CANVASES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "3:4": (1080, 1440),
    "4:3": (1440, 1080),
}
DEFAULT_ASPECT = "9:16"

# Areas Panda's own render draws on top of every scene, as fractions of the frame:
# the caption scrim (ugc profile, one zh + one en line — longer captions grow upward) and the
# logo pill that the brand step stamps (bgc profile). Measured from
# vendor/montage_svc/render/overlays.py; tests/contracts/test_screen_layout.py re-measures.
KEEP_OUT: dict[tuple[int, int], dict[str, dict[str, float]]] = {
    (1080, 1920): {"captions": {"x": 0.03, "y": 0.76, "w": 0.94, "h": 0.09},
                   "logo": {"x": 0.64, "y": 0.03, "w": 0.33, "h": 0.10}},
    (1920, 1080): {"captions": {"x": 0.03, "y": 0.58, "w": 0.94, "h": 0.15},
                   "logo": {"x": 0.79, "y": 0.06, "w": 0.20, "h": 0.17}},
    (1080, 1080): {"captions": {"x": 0.03, "y": 0.58, "w": 0.94, "h": 0.15},
                   "logo": {"x": 0.64, "y": 0.06, "w": 0.33, "h": 0.17}},
    (1080, 1350): {"captions": {"x": 0.03, "y": 0.66, "w": 0.94, "h": 0.13},
                   "logo": {"x": 0.64, "y": 0.05, "w": 0.33, "h": 0.13}},
    (1080, 1440): {"captions": {"x": 0.03, "y": 0.68, "w": 0.94, "h": 0.12},
                   "logo": {"x": 0.64, "y": 0.04, "w": 0.33, "h": 0.13}},
    (1440, 1080): {"captions": {"x": 0.03, "y": 0.57, "w": 0.94, "h": 0.16},
                   "logo": {"x": 0.72, "y": 0.05, "w": 0.26, "h": 0.18}},
}

# The brand stamp is a FIXED-PIXEL pill (vendor/montage_svc/render/overlays.py draw_logo: a
# 300 px logo + 22 px pill padding, 40 px from the right edge, 70 px from the top), so on a
# smaller frame it covers a larger share of it. A carousel / image still is whatever size the
# image model returned (1024x1024 and friends, see dify_launcher/CAROUSEL.md), so their logo
# keep-out is sized for the smallest still a model returns — not for the 1080 mock canvases.
# tests/contracts/test_screen_layout.py re-measures both against the real draw_logo.
LOGO_STAMP_PX = (384, 235)
MIN_STILL_PIXELS = 960 * 960

# The caption scrim in KEEP_OUT is ONE zh + ONE en line. A longer caption wraps and the scrim
# grows upward from the same bottom margin, so the table alone would accept a screenshot that a
# wrapped bilingual caption then covers. measure_caption() draws the scene's real caption with
# panda_render's own renderer and profile and measures it; when that is not possible the table box
# is grown upward to CAPTION_FALLBACK_LINES times its height as a documented conservative
# fallback, and the caller says the area is an estimate. Video only — a carousel / image still
# draws its copy into the still itself.
CAPTION_PROFILE = "ugc"                 # the profile panda_render renders at (CLEAN, no brand)
CAPTION_FALLBACK_LINES = 3.0
_CAPTION_CACHE: dict[str, Optional[dict[str, float]]] = {}

# Legibility: how much the screenshot is scaled on screen. Below MIN the text gets tiny,
# above MAX it is upscaled and soft. Tune on real renders.
LEGIBLE_MIN_SCALE = 0.35
LEGIBLE_MAX_SCALE = 2.0
EPS = 0.005
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "artifacts"


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def inputs_dir(project_dir: Path) -> Path:
    return Path(project_dir) / INPUTS_DIR


def overlay_dir(project_dir: Path) -> Path:
    return Path(project_dir) / OVERLAY_DIR


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_inputs(project_dir: Path) -> list[dict[str, Any]]:
    data = _read_json(inputs_dir(project_dir) / INPUTS_FILE)
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("input_id")]


def has_inputs(project_dir: Path) -> bool:
    return bool(load_inputs(project_dir))


def pipeline_of(project_dir: Path) -> str:
    """The pipeline the uploads were accepted for (inputs/job.json); panda-video if unknown."""
    data = _read_json(inputs_dir(project_dir) / JOB_FILE)
    name = str((data or {}).get("pipeline") or "") if isinstance(data, dict) else ""
    return name if name in PIPELINES else VIDEO_PIPELINE


def is_still_pipeline(pipeline: Optional[str]) -> bool:
    return pipeline in STILL_PIPELINES


def unit_word(pipeline: Optional[str]) -> str:
    """What a scene is called for this pipeline: scene / slide / image."""
    return {"panda-carousel": "slide", "panda-image": "image"}.get(str(pipeline), "scene")


def unit_label(pipeline: Optional[str], n: int) -> str:
    """'scene 3' / 'slide 3' / 'the image' (panda-image makes one image)."""
    word = unit_word(pipeline)
    if pipeline == "panda-image" and n == 1:
        return "the image"
    return f"{word} {n}"


def deliverable_word(pipeline: Optional[str]) -> str:
    return {"panda-carousel": "carousel", "panda-image": "image"}.get(str(pipeline), "video")


def baked_text_words(pipeline: Optional[str]) -> tuple[str, str]:
    """What the text drawn INTO the deliverable is called: (in a still, as a body of copy).

    A carousel has slide copy, panda-image has on-image copy; a video's text is the caption
    strip the render draws later, so it stays the plain word.
    """
    if pipeline == "panda-carousel":
        return "slide text", "slide copy"
    if pipeline == "panda-image":
        return "on-image text", "on-image copy"
    return "text", "copy"


def load_requests(project_dir: Path) -> Optional[list[dict[str, Any]]]:
    data = _read_json(inputs_dir(project_dir) / REQUESTS_FILE)
    if isinstance(data, dict) and isinstance(data.get("requests"), list):
        data = data["requests"]
    if not isinstance(data, list):
        return None
    return [d for d in data if isinstance(d, dict)]


def load_scene_plan(project_dir: Path) -> Optional[dict[str, Any]]:
    """The scene plan the launcher acts on: the scene_plan checkpoint or artifacts/scene_plan.json,
    whichever was written LAST.

    A later leg (a stills revise that moves a screenshot to another slide) rewrites
    artifacts/scene_plan.json and may leave the approved checkpoint alone. Preferring the
    checkpoint unconditionally placed the screenshots from the stale plan, so the reviewer saw the
    move ignored with nothing to explain it.
    """
    project_dir = Path(project_dir)
    cp_path = project_dir / "checkpoint_scene_plan.json"
    art_path = project_dir / "artifacts" / "scene_plan.json"
    cp = _read_json(cp_path)
    from_cp = ((cp or {}).get("artifacts") or {}).get("scene_plan") if isinstance(cp, dict) else None
    from_cp = from_cp if isinstance(from_cp, dict) and from_cp.get("scenes") else None
    data = _read_json(art_path)
    from_art = data if isinstance(data, dict) and data.get("scenes") else None
    if from_cp is not None and from_art is not None:
        try:
            if art_path.stat().st_mtime > cp_path.stat().st_mtime:
                return from_art
        except OSError:
            pass
        return from_cp
    return from_cp if from_cp is not None else from_art


def load_manifest_rows(project_dir: Path) -> list[dict[str, Any]]:
    """asset_manifest rows: artifacts/asset_manifest.json, else the assets checkpoint."""
    man = _read_json(Path(project_dir) / "artifacts" / "asset_manifest.json")
    if not (isinstance(man, dict) and isinstance(man.get("assets"), list)):
        cp = _read_json(Path(project_dir) / "checkpoint_assets.json")
        man = ((cp or {}).get("artifacts") or {}).get("asset_manifest") if isinstance(cp, dict) else None
    rows = man.get("assets") if isinstance(man, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


def scene_media(project_dir: Path, scene_id: str, kind: str) -> Optional[Path]:
    """Newest existing generated file for a scene: kind 'image' (still) or 'video' (clip)."""
    project_dir = Path(project_dir)
    found: Optional[Path] = None
    for row in load_manifest_rows(project_dir):
        if str(row.get("scene_id")) != str(scene_id):
            continue
        typ = row.get("type")
        if kind == "image" and typ != "image":
            continue
        if kind == "video" and typ not in ("video", "animation"):
            continue
        p = Path(str(row.get("path") or ""))
        p = p if p.is_absolute() else project_dir / p
        try:
            if p.is_file() and not is_launcher_owned(p, project_dir):
                found = p
        except OSError:
            continue
    return found


def input_path(project_dir: Path, rec: dict[str, Any]) -> Path:
    return inputs_dir(project_dir) / str(rec.get("file") or f"{rec['input_id']}.png")


def is_launcher_owned(path: Path, project_dir: Path) -> bool:
    """True for anything under inputs/ or overlay/ — never a generated still or clip."""
    try:
        rel = Path(path).resolve().relative_to(Path(project_dir).resolve())
    except (ValueError, OSError):
        return False
    return bool(rel.parts) and rel.parts[0] in (INPUTS_DIR, OVERLAY_DIR)


# ---------------------------------------------------------------------------
# boxes
# ---------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


def valid_box(b: Any, *, min_size: float = 0.005) -> bool:
    if not isinstance(b, dict):
        return False
    x, y, w, h = (_num(b.get(k)) for k in ("x", "y", "w", "h"))
    if None in (x, y, w, h):
        return False
    return (x >= -EPS and y >= -EPS and w >= min_size and h >= min_size
            and x + w <= 1 + EPS and y + h <= 1 + EPS)


def valid_point(p: Any) -> bool:
    return (isinstance(p, (list, tuple)) and len(p) == 2
            and all(_num(v) is not None and -EPS <= float(v) <= 1 + EPS for v in p))


def norm_box(b: Any, fallback: Optional[dict[str, float]] = None) -> dict[str, float]:
    fb = fallback or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    if not isinstance(b, dict):
        return dict(fb)
    x = min(1.0, max(0.0, _num(b.get("x")) or 0.0))
    y = min(1.0, max(0.0, _num(b.get("y")) or 0.0))
    w = min(1.0 - x, max(0.001, _num(b.get("w")) or 0.0))
    h = min(1.0 - y, max(0.001, _num(b.get("h")) or 0.0))
    return {"x": x, "y": y, "w": w, "h": h}


def overlap_area(a: dict[str, float], b: dict[str, float]) -> float:
    ix = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy


def overlaps(a: dict[str, float], b: dict[str, float], min_area: float = 1e-4) -> bool:
    return overlap_area(a, b) > min_area


def contains_box(outer: dict[str, float], inner: dict[str, float], *, slack: float = EPS) -> bool:
    """True when ``inner`` sits entirely inside ``outer`` (fraction boxes), within slack."""
    return (
        inner["x"] >= outer["x"] - slack
        and inner["y"] >= outer["y"] - slack
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + slack
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + slack
    )


def fmt_box(b: dict[str, float]) -> str:
    return (f"x {b['x']:.2f}–{b['x'] + b['w']:.2f}, "
            f"y {b['y']:.2f}–{b['y'] + b['h']:.2f}")


# ---------------------------------------------------------------------------
# geometry (mirrors screenGeometry.ts)
# ---------------------------------------------------------------------------

def device_geometry(layout: dict[str, Any], W: int, H: int,
                    natural: dict[str, Any]) -> dict[str, Any]:
    frame = layout.get("frame") if layout.get("frame") in FRAMES else "card"
    z = norm_box(layout.get("zone"))
    zx, zy, zw, zh = z["x"] * W, z["y"] * H, z["w"] * W, z["h"] * H
    m = min(zw, zh)
    c = CHROME[frame]
    crop = norm_box(layout.get("crop"))
    nw, nh = float(natural["width"]), float(natural["height"])
    aspect = (crop["w"] * nw) / (crop["h"] * nh)
    side, top, bottom = c["side"] * m, c["top"] * m, c["bottom"] * m
    avail_w = max(1.0, zw - 2 * side)
    avail_h = max(1.0, zh - top - bottom)
    if avail_w / avail_h > aspect:
        sh = avail_h
        sw = sh * aspect
    else:
        sw = avail_w
        sh = sw / aspect
    dw, dh = sw + 2 * side, sh + top + bottom
    dx, dy = zx + (zw - dw) / 2, zy + (zh - dh) / 2
    return {
        "frame": frame,
        "device": {"x": dx, "y": dy, "w": dw, "h": dh},
        "screen": {"x": dx + side, "y": dy + top, "w": sw, "h": sh},
        "aspect": aspect,
    }


def fit_region(region: Any, aspect: float, natural: dict[str, Any]) -> dict[str, float]:
    nw, nh = float(natural["width"]), float(natural["height"])
    r = norm_box(region)
    w, h = r["w"] * nw, r["h"] * nh
    cx, cy = (r["x"] + r["w"] / 2) * nw, (r["y"] + r["h"] / 2) * nh
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    if w > nw:
        w = nw
        h = w / aspect
    if h > nh:
        h = nh
        w = h * aspect
    x = min(max(cx - w / 2, 0.0), nw - w)
    y = min(max(cy - h / 2, 0.0), nh - h)
    return {"x": x, "y": y, "w": w, "h": h}


def device_box_fraction(layout: dict[str, Any], W: int, H: int,
                        natural: dict[str, Any]) -> dict[str, float]:
    d = device_geometry(layout, W, H, natural)["device"]
    return {"x": d["x"] / W, "y": d["y"] / H, "w": d["w"] / W, "h": d["h"] / H}


# ---------------------------------------------------------------------------
# scene plan
# ---------------------------------------------------------------------------

def ratio_canvas(ratio: Optional[str]) -> Optional[tuple[int, int]]:
    """Canvas for an ``options.aspect_ratio`` value: a table entry, ``WIDTHxHEIGHT``, or any
    ``W:H`` scaled to 1080 on the short side. None when it is not a size at all.

    Mirrors ``dify_launcher/runner.py`` ``_carousel_pixel_size``, which sizes the real stills —
    a stills job passes the caller's ratio straight through (dify_launcher/CAROUSEL.md), so the
    checks and the prompt facts must measure the same frame the job actually produces.
    """
    key = str(ratio or "").strip().lower().replace(" ", "")
    if key in CANVASES:
        return CANVASES[key]
    m = re.fullmatch(r"(\d+)x(\d+)", key)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w > 0 and h > 0:
            return w, h
    m = re.fullmatch(r"(\d+):(\d+)", key)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            return ((1080, max(1, round(1080 * b / a))) if a <= b
                    else (max(1, round(1080 * a / b)), 1080))
    return None


def canvas_for(scene_plan: Optional[dict[str, Any]] = None,
               resolution: Optional[str] = None,
               pipeline: Optional[str] = None) -> tuple[int, int]:
    if resolution and "x" in str(resolution):
        try:
            w, h = (int(v) for v in str(resolution).lower().split("x", 1))
            if w > 0 and h > 0:
                return w, h
        except ValueError:
            pass
    default = DEFAULT_ASPECTS.get(str(pipeline), DEFAULT_ASPECT)
    meta = (scene_plan or {}).get("metadata") if isinstance(scene_plan, dict) else None
    ratio = str((meta or {}).get("aspect_ratio") or default).strip()
    if ratio in CANVASES:
        return CANVASES[ratio]
    if is_still_pipeline(pipeline):
        size = ratio_canvas(ratio)      # carousel / image accept WIDTHxHEIGHT and any W:H
        if size is not None:
            return size
    return CANVASES[default]


def keep_out_for(W: int, H: int, pipeline: Optional[str] = None) -> dict[str, dict[str, float]]:
    """Areas the screenshot must stay out of.

    Carousel / image stills get no caption strip (the copy is part of the generated still) and the
    whole top-right corner for the logo: the brand stamp is a fixed pixel size (LOGO_STAMP_PX), so
    it reaches further in from the corner the smaller the still is. The box therefore covers the
    stamp on the smallest still an image model returns (MIN_STILL_PIXELS) as well as on the mock
    canvases — a 1024x1024 still is a documented real output and its stamp started 15 px left of
    the old box, so a passing layout could still be covered.
    """
    if (W, H) in KEEP_OUT:
        areas = KEEP_OUT[(W, H)]
    else:
        ratio = W / H
        areas = KEEP_OUT[min(KEEP_OUT, key=lambda k: abs(k[0] / k[1] - ratio))]
    if not is_still_pipeline(pipeline):
        return areas
    logo = areas["logo"]
    w_min = math.sqrt(MIN_STILL_PIXELS * W / H)
    h_min = math.sqrt(MIN_STILL_PIXELS * H / W)
    x = max(0.0, min(logo["x"], 1.0 - LOGO_STAMP_PX[0] / w_min))
    h = min(1.0, max(logo["y"] + logo["h"], LOGO_STAMP_PX[1] / h_min))
    return {"logo": {"x": x, "y": 0.0, "w": 1.0 - x, "h": h}}


def _caption_cache_key(W: int, H: int, captions: Any, profile: str) -> str:
    zh, en = caption_text(captions)
    return f"{W}x{H}|{profile}|{zh}|{en}"


def caption_text(captions: Any) -> tuple[str, str]:
    """The zh / en caption strings panda_render would draw for a scene."""
    if not isinstance(captions, dict):
        return "", ""
    return str(captions.get("zh") or ""), str(captions.get("en") or "")


def measure_caption(W: int, H: int, captions: Any,
                    profile: str = CAPTION_PROFILE) -> Optional[dict[str, float]]:
    """The pixels this scene's caption really covers on a W×H frame, as fractions — or None.

    Drawn with the SAME renderer, profile, fonts, wrapping and frame size panda_render uses
    (``vendor/montage_svc/render/overlays.py::draw_caption``), then measured, so a caption that
    wraps to three lines reports the box it actually fills. None when the vendored renderer or
    its fonts are not available here — the caller must then fall back and say so.
    """
    zh, en = caption_text(captions)
    if not zh and not en:
        return None
    key = _caption_cache_key(W, H, captions, profile)
    if key in _CAPTION_CACHE:
        return _CAPTION_CACHE[key]
    box: Optional[dict[str, float]] = None
    try:
        root = Path(__file__).resolve().parents[1]
        vendor = root / "vendor"
        import os
        import sys

        os.environ.setdefault("MONTAGE_BRAND_DIR", str(vendor / "brand"))
        os.environ.setdefault("MONTAGE_DATA_DIR", str(vendor / "data"))
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        from PIL import Image
        from montage_svc import storage as st
        from montage_svc.render import overlays as ov

        st.ensure_profiles()
        prof = st.load_profile(profile)
        img = Image.new("RGBA", (int(W), int(H)), (0, 0, 0, 0))
        ov.draw_caption(img, prof, zh or None, en or None)
        bbox = img.getbbox()
        if bbox:
            box = {"x": bbox[0] / W, "y": bbox[1] / H,
                   "w": (bbox[2] - bbox[0]) / W, "h": (bbox[3] - bbox[1]) / H}
    except Exception:  # noqa: BLE001 — measuring is an extra; the caller falls back and says so
        box = None
    _CAPTION_CACHE[key] = box
    return box


def caption_keep_out(W: int, H: int, captions: Any = None,
                     pipeline: Optional[str] = None) -> tuple[Optional[dict[str, float]], bool]:
    """(area the caption covers, measured?) for one scene. Stills pipelines get (None, True).

    The KEEP_OUT table is one zh + one en line — the caption grows UPWARD as it wraps, so the
    table alone accepts a screenshot that a wrapped bilingual caption then covers. With the real
    caption text this measures it; without it (or with the renderer unavailable) the table box is
    grown upward by CAPTION_FALLBACK_LINES lines, which is the documented conservative fallback.
    """
    if is_still_pipeline(pipeline):
        return None, True                     # a still draws its own copy: no caption strip
    table = keep_out_for(W, H, pipeline).get("captions")
    if table is None:
        return None, True
    if caption_text(captions) == ("", ""):
        # The plan does not say what this scene's caption is (it is optional for video), so there
        # is nothing to measure: keep the one-line strip the table records and do not guess wider.
        return table, True
    real = measure_caption(W, H, captions)
    if real is not None:
        y = min(table["y"], real["y"])
        bottom = max(table["y"] + table["h"], real["y"] + real["h"])
        x = min(table["x"], real["x"])
        right = max(table["x"] + table["w"], real["x"] + real["w"])
        return {"x": x, "y": y, "w": min(1.0, right - x), "h": min(1.0 - y, bottom - y)}, True
    grown = table["h"] * CAPTION_FALLBACK_LINES
    y = max(0.0, table["y"] + table["h"] - grown)
    return {"x": table["x"], "y": y, "w": table["w"],
            "h": min(1.0 - y, table["y"] + table["h"] - y)}, False


def scene_duration(scene: dict[str, Any]) -> float:
    s, e = _num(scene.get("start_seconds")), _num(scene.get("end_seconds"))
    if s is None or e is None or e <= s:
        return 5.0
    return round(e - s, 3)


def screenshot_items(scene_plan: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every screenshot placement in the plan, in scene order (scene numbers are 1-based)."""
    out: list[dict[str, Any]] = []
    scenes = (scene_plan or {}).get("scenes") if isinstance(scene_plan, dict) else None
    for n, scene in enumerate(scenes or [], 1):
        if not isinstance(scene, dict):
            continue
        for j, ra in enumerate(scene.get("required_assets") or []):
            if not (isinstance(ra, dict) and ra.get("source") == "provided" and ra.get("input_id")):
                continue
            out.append({
                "scene_number": n,
                "scene_id": str(scene.get("id") or f"scene-{n}"),
                "item_index": j,
                "input_id": str(ra.get("input_id")),
                "description": str(ra.get("description") or ""),
                "layout": ra.get("layout") if isinstance(ra.get("layout"), dict) else {},
                "duration": scene_duration(scene),
                # the scene's own caption text, so the caption area can be measured as rendered
                "captions": scene.get("captions") if isinstance(scene.get("captions"), dict) else None,
            })
    return out


def items_by_scene(items: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        grouped.setdefault(it["scene_id"], []).append(it)
    return grouped


# ---------------------------------------------------------------------------
# timing (video only — a carousel / image still shows everything at once)
# ---------------------------------------------------------------------------

def show_window(layout: dict[str, Any], duration: float,
                still: bool = False) -> tuple[float, float]:
    """The window the layout ASKS to be on screen for, as written — never clamped.

    Unclamped on purpose: a window that falls outside the scene it is composed at is the defect
    timing_errors() reports, and clamping it here would hide it.
    """
    if still:
        return 0.0, float(duration)
    show = layout.get("show") if isinstance(layout.get("show"), dict) else {}
    frm = _num(show.get("from_s"))
    to = _num(show.get("to_s"))
    return (0.0 if frm is None else float(frm), float(duration) if to is None else float(to))


def settled_span(layout: dict[str, Any], duration: float,
                 window: Optional[tuple[float, float]] = None) -> tuple[float, float]:
    """Scene-local span in which this layer is on screen AND every move has finished.

    Enter / zoom / cursor / click / card animations are excluded at the front, the exit (explicit,
    or the implicit fade when the window closes before the scene does) at the back. This is the
    span a checker may sample and the moment a preview should show.
    """
    frm, to = window or show_window(layout, duration)
    frm = max(0.0, min(frm, float(duration)))
    to = max(frm, min(to, float(duration)))
    enter = layout.get("enter") if isinstance(layout.get("enter"), dict) else {}
    done = max(frm, float(_num(enter.get("at_s")) or frm)
               + float(_num(enter.get("duration_s")) or 0.35))
    for st in layout.get("steps") or []:
        if not isinstance(st, dict) or _num(st.get("at_s")) is None:
            continue
        at = float(_num(st.get("at_s")) or 0.0)
        kind = st.get("kind")
        if kind in ("zoom_to", "cursor_move"):
            done = max(done, at + float(_num(st.get("duration_s")) or 0.8))
        elif kind == "click_pulse":
            done = max(done, at + float(_num(st.get("duration_s")) or 0.5))
        elif kind in ("highlight_box", "card"):
            done = max(done, at + 0.3)
    end = to
    exit_ = layout.get("exit") if isinstance(layout.get("exit"), dict) else {}
    if exit_:
        end = min(end, float(_num(exit_.get("at_s")) or (to - float(
            _num(exit_.get("duration_s")) or 0.3))))
    elif to < float(duration) - 1e-3:
        end = max(frm, to - 0.3)            # ScreenLayer fades a window that closes early
    start = min(done + 0.15, end)
    return max(frm, min(start, end)), max(frm, end)


def sample_times(layout: dict[str, Any], duration: float,
                 window: Optional[tuple[float, float]] = None,
                 count: int = 3, margin: float = 0.0) -> list[float]:
    """Up to `count` scene-local moments inside the settled span, `margin` s clear of its edges."""
    a, b = settled_span(layout, duration, window)
    if margin > 0 and b - a > 2 * margin + 0.1:
        a, b = a + margin, b - margin
    if b - a < 0.08 or count <= 1:
        return [round(max(0.0, min((a + b) / 2, float(duration))), 3)]
    return [round(a + (b - a) * f, 3) for f in (0.15, 0.5, 0.85)][:count]


def display_windows(items: list[dict[str, Any]],
                    duration: float) -> list[tuple[float, float, list[dict[str, Any]]]]:
    """One entry per DISTINCT set of screenshots on screen together, in scene order.

    Two screenshots shown at the same time stay in one window (an intentionally simultaneous
    layout); one shown after another gives two, so a preview can show each of them.
    """
    spans = []
    for it in items:
        frm, to = show_window(it.get("layout") or {}, it.get("duration", duration))
        frm, to = max(0.0, min(frm, duration)), max(0.0, min(to, duration))
        if to - frm > 1e-3:
            spans.append((frm, to, it))
    if not spans:
        return []
    edges = sorted({v for frm, to, _ in spans for v in (frm, to)})
    out: list[tuple[float, float, list[dict[str, Any]]]] = []
    for a, b in zip(edges, edges[1:]):
        if b - a <= 1e-3:
            continue
        mid = (a + b) / 2
        live = [it for frm, to, it in spans if frm <= mid <= to]
        if not live:
            continue
        if out and out[-1][2] == live:                 # same set: one window
            out[-1] = (out[-1][0], b, live)
        else:
            out.append((a, b, live))
    return out


def preview_time(items: list[dict[str, Any]], duration: float,
                 window: Optional[tuple[float, float]] = None) -> float:
    """A scene-local moment at which every item in `items` is on screen and settled."""
    starts, ends = [], []
    for it in items:
        a, b = settled_span(it.get("layout") or {}, it.get("duration", duration), window)
        starts.append(a)
        ends.append(b)
    lo, hi = (max(starts) if starts else 0.0), (min(ends) if ends else float(duration))
    return round(lo if lo <= hi else (min(starts) if starts else 0.0), 3)


def timing_errors(items: Iterable[dict[str, Any]], duration: float,
                  inputs: Iterable[dict[str, Any]] = (),
                  pipeline: Optional[str] = None) -> list[str]:
    """Placements whose timing does not fit a scene `duration` seconds long.

    Called with the duration the scene is actually COMPOSED at, which is the edit's cut and not
    necessarily the one the scene plan wrote: shortening a scene to 3 s makes a screenshot placed
    at 4–5 s invisible. Naming the scene, the screenshot and the timing, so the caller can refuse
    the render instead of quietly moving what the user asked for.
    """
    if is_still_pipeline(pipeline):
        return []                            # a still has no timing
    numbers = {r.get("input_id"): r.get("n") for r in inputs}
    out: list[str] = []
    for it in items:
        layout = it.get("layout") or {}
        n = numbers.get(it.get("input_id"))
        who = f"screenshot {n}" if n is not None else f"screenshot {it.get('input_id')}"
        label = f"{unit_label(pipeline, it.get('scene_number') or 0)} ({who})"
        frm, to = show_window(layout, it.get("duration", duration))
        # A layout with no show window asks for "the whole scene", so a shorter cut simply makes
        # it shorter — only timing the plan WROTE can be violated by the cut.
        show = layout.get("show") if isinstance(layout.get("show"), dict) else {}
        if _num(show.get("to_s")) is None:
            to = min(to, duration)
        if to <= frm + 1e-3:
            out.append(f"{label}: its show window {frm:g}–{to:g} s is empty")
            continue
        if frm >= duration - 1e-3:
            out.append(f"{label}: it is placed at {frm:g}–{to:g} s but the scene is composed at "
                       f"{duration:g} s, so it would never appear — move the placement into "
                       f"0–{duration:g} s or keep the scene long enough for it")
            continue
        if to > duration + 0.05:
            out.append(f"{label}: it is placed until {to:g} s but the scene is composed at "
                       f"{duration:g} s, so {to - duration:g} s of it would be cut — shorten the "
                       f"show window to 0–{duration:g} s or keep the scene long enough for it")
        enter = layout.get("enter") if isinstance(layout.get("enter"), dict) else {}
        at = _num(enter.get("at_s"))
        if at is not None and at + float(_num(enter.get("duration_s")) or 0.35) > duration + 0.05:
            out.append(f"{label}: its entrance ends at "
                       f"{at + float(_num(enter.get('duration_s')) or 0.35):g} s, after the "
                       f"{duration:g} s the scene is composed at")
        for i, st in enumerate(layout.get("steps") or []):
            if not isinstance(st, dict):
                continue
            s_at = _num(st.get("at_s"))
            if s_at is None:
                continue
            ends = s_at + float(_num(st.get("duration_s")) or 0.0)
            if s_at < -EPS or ends > duration + 0.05:
                out.append(f"{label}: step {i + 1} ({st.get('kind')}) runs {s_at:g}–{ends:g} s, "
                           f"outside the {duration:g} s the scene is composed at")
    return out


def layout_hash(items: Iterable[dict[str, Any]], inputs: Iterable[dict[str, Any]] = ()) -> str:
    """Stable hash of a scene's placements (+ the screenshots' content hashes)."""
    by_id = {r.get("input_id"): r.get("sha256") for r in inputs}
    payload = [{"input_id": it["input_id"], "sha": by_id.get(it["input_id"]),
                "layout": it.get("layout"), "duration": it.get("duration")} for it in items]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def schema_errors(instance: Any, name: str, limit: int = 3) -> list[str]:
    """Short JSON-schema error strings (schemas/artifacts/<name>.schema.json)."""
    try:
        import jsonschema

        schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
    except Exception:  # noqa: BLE001 — schema checks are an extra; code rules still run
        return []
    out: list[str] = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(top)"
        out.append(f"{where}: {err.message[:140]}")
        if len(out) >= limit:
            break
    return out


def request_scenes(req: dict[str, Any]) -> list[int]:
    raw = req.get("scenes")
    if raw is None and req.get("scene") is not None:
        raw = [req.get("scene")]
    out: list[int] = []
    for v in raw or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 1 and n not in out:
            out.append(n)
    return out


def is_unplaced(req: dict[str, Any]) -> bool:
    return not request_scenes(req) and not str(req.get("moment") or "").strip()


def validate_requests(inputs: list[dict[str, Any]],
                      requests: Optional[list[dict[str, Any]]],
                      pipeline: Optional[str] = None) -> list[str]:
    units = {"panda-carousel": "slides", "panda-image": "the image"}.get(str(pipeline), "scenes")
    if requests is None:
        return [f"requests.json is missing — the screenshots have not been matched to {units} yet"]
    notes: list[str] = [f"requests.json {e}" for e in schema_errors(requests, "screen_requests")]
    known = {r["input_id"]: r for r in inputs}
    seen: dict[str, int] = {}
    for req in requests:
        iid = str(req.get("input_id") or "")
        if iid not in known:
            notes.append(f"requests.json names an unknown screenshot id {iid!r}")
            continue
        seen[iid] = seen.get(iid, 0) + 1
        raw = req.get("scenes")
        if raw not in (None, []) and not request_scenes(req):
            if pipeline == "panda-image":
                notes.append(f"screenshot {known[iid].get('n')}: use 1 (the image) or leave it "
                             "unplaced")
            else:
                notes.append(f"screenshot {known[iid].get('n')}: {unit_word(pipeline)} numbers "
                             "must be 1 or higher")
    for iid, count in seen.items():
        if count > 1:
            notes.append(f"screenshot {known[iid].get('n')} is listed {count} times in requests.json")
    for rec in inputs:
        if rec["input_id"] not in seen:
            notes.append(f"screenshot {rec.get('n')} ({rec.get('name')}) is missing from requests.json")
    return notes


def _check_steps(label: str, layout: dict[str, Any], duration: float,
                 geo: dict[str, Any], natural: dict[str, Any], W: int, H: int,
                 keep_out: dict[str, dict[str, float]],
                 subject: Optional[dict[str, float]], still: bool = False,
                 pipeline: Optional[str] = None) -> list[str]:
    notes: list[str] = []
    crop = fit_region(norm_box(layout.get("crop")), geo["aspect"], natural)
    for i, st in enumerate(layout.get("steps") or []):
        if not isinstance(st, dict) or st.get("kind") not in STEP_KINDS:
            notes.append(f"{label}: step {i + 1} has an unknown kind {st.get('kind') if isinstance(st, dict) else st!r}")
            continue
        kind = st["kind"]
        where = f"{label}: {kind}"
        if kind in ("blur_region", "highlight_box", "zoom_to") and not valid_box(st.get("region")):
            notes.append(f"{where} needs a region inside the screenshot (fractions 0–1)")
            continue
        if kind == "cursor_move" and not valid_point(st.get("to")):
            notes.append(f"{where} needs a 'to' point inside the screenshot")
            continue
        if kind == "click_pulse" and st.get("at") is not None and not valid_point(st.get("at")):
            notes.append(f"{where} 'at' point is outside the screenshot")
            continue
        if kind == "card":
            if not valid_box(st.get("zone")):
                notes.append(f"{where} needs a zone inside the frame")
                continue
            if not st.get("text"):
                notes.append(f"{where} has no text")
            cz = norm_box(st.get("zone"))
            if "captions" in keep_out and overlaps(cz, keep_out["captions"]):
                notes.append(f"{where} overlaps the caption strip ({fmt_box(keep_out['captions'])})")
            if subject and overlaps(cz, subject):
                notes.append(f"{where} covers the character area")
            if overlaps(cz, keep_out["logo"]):
                notes.append(f"{where} sits in the corner where the Panda logo goes if the "
                             f"{deliverable_word(pipeline)} is branded — the stamp would cover it "
                             f"({fmt_box(keep_out['logo'])})")
            txt = st.get("text")
            zh = str((txt or {}).get("zh") or "") if isinstance(txt, dict) else str(txt or "")
            en = str((txt or {}).get("en") or "") if isinstance(txt, dict) else ""
            if len(zh) > 12 or len(en.split()) > 5:
                notes.append(f"{where} text is too long — the card is one line that does not wrap, "
                             "so anything past ~12 CJK characters or 5 English words is cut off")
        at = _num(st.get("at_s"))
        dur = _num(st.get("duration_s")) or 0.0
        if not still and at is not None and (at < -EPS or at + dur > duration + 0.05):
            notes.append(f"{where} runs outside the scene (0–{duration:g} s)")
        if kind == "zoom_to":
            target = fit_region(st["region"], geo["aspect"], natural)
            if target["w"] * target["h"] >= 0.9 * crop["w"] * crop["h"]:
                notes.append(f"{where} barely zooms — the region grows to the screen's shape; "
                             "pick a narrower region")
            scale = geo["screen"]["w"] / max(1.0, target["w"])
            if scale > LEGIBLE_MAX_SCALE:
                notes.append(f"{where} enlarges the screenshot {scale:.1f}× — it will look soft")
    return notes


def validate_layouts(scene_plan: Optional[dict[str, Any]], inputs: list[dict[str, Any]],
                     requests: Optional[list[dict[str, Any]]],
                     canvas: Optional[tuple[int, int]] = None,
                     pipeline: Optional[str] = None) -> list[str]:
    """Plain-language problems with the plan's screenshot placements (empty = all good).

    Carousel / image stills have no timing and no caption strip: layouts are checked in their
    settled state, and any two screenshots that overlap on the same slide are a problem."""
    notes: list[str] = []
    pipeline = pipeline or VIDEO_PIPELINE
    still = is_still_pipeline(pipeline)
    word = unit_word(pipeline)
    unit = lambda s: unit_label(pipeline, s)  # noqa: E731
    scenes = (scene_plan or {}).get("scenes") or [] if isinstance(scene_plan, dict) else []
    W, H = canvas or canvas_for(scene_plan, pipeline=pipeline)
    base_keep_out = keep_out_for(W, H, pipeline)
    keep_out = base_keep_out
    known = {r["input_id"]: r for r in inputs}
    items = screenshot_items(scene_plan)
    number = {iid: known[iid].get("n") for iid in known}

    # --- the user's assignments are binding ---------------------------------
    if requests is not None:
        by_scene: dict[int, set[str]] = {}
        for it in items:
            by_scene.setdefault(it["scene_number"], set()).add(it["input_id"])
        placed_anywhere = {it["input_id"] for it in items}
        for req in requests:
            iid = str(req.get("input_id") or "")
            if iid not in known:
                continue
            n = number.get(iid)
            wanted = request_scenes(req)
            if wanted:
                for s in wanted:
                    if s > len(scenes):
                        if pipeline == "panda-image":
                            notes.append(f"screenshot {n} is assigned to image {s}, but this job "
                                         "makes one image")
                        else:
                            notes.append(f"screenshot {n} is assigned to {word} {s}, but the plan "
                                         f"has only {len(scenes)} {word}s")
                    elif iid not in by_scene.get(s, set()):
                        notes.append(f"{unit(s)} is missing screenshot {n}, which the user put there")
                for s, ids in sorted(by_scene.items()):
                    if iid in ids and s not in wanted:
                        notes.append(f"{unit(s)} shows screenshot {n}, but the user put it in "
                                     f"{word} {', '.join(str(x) for x in wanted)}")
            elif is_unplaced(req):
                if iid in placed_anywhere:
                    notes.append(f"screenshot {n} has no {word} from the user but appears in the plan")
            elif iid not in placed_anywhere:
                notes.append(f"screenshot {n} ({str(req.get('moment'))[:60]}) is not used in any {word}")

    # --- each placement ------------------------------------------------------
    grouped = items_by_scene(items)
    estimated = False
    for scene_id, group in grouped.items():
        shown: list[tuple[dict[str, float], float, float, str]] = []
        # This scene's caption as panda_render will really draw it: a wrapped bilingual caption
        # reaches above the one-line strip in the table (keep_out), so measure it per scene.
        cap, measured = caption_keep_out(W, H, group[0].get("captions"), pipeline)
        keep_out = {k: v for k, v in base_keep_out.items() if k != "captions"}
        if cap is not None:
            keep_out["captions"] = cap
            estimated = estimated or not measured
        for it in group:
            rec = known.get(it["input_id"])
            label = unit(it["scene_number"])
            if rec is None:
                notes.append(f"{label} uses an unknown screenshot id {it['input_id']!r}")
                continue
            label = f"{label} (screenshot {rec.get('n')})"
            layout = it["layout"]
            if not layout:
                notes.append(f"{label} has no layout")
                continue
            for err in schema_errors(layout, "screen_layout"):
                notes.append(f"{label}: layout {err}")
            if not valid_box(layout.get("zone"), min_size=0.05):
                notes.append(f"{label}: zone must be a box inside the frame (fractions 0–1)")
                continue
            frame = layout.get("frame")
            if frame not in FRAMES:
                notes.append(f"{label}: frame must be one of {', '.join(FRAMES)}")
            held = frame == "held"
            if layout.get("crop") is not None and not valid_box(layout.get("crop")):
                notes.append(f"{label}: crop must be a box inside the screenshot")
            for key in ("enter", "exit"):
                mv = layout.get(key)
                if isinstance(mv, dict) and mv.get("type") not in (None, *MOTIONS):
                    notes.append(f"{label}: {key} type must be one of {', '.join(MOTIONS)}")
                if (held and isinstance(mv, dict)
                        and mv.get("type") in HELD_FORBIDDEN_MOTIONS):
                    notes.append(f"{label}: held-phone overlays stay on a fixed screen rect — "
                                 f"do not use {key} type {mv.get('type')!r} (omit or use fade/pop)")
            if held and not still and layout.get("camera") != "locked":
                notes.append(f"{label}: held-phone needs camera: \"locked\" so the blank screen "
                             "stays in a fixed place for the overlay")
            natural = {"width": rec.get("width") or 1, "height": rec.get("height") or 1}
            geo = device_geometry(layout, W, H, natural)
            # For held, zone IS the blank phone screen (no chrome) — compare zone to subject.
            zone = norm_box(layout.get("zone"))
            dev = zone if held else device_box_fraction(layout, W, H, natural)
            subject = (norm_box(layout.get("subject_zone"))
                       if valid_box(layout.get("subject_zone")) else None)
            if layout.get("subject_zone") is not None and subject is None:
                notes.append(f"{label}: subject_zone must be a box inside the frame")
            if held:
                if subject is None:
                    notes.append(f"{label}: held-phone needs a subject_zone covering the "
                                 "character and the phone body")
                elif not contains_box(subject, zone):
                    notes.append(f"{label}: held-phone zone (blank screen) must sit inside "
                                 f"subject_zone ({fmt_box(subject)})")
            elif subject and overlaps(dev, subject):
                notes.append(f"{label}: the screenshot covers the character area "
                             f"({fmt_box(subject)})")
            if "captions" in keep_out and overlaps(dev, keep_out["captions"]):
                notes.append(f"{label}: the screenshot overlaps the caption strip "
                             f"({fmt_box(keep_out['captions'])}"
                             + (f", this {word}'s caption as it wraps" if measured
                                and caption_text(it.get('captions')) != ("", "") else "")
                             + ")")
            if overlaps(dev, keep_out["logo"]):
                notes.append(f"{label}: the screenshot reaches the corner where the Panda logo "
                             f"goes if the {deliverable_word(pipeline)} is branded "
                             f"({fmt_box(keep_out['logo'])})")
            crop_px = fit_region(norm_box(layout.get("crop")), geo["aspect"], natural)
            scale = geo["screen"]["w"] / max(1.0, crop_px["w"])
            if scale < LEGIBLE_MIN_SCALE:
                notes.append(f"{label}: shown at {scale:.2f}× — text will be too small; make it "
                             "bigger, crop to the part that matters, or zoom in")
            elif scale > LEGIBLE_MAX_SCALE:
                notes.append(f"{label}: enlarged {scale:.1f}× — it will look soft")
            if still:
                s_from, s_to = 0.0, 1.0      # a still has no timing: everything shows at once
            else:
                show = layout.get("show") if isinstance(layout.get("show"), dict) else {}
                s_from = _num(show.get("from_s")) or 0.0
                s_to = _num(show.get("to_s"))
                s_to = it["duration"] if s_to is None else s_to
                if s_from < -EPS or s_to <= s_from or s_to > it["duration"] + 0.05:
                    notes.append(f"{label}: show window must fit the scene (0–{it['duration']:g} s)")
            notes += _check_steps(label, layout, it["duration"], geo, natural, W, H,
                                  keep_out, subject, still, pipeline)
            for other_dev, o_from, o_to, other_label in shown:
                if overlaps(dev, other_dev) and s_from < o_to - EPS and o_from < s_to - EPS:
                    notes.append(f"{label} and {other_label} overlap on screen"
                                 + ("" if still else " at the same time"))
            shown.append((dev, s_from, s_to, label))
    if estimated:
        notes.append("the caption area could not be measured with the brand renderer here, so a "
                     f"conservative estimate ({CAPTION_FALLBACK_LINES:g} lines) was used — check "
                     "the preview by eye before approving")
    return notes


def caption_notes(scene_plan: Optional[dict[str, Any]], inputs: list[dict[str, Any]],
                  canvas: Optional[tuple[int, int]] = None,
                  pipeline: Optional[str] = None) -> list[str]:
    """Only the caption-clearance part of validate_layouts, for the gates after the plan.

    The captions and the output size can both change after the plan was approved (an edit rewrites
    a caption, a job is re-rendered at another size), and a longer caption covers more of the
    frame — so the clearance is measured again wherever the current plan is at hand.
    """
    if is_still_pipeline(pipeline):
        return []
    W, H = canvas or canvas_for(scene_plan, pipeline=pipeline)
    known = {r["input_id"]: r for r in inputs}
    notes: list[str] = []
    estimated = False
    for _scene_id, group in items_by_scene(screenshot_items(scene_plan)).items():
        cap, measured = caption_keep_out(W, H, group[0].get("captions"), pipeline)
        if cap is None:
            continue
        estimated = estimated or not measured
        for it in group:
            rec = known.get(it["input_id"])
            layout = it.get("layout") or {}
            if rec is None or not valid_box(layout.get("zone"), min_size=0.05):
                continue
            natural = {"width": rec.get("width") or 1, "height": rec.get("height") or 1}
            dev = device_box_fraction(layout, W, H, natural)
            if overlaps(dev, cap):
                notes.append(
                    f"{unit_label(pipeline, it['scene_number'])} (screenshot {rec.get('n')}): the "
                    f"caption covers part of the screenshot ({fmt_box(cap)}) — shorten this "
                    f"{unit_word(pipeline)}'s caption or move the screenshot up")
    if notes and estimated:
        notes.append("the caption area could not be measured with the brand renderer here, so a "
                     f"conservative estimate ({CAPTION_FALLBACK_LINES:g} lines) was used — check "
                     "the preview by eye before approving")
    return notes


def keep_clear_lines(scene_plan: Optional[dict[str, Any]],
                     canvas: Optional[tuple[int, int]] = None,
                     pipeline: Optional[str] = None) -> list[str]:
    """Per screenshot scene: which areas the still/clip must leave plain (for prompts)."""
    still = is_still_pipeline(pipeline)
    lines: list[str] = []
    for scene_id, group in items_by_scene(screenshot_items(scene_plan)).items():
        zones = [norm_box(it["layout"].get("zone")) for it in group if valid_box(it["layout"].get("zone"))]
        subjects = [norm_box(it["layout"].get("subject_zone")) for it in group
                    if valid_box(it["layout"].get("subject_zone"))]
        if not zones:
            continue
        held = any((it.get("layout") or {}).get("frame") == "held" for it in group)
        if held:
            parts = [f"{unit_label(pipeline, group[0]['scene_number'])} ({scene_id}): keep "
                     + "; ".join(fmt_box(z) for z in zones)
                     + " plain white blank phone screen (no UI glyphs, no props, no text) — "
                     "the real screenshot is composited there after generation"]
            if subjects:
                parts.append("character + phone body inside "
                             + "; ".join(fmt_box(s) for s in subjects))
        else:
            parts = [f"{unit_label(pipeline, group[0]['scene_number'])} ({scene_id}): keep "
                     + "; ".join(fmt_box(z) for z in zones)
                     + f" plain white — no character, props or {baked_text_words(pipeline)[0]} there"]
            if subjects:
                parts.append("character inside " + "; ".join(fmt_box(s) for s in subjects))
        if not still:
            parts.append("camera locked")
        lines.append(", ".join(parts))
    return lines


def layer_props(item: dict[str, Any], rec: dict[str, Any], src_name: str) -> dict[str, Any]:
    return {
        "src": src_name,
        "natural": {"width": int(rec.get("width") or 1), "height": int(rec.get("height") or 1)},
        "layout": item["layout"],
    }
