"""User screenshots — the launcher side.

Only active for jobs whose POST /jobs carried ``options.media`` (panda-video, panda-carousel,
panda-image). Everything here is a no-op for jobs without uploads, so their prompts, gates and
renders are unchanged.

  intake           download the Dify file links (allow-listed hosts only), check they are real
                   images, normalise them (EXIF rotation, metadata stripped, sRGB PNG) and stage
                   them under projects/<job>/inputs/ BEFORE the job exists
  facts            the USER SCREENSHOTS block appended to every agent leg's prompt
  place_on_stills  carousel / image: bake the screenshots into the job-store copy of each
                   generated still (the clean still under assets/images is never touched)
  apply_gate       per-gate checks (plain-language notes for the gate question) + preview boards

Shared layout rules live in lib/screen_layout.py; rendering lives in tools/video/screen_overlay.py.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from lib import screen_layout as sl

MAX_FILES = int(os.environ.get("SCREENSHOT_MAX_FILES", "20"))
MAX_MB = float(os.environ.get("SCREENSHOT_MAX_MB", "10"))
TOTAL_MB = float(os.environ.get("SCREENSHOT_TOTAL_MB", "150"))
FETCH_BUDGET_S = float(os.environ.get("SCREENSHOT_FETCH_BUDGET_S", "40"))
MAX_PIXELS = int(os.environ.get("SCREENSHOT_MAX_PIXELS", "40000000"))
MAX_EDGE = int(os.environ.get("SCREENSHOT_MAX_EDGE", "4096"))
ACCEPTED_FORMATS = ("PNG", "JPEG", "WEBP")

# A still/clip region counts as "not clear" when more than this share of it is busy.
CLEAR_MAX_BUSY = float(os.environ.get("SCREENSHOT_CLEAR_MAX_BUSY", "0.04"))
# Final-video check: mean absolute difference (0-255) below which a frame "matches".
FINAL_MATCH_MAX_DIFF = float(os.environ.get("SCREENSHOT_FINAL_MATCH_MAX_DIFF", "14"))
# Carousel / image: time allowed for STARTING still renders in one pass (renders are cached, so
# each still is normally rendered once, ~7 s). Checked before each render, so a pass can run this
# long plus the one render already under way (SCREEN_OVERLAY_BOARD_TIMEOUT_S at worst). Stills
# left over are placed by the next pass, and the pass that opens the brand gate is not time-boxed
# at all, so nothing ships unplaced without a note.
STILLS_BUDGET_S = float(os.environ.get("SCREENSHOT_STILLS_BUDGET_S", "300"))
STILL_RENDER_VERSION = "1"

# One placement pass per job at a time: two syncs of the same job would otherwise render the same
# slide into the same work dir and record a phantom failure against the retry cap.
_PLACE_LOCKS: dict[str, threading.Lock] = {}
_PLACE_LOCKS_GUARD = threading.Lock()

BOARD_FILES = {"uploads": "screens_uploads.png", "layouts": "screens_layouts.png",
               "stills": "screens_stills.png", "clips": "screens_clips.png"}


class IntakeError(ValueError):
    """A problem with the uploaded media — surfaced to Dify as HTTP 400."""


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------

def media_items(options: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    """The validated ``options.media`` list ([] when absent)."""
    raw = (options or {}).get("media")
    if raw in (None, [], ""):
        return []
    if not isinstance(raw, list):
        raise IntakeError("options.media must be a list of {url, name}")
    if len(raw) > MAX_FILES:
        raise IntakeError(f"too many images: {len(raw)} (this server accepts up to {MAX_FILES})")
    items: list[dict[str, str]] = []
    for i, it in enumerate(raw, 1):
        if isinstance(it, str):
            it = {"url": it}
        if not isinstance(it, dict) or not isinstance(it.get("url"), str) or not it["url"].strip():
            raise IntakeError(f"image {i}: each options.media item needs a url")
        name = str(it.get("name") or Path(urlparse(it["url"]).path).name or f"image-{i}")
        name = "".join(ch for ch in name if ch.isprintable() and ch not in '\\/<>:"|?*')[:80]
        items.append({"url": it["url"].strip(), "name": name or f"image-{i}"})
    return items


def _allowed_hosts() -> set[str]:
    hosts = {h.strip().lower() for h in os.environ.get("DIFY_FILES_HOSTS", "").split(",") if h.strip()}
    base = os.environ.get("DIFY_FILES_BASE", "").strip()
    if base:
        host = urlparse(base).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def resolve_url(url: str) -> str:
    """Absolute, allow-listed URL for one Dify file link (relative /files/... links get the base)."""
    hosts = _allowed_hosts()
    if not hosts:
        raise IntakeError("image uploads are not enabled on this server (DIFY_FILES_HOSTS is not set)")
    if url.startswith("/"):
        base = os.environ.get("DIFY_FILES_BASE", "").strip().rstrip("/")
        if not base:
            raise IntakeError("got a relative file link but DIFY_FILES_BASE is not set")
        url = base + url
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise IntakeError("image links must be http(s)")
    if (parsed.hostname or "").lower() not in hosts:
        raise IntakeError(f"image link host {parsed.hostname!r} is not an allowed Dify file host")
    return url


def fetch(url: str, deadline: float) -> bytes:
    import requests

    limit = int(MAX_MB * 1024 * 1024)
    remaining = max(1.0, deadline - time.monotonic())
    try:
        with requests.get(url, stream=True, allow_redirects=False,
                          timeout=(min(10.0, remaining), min(30.0, remaining))) as r:
            if 300 <= r.status_code < 400:
                raise IntakeError("the image link redirected — redirects are not followed")
            if r.status_code != 200:
                raise IntakeError(f"the image link returned HTTP {r.status_code} "
                                  "(Dify file links expire after a few minutes)")
            buf = io.BytesIO()
            for chunk in r.iter_content(64 * 1024):
                buf.write(chunk)
                if buf.tell() > limit:
                    raise IntakeError(f"an image is larger than {MAX_MB:g} MB")
                if time.monotonic() > deadline:
                    raise IntakeError("downloading the images took too long")
            return buf.getvalue()
    except IntakeError:
        raise
    except Exception as e:  # noqa: BLE001 — network errors become a clear 400
        raise IntakeError(f"could not download an image: {type(e).__name__}") from e


def normalise(data: bytes) -> tuple[bytes, int, int]:
    """Real-image check + EXIF rotation + metadata stripped + sRGB PNG. Returns (png, w, h)."""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            if probe.width * probe.height > MAX_PIXELS:
                raise IntakeError("an image has too many pixels")
            probe.verify()
    except IntakeError:
        raise
    except Image.DecompressionBombError as e:
        raise IntakeError("an image has too many pixels") from e
    except Exception as e:  # noqa: BLE001
        raise IntakeError("a file is not a readable image") from e
    if fmt not in ACCEPTED_FORMATS:
        raise IntakeError(f"unsupported image type {fmt or 'unknown'} (use PNG, JPEG or WebP)")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            if im.width * im.height > MAX_PIXELS:
                raise IntakeError("an image has too many pixels")
            im = ImageOps.exif_transpose(im)
            icc = im.info.get("icc_profile")
            if icc:
                try:
                    from PIL import ImageCms

                    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                    dst = ImageCms.createProfile("sRGB")
                    mode = "RGBA" if "A" in im.getbands() else "RGB"
                    im = ImageCms.profileToProfile(im.convert(mode), src, dst, outputMode=mode)
                except Exception:  # noqa: BLE001 — a broken profile falls back to plain RGB
                    pass
            mode = "RGBA" if ("A" in im.getbands() or "transparency" in im.info) else "RGB"
            im = im.convert(mode)
            if max(im.size) > MAX_EDGE:
                im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
            out = io.BytesIO()
            clean = Image.new(mode, im.size)
            clean.paste(im)
            clean.save(out, format="PNG", optimize=True)
            return out.getvalue(), clean.width, clean.height
    except IntakeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise IntakeError("a file could not be read as an image") from e


def prepare(options: Optional[dict[str, Any]]) -> Optional[tuple[Path, list[dict[str, Any]]]]:
    """Download + normalise every upload into a temp dir. None when there is no media."""
    items = media_items(options)
    if not items:
        return None
    urls = [resolve_url(it["url"]) for it in items]
    deadline = time.monotonic() + FETCH_BUDGET_S
    with ThreadPoolExecutor(max_workers=min(6, len(urls))) as pool:
        blobs = list(pool.map(lambda u: fetch(u, deadline), urls))
    if sum(len(b) for b in blobs) > TOTAL_MB * 1024 * 1024:
        raise IntakeError(f"the images add up to more than {TOTAL_MB:g} MB")
    tmp = Path(tempfile.mkdtemp(prefix="panda_inputs_"))
    try:
        records: list[dict[str, Any]] = []
        for n, (it, blob) in enumerate(zip(items, blobs), 1):
            png, w, h = normalise(blob)
            iid = f"in_{n:02d}"
            (tmp / f"{iid}.png").write_bytes(png)
            records.append({"n": n, "input_id": iid, "name": it["name"], "file": f"{iid}.png",
                            "width": w, "height": h,
                            "orientation": "portrait" if h > w else ("landscape" if w > h else "square"),
                            "sha256": hashlib.sha256(png).hexdigest()})
        return tmp, records
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def commit(prepared: tuple[Path, list[dict[str, Any]]], project_dir: Path,
           pipeline: str = sl.VIDEO_PIPELINE, language: str = "en") -> list[dict[str, Any]]:
    tmp, records = prepared
    dest = sl.inputs_dir(project_dir)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for rec in records:
            os.replace(tmp / rec["file"], dest / rec["file"])
        (dest / sl.JOB_FILE).write_text(json.dumps({"pipeline": pipeline, "language": language}),
                                        encoding="utf-8")
        (dest / sl.INPUTS_FILE).write_text(json.dumps(records, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return records


def public_inputs(project_dir: Path) -> list[dict[str, Any]]:
    return [{"n": r.get("n"), "name": r.get("name")} for r in sl.load_inputs(project_dir)]


# ---------------------------------------------------------------------------
# prompt facts
# ---------------------------------------------------------------------------

def _describe_request(q: dict[str, Any], pipeline: str = sl.VIDEO_PIPELINE) -> str:
    scenes = sl.request_scenes(q)
    if scenes and pipeline == "panda-image" and scenes == [1]:
        where = "the image"
    elif scenes:
        where = f"{sl.unit_word(pipeline)} " + ", ".join(str(s) for s in scenes)
    elif str(q.get("moment") or "").strip():
        where = f"moment: {str(q.get('moment')).strip()[:80]}"
    else:
        where = "not placed (do not use it)"
    how = str(q.get("instruction") or "").strip()
    return f"{where}" + (f" — {how[:160]}" if how else "")


def facts(project_dir: Path) -> str:
    """USER SCREENSHOTS block for a leg's prompt; "" for jobs without uploads."""
    project_dir = Path(project_dir)
    recs = sl.load_inputs(project_dir)
    if not recs:
        return ""
    idir = sl.inputs_dir(project_dir).resolve()
    lines = ["", "", "USER SCREENSHOTS — facts for this leg. Follow the 'User screenshots' section of "
             "this stage's director skill.",
             f"- {len(recs)} upload(s) in {idir}. Read-only: never copy, edit or regenerate them, "
             "never put them under assets/, never send them to Higgsfield."]
    for r in recs:
        lines.append(f"  {r.get('n')}. {r['input_id']}  {r.get('name')}  "
                     f"{r.get('width')}x{r.get('height')}  {idir / str(r.get('file'))}")
    pipeline = sl.pipeline_of(project_dir)
    still = sl.is_still_pipeline(pipeline)
    word = sl.unit_word(pipeline)
    reqs = sl.load_requests(project_dir)
    req_path = idir / sl.REQUESTS_FILE
    if reqs is None:
        lines.append(f"- {req_path} does not exist yet.")
    else:
        by_id = {str(q.get("input_id")): q for q in reqs}
        binding = ("every placed screenshot goes on the one image" if pipeline == "panda-image"
                   else f"{word} numbers are binding")
        lines.append(f"- The user's guidance ({req_path}) — {binding}. If feedback in "
                     "this leg changes which screenshot goes where or how, update that file to match:")
        for r in recs:
            q = by_id.get(r["input_id"])
            lines.append(f"  {r.get('n')} → " + (_describe_request(q, pipeline) if q else "(missing)"))
    plan = sl.load_scene_plan(project_dir)
    keep = sl.keep_clear_lines(plan, pipeline=pipeline)
    W, H = sl.canvas_for(plan, pipeline=pipeline)
    ko = sl.keep_out_for(W, H, pipeline)
    if still:
        if keep:
            lines.append("- The screenshot area in the scene plan — the image leaves it plain:"
                         if pipeline == "panda-image" else
                         f"- Screenshot {word}s in the scene plan — the stills leave these areas plain:")
            lines += [f"  {line}" for line in keep]
        lines.append("- The launcher places the screenshots onto the stills itself after every stills "
                     "pass (settled layout, no timing), so the stills the user reviews already show "
                     "them. Do not call screen_overlay; never draw, describe or imitate a screenshot "
                     "in an image prompt; generate and revise from the clean stills under assets/images.")
        lines.append(f"- Canvas {W}x{H}. No caption strip is drawn: {sl.baked_text_words(pipeline)[1]} "
                     f"is part of the still, so keep it out of the screenshot areas. The Panda logo "
                     f"goes over {sl.fmt_box(ko['logo'])} if the "
                     f"{sl.deliverable_word(pipeline)} is branded — keep the screenshot and any "
                     "card out of that box.")
        return "\n".join(lines)
    if keep:
        lines.append("- Screenshot scenes in the scene plan — stills and clips leave these areas plain:")
        lines += [f"  {line}" for line in keep]
        lines.append("- Compose: before panda_render, call screen_overlay mode=compose with the exact "
                     "panda_render scene list (scene_id on every item) and pass panda_render the list it returns.")
    lines.append(f"- Frame {W}x{H}. Captions are drawn over {sl.fmt_box(ko['captions'])}; "
                 f"the Panda logo goes over {sl.fmt_box(ko['logo'])} if the video is branded.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def integrity_notes(project_dir: Path, recs: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    for r in recs:
        p = sl.input_path(project_dir, r)
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            notes.append(f"screenshot {r.get('n')} ({r.get('name')}) is missing from the job")
            continue
        if r.get("sha256") and digest != r["sha256"]:
            notes.append(f"screenshot {r.get('n')} ({r.get('name')}) was changed after upload")
    return notes


def _busy_fraction(img: Any, box: dict[str, float], W: int, H: int) -> float:
    """Share of 'busy' pixels (dark or colourful) inside a frame-fraction box of a W×H canvas,
    after cover-cropping img to the canvas the way panda_render does."""
    from PIL import Image

    iw, ih = img.size
    scale = max(W / iw, H / ih)
    ox, oy = (iw * scale - W) / 2, (ih * scale - H) / 2
    inset = 0.02
    bx0 = (box["x"] + box["w"] * inset) * W
    by0 = (box["y"] + box["h"] * inset) * H
    bx1 = (box["x"] + box["w"] * (1 - inset)) * W
    by1 = (box["y"] + box["h"] * (1 - inset)) * H
    crop = ((bx0 + ox) / scale, (by0 + oy) / scale, (bx1 + ox) / scale, (by1 + oy) / scale)
    region = img.convert("RGB").crop(tuple(int(round(v)) for v in crop))
    region.thumbnail((160, 160), Image.BILINEAR)
    px = list(region.getdata())
    if not px:
        return 0.0
    busy = sum(1 for (r, g, b) in px if min(r, g, b) < 200 or max(r, g, b) - min(r, g, b) > 60)
    return busy / len(px)


def _device_boxes(group: list[dict[str, Any]], recs: dict[str, dict[str, Any]],
                  W: int, H: int) -> list[dict[str, float]]:
    boxes = []
    for it in group:
        rec = recs.get(it["input_id"])
        if rec and sl.valid_box(it["layout"].get("zone")):
            natural = {"width": rec.get("width") or 1, "height": rec.get("height") or 1}
            boxes.append(sl.device_box_fraction(it["layout"], W, H, natural))
    return boxes


def still_clear_notes(project_dir: Path, plan: Optional[dict[str, Any]],
                      recs: list[dict[str, Any]], pipeline: str = sl.VIDEO_PIPELINE) -> list[str]:
    """Is the area each screenshot will cover empty in the generated (clean) still?

    Video stills are cover-cropped to the frame the way panda_render does; a carousel / image
    still IS the deliverable, so it is measured at its own size."""
    from PIL import Image, ImageOps

    still_pipeline = sl.is_still_pipeline(pipeline)
    by_id = {r["input_id"]: r for r in recs}
    notes: list[str] = []
    for scene_id, group in sl.items_by_scene(sl.screenshot_items(plan)).items():
        still = _live_still(project_dir, scene_id) if still_pipeline else \
            sl.scene_media(project_dir, scene_id, "image")
        if still is None:
            continue
        label = sl.unit_label(pipeline, group[0]["scene_number"])
        try:
            # One unreadable still used to abort the whole check: the reviewer then lost every
            # other slide's note (and the placement notes) to one generic "checks could not run".
            with Image.open(still) as raw_im:
                im = ImageOps.exif_transpose(raw_im) if still_pipeline else raw_im
                W, H = im.size if still_pipeline else sl.canvas_for(plan, pipeline=pipeline)
                for box in _device_boxes(group, by_id, W, H):
                    frac = _busy_fraction(im, box, W, H)
                    if frac > CLEAR_MAX_BUSY:
                        what = (f"the character, props or {sl.baked_text_words(pipeline)[0]}"
                                if still_pipeline else "the character or props")
                        notes.append(f"{label}: the still has {what} where the screenshot goes "
                                     f"({frac:.0%} of that area) — regenerate that still with the "
                                     "area empty, or move the screenshot")
                        break
        except Exception:  # noqa: BLE001 — a broken still is reported, not raised
            notes.append(f"{label}: its generated still could not be read — regenerate it")
    return notes


def _frames(media: Path, times: list[float]) -> list[Any]:
    from PIL import Image

    out = []
    for t in times:
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i",
                               str(media), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                              capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        if proc.returncode == 0 and proc.stdout:
            out.append(Image.open(io.BytesIO(proc.stdout)).convert("RGB"))
    return out


def _frames_every(media: Path, step_s: float = 0.25, width: int = 360) -> list[Any]:
    """Every step_s seconds of a video, downscaled, in ONE ffmpeg pass."""
    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="panda_frames_"))
    try:
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(media), "-vf",
                               f"fps={1.0 / step_s:g},scale={width}:-2", str(tmp / "f_%05d.png")],
                              capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            return []
        frames = []
        for f in sorted(tmp.glob("f_*.png")):
            with Image.open(f) as im:
                frames.append(im.convert("RGB"))
        return frames
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _duration(media: Path) -> Optional[float]:
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                               "default=nw=1:nk=1", str(media)], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)
        return float(proc.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def clip_clear_notes(project_dir: Path, plan: Optional[dict[str, Any]],
                     recs: list[dict[str, Any]]) -> list[str]:
    W, H = sl.canvas_for(plan)
    by_id = {r["input_id"]: r for r in recs}
    notes: list[str] = []
    for scene_id, group in sl.items_by_scene(sl.screenshot_items(plan)).items():
        clip = sl.scene_media(project_dir, scene_id, "video")
        if clip is None:
            continue
        dur = _duration(clip) or group[0]["duration"]
        boxes = _device_boxes(group, by_id, W, H)
        worst = 0.0
        for frame in _frames(clip, [dur * f for f in (0.05, 0.3, 0.5, 0.7, 0.95)]):
            for box in boxes:
                worst = max(worst, _busy_fraction(frame, box, W, H))
        if worst > CLEAR_MAX_BUSY:
            notes.append(f"scene {group[0]['scene_number']}: the clip moves the character or props "
                         f"into the screenshot area ({worst:.0%} of it at worst) — regenerate that "
                         "clip with a locked camera, or move the screenshot")
    return notes


def _region_thumb(img: Any, box: dict[str, float], W: int, H: int, size: int = 64) -> list[int]:
    """Greyscale fingerprint of one frame-fraction box (image cover-cropped to the W×H canvas)."""
    from PIL import Image

    iw, ih = img.size
    scale = max(W / iw, H / ih)
    ox, oy = (iw * scale - W) / 2, (ih * scale - H) / 2
    crop = ((box["x"] * W + ox) / scale, (box["y"] * H + oy) / scale,
            ((box["x"] + box["w"]) * W + ox) / scale, ((box["y"] + box["h"]) * H + oy) / scale)
    region = img.convert("L").crop(tuple(int(round(v)) for v in crop))
    rh = max(1, round(size * box["h"] * H / max(1e-6, box["w"] * W)))
    return list(region.resize((size, rh), Image.BILINEAR).getdata())


def _mad(a: list[int], b: list[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, min(len(a), len(b)))


def _settled_time(group: list[dict[str, Any]], duration: float) -> float:
    """A moment when every enter / zoom / cursor / pop has finished (the screenshot is still)."""
    t = 0.0
    for it in group:
        lay = it["layout"]
        enter = lay.get("enter") if isinstance(lay.get("enter"), dict) else {}
        t = max(t, float(enter.get("at_s") or 0) + float(enter.get("duration_s") or 0.35))
        for st in lay.get("steps") or []:
            if not isinstance(st, dict) or st.get("at_s") is None:
                continue
            at = float(st.get("at_s") or 0)
            if st.get("kind") in ("zoom_to", "cursor_move"):
                t = max(t, at + float(st.get("duration_s") or 0.8))
            elif st.get("kind") in ("highlight_box", "card"):
                t = max(t, at + 0.3)
            elif st.get("kind") == "click_pulse":
                t = max(t, at + float(st.get("duration_s") or 0.5))
    return min(max(t + 0.2, duration * 0.3), duration * 0.9)


def final_notes(project_dir: Path, plan: Optional[dict[str, Any]], recs: list[dict[str, Any]],
                final_path: Optional[Path]) -> list[str]:
    W, H = sl.canvas_for(plan)
    ko = sl.keep_out_for(W, H)
    by_id = {r["input_id"]: r for r in recs}
    notes: list[str] = []
    grouped = sl.items_by_scene(sl.screenshot_items(plan))
    if not grouped:
        return notes
    final_frames: list[Any] = []
    if final_path and final_path.is_file():
        final_frames = _frames_every(final_path, 0.25)
    for scene_id, group in grouped.items():
        n = group[0]["scene_number"]
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in scene_id)[:60] or "scene"
        clip = sl.overlay_dir(project_dir) / f"{safe}.mp4"
        meta = sl._read_json(sl.overlay_dir(project_dir) / f"{safe}.json")
        if not clip.is_file() or not isinstance(meta, dict):
            notes.append(f"scene {n}: its screenshots were not rendered — compose must run "
                         "screen_overlay before panda_render")
            continue
        if meta.get("layout_hash") != sl.layout_hash(group, recs):
            notes.append(f"scene {n}: the screenshot layout changed after it was rendered — "
                         "re-run compose")
        if not final_frames:
            continue
        boxes = _device_boxes(group, by_id, W, H)
        if not boxes:
            continue
        x0 = min(b["x"] for b in boxes)
        y0 = min(b["y"] for b in boxes)
        x1 = max(b["x"] + b["w"] for b in boxes)
        y1 = min(max(b["y"] + b["h"] for b in boxes), ko["captions"]["y"])   # captions are drawn later
        if x1 - x0 < 0.02 or y1 - y0 < 0.02:
            continue
        box = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
        dur = float(meta.get("duration_s") or group[0]["duration"])
        t = _settled_time(group, dur)
        probe = _frames(clip, [t])
        raw_src = sl.scene_media(project_dir, scene_id, "video")
        raw = _frames(raw_src, [t]) if raw_src else []
        if not probe or not raw:
            continue
        ref = _region_thumb(probe[0], box, W, H)
        baseline = _mad(ref, _region_thumb(raw[0], box, W, H))
        if baseline < 6:          # the screenshot looks like its background — nothing to prove
            continue
        best = min(_mad(ref, _region_thumb(f, box, W, H)) for f in final_frames)
        if best > min(FINAL_MATCH_MAX_DIFF, 0.5 * baseline):
            notes.append(f"scene {n}: its screenshot could not be found in the final video")
    return notes


# ---------------------------------------------------------------------------
# gate markdown + boards
# ---------------------------------------------------------------------------

def uploads_markdown(recs: list[dict[str, Any]], reqs: Optional[list[dict[str, Any]]],
                     pipeline: str = sl.VIDEO_PIPELINE) -> str:
    by_id = {str(q.get("input_id")): q for q in reqs or []}
    ask = ("say if it goes on the image" if pipeline == "panda-image"
           else f"say which {sl.unit_word(pipeline)} to use it in")
    lines = ["", "## Your screenshots", ""]
    for r in recs:
        q = by_id.get(r["input_id"])
        desc = _describe_request(q, pipeline) if q else "not matched yet"
        desc = desc.replace("not placed (do not use it)", f"not placed — {ask}")
        lines.append(f"- **{r.get('n')}** · {r.get('name')} → {desc}")
    return "\n".join(lines) + "\n"


def layouts_markdown(plan: Optional[dict[str, Any]], recs: list[dict[str, Any]],
                     pipeline: str = sl.VIDEO_PIPELINE) -> str:
    by_id = {r["input_id"]: r for r in recs}
    items = sl.screenshot_items(plan)
    if not items:
        return ""
    lines = ["", "## Screenshots in this plan", ""]
    for it in items:
        rec = by_id.get(it["input_id"], {})
        lay = it["layout"]
        steps = [s.get("kind") for s in lay.get("steps") or [] if isinstance(s, dict)]
        bits = [f"{lay.get('frame', 'card')} frame at {sl.fmt_box(sl.norm_box(lay.get('zone')))}"]
        if "zoom_to" in steps:
            bits.append("zooms in")
        if "highlight_box" in steps:
            bits.append(f"{steps.count('highlight_box')} highlight(s)")
        if "cursor_move" in steps or "click_pulse" in steps:
            bits.append("cursor")
        if "blur_region" in steps:
            bits.append(f"blurs {steps.count('blur_region')} area(s)")
        cards = [s for s in lay.get("steps") or [] if isinstance(s, dict) and s.get("kind") == "card"]
        for c in cards:
            txt = c.get("text")
            if isinstance(txt, dict):
                txt = txt.get("zh") or txt.get("en")
            bits.append(f"card “{str(txt)[:40]}”")
        unit = sl.unit_label(pipeline, it["scene_number"])
        lines.append(f"- **{unit[:1].upper()}{unit[1:]}** · screenshot {rec.get('n')} "
                     f"({rec.get('name')}): " + ", ".join(bits))
    return "\n".join(lines) + "\n"


def _append_md(job_id: str, name: str, text: str) -> None:
    if not text:
        return
    from dify_launcher import store

    p = store.artifact_path(job_id, name)
    if not p.is_file():
        return
    body = p.read_text(encoding="utf-8")
    marker = text.strip().splitlines()[0]
    if marker in body:
        return
    p.write_text(body.rstrip() + "\n" + text, encoding="utf-8")


def _board_signature(project_dir: Path, kind: str, plan: Optional[dict[str, Any]],
                     recs: list[dict[str, Any]], notes_map: dict[str, str], language: str) -> str:
    parts: list[Any] = [kind, language, notes_map, [r.get("sha256") for r in recs],
                        sl.load_requests(project_dir)]
    items = sl.screenshot_items(plan)
    parts.append([(it["scene_id"], it["layout"], it["duration"]) for it in items])
    if kind in ("stills", "clips"):
        for scene_id in sl.items_by_scene(items):
            media = sl.scene_media(project_dir, scene_id, "image" if kind == "stills" else "video")
            if media:
                st = media.stat()
                parts.append((str(media), st.st_size, int(st.st_mtime)))
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_board(project_dir: Path, job_id: str, kind: str, plan: Optional[dict[str, Any]],
                 recs: list[dict[str, Any]], notes_map: dict[str, str], language: str) -> str:
    """Render (or reuse) a gate board and copy it into the launcher store. Returns the file name."""
    from dify_launcher import store
    from tools.video.screen_overlay import ScreenOverlay

    boards = sl.overlay_dir(project_dir) / "boards"
    boards.mkdir(parents=True, exist_ok=True)
    png = boards / BOARD_FILES[kind]
    sig_path = boards / f"{kind}.sig"
    sig = _board_signature(project_dir, kind, plan, recs, notes_map, language)
    try:
        cached = sig_path.read_text(encoding="utf-8").strip() == sig and png.is_file()
    except OSError:
        cached = False
    if not cached:
        res = ScreenOverlay().execute({"mode": "board", "project_dir": str(project_dir), "kind": kind,
                                       "output_path": str(png), "notes": notes_map,
                                       "language": language})
        if not res.success:
            raise RuntimeError(res.error or "board render failed")
        sig_path.write_text(sig, encoding="utf-8")
    store.ensure_job(job_id)
    shutil.copyfile(png, store.artifact_path(job_id, png.name))
    return png.name


def _scene_note_map(notes: list[str], pipeline: str = sl.VIDEO_PIPELINE) -> dict[str, str]:
    out: dict[str, str] = {}
    prefix = f"{sl.unit_word(pipeline)} "
    for note in notes:
        if pipeline == "panda-image" and note.startswith("the image"):
            out.setdefault("1", note.split(":", 1)[-1].strip()[:120])
        elif note.startswith(prefix):
            num = note[len(prefix):].split(" ", 1)[0].split(":", 1)[0].strip("(),")
            if num.isdigit():
                out.setdefault(num, note.split(":", 1)[-1].strip()[:120])
    return out


# ---------------------------------------------------------------------------
# carousel / image: screenshots baked into the stills
# ---------------------------------------------------------------------------

def _stills_dir(project_dir: Path) -> Path:
    return sl.overlay_dir(project_dir) / "stills"


def _read_status(project_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((_stills_dir(project_dir) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _job_language(project_dir: Path) -> str:
    """The language the job runs in (inputs/job.json), as the agent's prompts were told it.

    Defaults to "en" for the same reason the prompts do: only an exact "zh" selects the Chinese
    card text, so guessing zh here would put Chinese cards on an English carousel.
    """
    try:
        data = json.loads((sl.inputs_dir(project_dir) / sl.JOB_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "en"
    lang = str((data or {}).get("language") or "") if isinstance(data, dict) else ""
    return lang.strip().lower() or "en"


def _live_still(project_dir: Path, scene_id: str) -> Optional[Path]:
    """The generated still this scene actually ships, skipping archived takes.

    ``sl.scene_media`` returns the LAST manifest row that exists, and the assets stage keeps
    rejected takes (``rejected_<scene>_takeN.png``, ``*.pre-*``, ``history/``) beside the kept
    still. A manifest that lists the kept still and then an archived take pointed placement at a
    file the launcher never shows, so the slide shipped without its screenshot.
    """
    from dify_launcher.storyboard_preview import is_superseded_still

    project_dir = Path(project_dir)
    found: Optional[Path] = None
    for row in sl.load_manifest_rows(project_dir):
        if str(row.get("scene_id")) != str(scene_id) or row.get("type") != "image":
            continue
        raw = str(row.get("path") or "")
        if not raw or is_superseded_still(raw):
            continue
        p = Path(raw)
        p = p if p.is_absolute() else project_dir / p
        try:
            if p.is_file() and not sl.is_launcher_owned(p, project_dir):
                found = p
        except OSError:
            continue
    return found


def _render_still(project_dir: Path, scene_id: str, still: Path, out: Path,
                  language: str) -> tuple[bool, str]:
    from tools.video.screen_overlay import ScreenOverlay

    res = ScreenOverlay().execute({"mode": "still", "project_dir": str(project_dir),
                                   "scene_id": scene_id, "still_path": str(still),
                                   "output_path": str(out), "language": language})
    return bool(res.success), str(res.error or "")


def _cache_key(scene_id: str) -> str:
    """Cache-file stem for a scene: readable, but unique per exact scene id.

    The readable part is lossy (punctuation folded, cut at 60 characters) and the prune below
    matches "<key>_*", so without the digest a scene id that is another id plus "_..." — or that
    folds to the same text — deleted the other slide's live composite on every pass.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in scene_id)[:60] or "scene"
    return f"{safe}-{hashlib.sha256(scene_id.encode('utf-8')).hexdigest()[:8]}"


def _publish(job_id: str, cached: Path, name: str) -> str:
    """Put a composite into the job store under the still's own name, atomically."""
    from dify_launcher import store

    store.ensure_job(job_id)
    dest = store.artifact_path(job_id, name)
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_bytes(cached.read_bytes())
    try:
        os.replace(tmp, dest)
    except OSError:          # Windows: a reader holds the file open — fall back to a plain copy
        shutil.copyfile(cached, dest)
        tmp.unlink(missing_ok=True)
    return hashlib.sha256(cached.read_bytes()).hexdigest()


def _prune_caches(out_dir: Path, key: str, keep: Path) -> None:
    own = re.compile(re.escape(key) + r"_[0-9a-f]{16}(?:\.[0-9a-f]+)?(?:\.tmp)?\.[a-z0-9]+")
    try:
        entries = list(out_dir.iterdir())
    except OSError:
        return
    for old in entries:
        if old == keep or not own.fullmatch(old.name):
            continue
        try:
            old.unlink()
        except OSError:      # a held-open file is pruned on a later pass; never fail placement
            pass


def place_on_stills(projects_dir: Path, job_id: str, arts: dict[str, Any], *,
                    render: Optional[Any] = None,
                    budget_s: Optional[float] = None) -> list[dict[str, Any]]:
    """Carousel / image: copy each screenshot scene's still, with its screenshots placed, into
    the job store under the still's own name — what Dify shows, the storyboard joins and the
    brand pass stamps. The clean still under assets/images is never touched, so revisions work
    from it. Renders are cached per (still, layout, screenshots, language). Never raises.

    Cached composites are published first, before any render starts, so a slide that has not
    changed is never served as the clean still while another slide renders. ``budget_s`` bounds
    the renders this pass may START (None = STILLS_BUDGET_S, math.inf = no bound, for the pass
    that opens the brand gate).
    """
    project = Path(projects_dir) / job_id
    with _PLACE_LOCKS_GUARD:
        lock = _PLACE_LOCKS.setdefault(job_id, threading.Lock())
    with lock:
        try:
            recs = sl.load_inputs(project)
            pipeline = sl.pipeline_of(project)
            stills = {str(Path(str(n)).name) for n in arts.get("stills") or []}
            if not recs or not sl.is_still_pipeline(pipeline) or not stills:
                return []
            grouped = sl.items_by_scene(sl.screenshot_items(sl.load_scene_plan(project)))
            if not grouped:
                return []
            render = render or _render_still
            language = _job_language(project)
            out_dir = _stills_dir(project)
            out_dir.mkdir(parents=True, exist_ok=True)
            status = _read_status(project)
            deadline = time.monotonic() + (STILLS_BUDGET_S if budget_s is None else budget_s)
            results: list[dict[str, Any]] = []
            todo: list[tuple[dict[str, Any], str, list[dict[str, Any]], Path, str, str, str, Path]] = []

            def publish(entry: dict[str, Any], scene_id: str, raw: Path, raw_sha: str, lh: str,
                        sig: str, key: str, cached: Path) -> None:
                status[scene_id] = {
                    "sig": sig, "name": raw.name, "raw_sha": raw_sha, "layout_hash": lh,
                    "composite": cached.name,
                    "composite_sha": _publish(job_id, cached, raw.name), "failures": 0}
                entry.update(state="placed", name=raw.name)
                _prune_caches(out_dir, key, cached)

            # pass 1 — hash every slide and publish the ones already rendered
            for scene_id, group in grouped.items():
                entry: dict[str, Any] = {"scene_id": scene_id,
                                         "scene_number": group[0]["scene_number"]}
                results.append(entry)
                try:
                    raw = _live_still(project, scene_id)
                    if raw is None or raw.name not in stills:
                        entry["state"] = "no_still"
                        continue
                    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
                    lh = sl.layout_hash(group, recs)
                    sig = hashlib.sha256(json.dumps([raw_sha, lh, language, STILL_RENDER_VERSION])
                                         .encode("utf-8")).hexdigest()[:16]
                    key = _cache_key(scene_id)
                    cached = out_dir / f"{key}_{sig}{raw.suffix.lower()}"
                    if cached.is_file():
                        publish(entry, scene_id, raw, raw_sha, lh, sig, key, cached)
                    else:
                        todo.append((entry, scene_id, group, raw, raw_sha, lh, sig, cached))
                except Exception as e:  # noqa: BLE001 — one bad slide must not skip the others
                    entry.update(state="failed", error=f"{type(e).__name__}: {e}"[:300])

            # pass 2 — render what is missing, newest first is not needed: plan order is fine
            for entry, scene_id, group, raw, raw_sha, lh, sig, cached in todo:
                try:
                    prev = status.get(scene_id) if isinstance(status.get(scene_id), dict) else {}
                    n = prev.get("failures")
                    failures = n if isinstance(n, int) and prev.get("sig") == sig else 0
                    if failures >= 2:
                        entry.update(state="failed", error=prev.get("error") or "render failed")
                        continue
                    if time.monotonic() > deadline:
                        status[scene_id] = {"sig": sig, "name": raw.name, "failures": failures,
                                            "pending": True}
                        entry.update(state="pending")
                        continue
                    ok, err = render(project, scene_id, raw, cached, language)
                    if not ok or not cached.is_file():
                        status[scene_id] = {"sig": sig, "name": raw.name, "failures": failures + 1,
                                            "error": (err or "render produced no file")[:600]}
                        entry.update(state="failed", error=status[scene_id]["error"])
                        continue
                    publish(entry, scene_id, raw, raw_sha, lh, sig, _cache_key(scene_id), cached)
                except Exception as e:  # noqa: BLE001 — record it and carry on with the next slide
                    status[scene_id] = {"sig": sig, "name": raw.name,
                                        "failures": int(status.get(scene_id, {}).get("failures") or 0) + 1
                                        if isinstance(status.get(scene_id), dict) else 1,
                                        "error": f"{type(e).__name__}: {e}"[:300]}
                    entry.update(state="failed", error=status[scene_id]["error"])
            (out_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
            return results
        except Exception:  # noqa: BLE001 — placing screenshots must never break mirroring
            return []


def _failure_reason(error: Any) -> str:
    """Why a placement failed, in plain words — never the tool's command line or server paths."""
    text = str(error or "").lower()
    if "timed out" in text or "timeout" in text:
        return " (drawing the screenshot took too long)"
    if not text:
        return ""
    return " (the screenshot layout could not be drawn — check that slide's layout steps)"


def placement_notes(project_dir: Path, job_id: str, plan: Optional[dict[str, Any]],
                    arts: dict[str, Any], pipeline: str, gate: Optional[str] = None) -> list[str]:
    """Carousel / image gates: is every still that should carry screenshots the placed one?"""
    from dify_launcher import store

    stills = {str(Path(str(n)).name) for n in arts.get("stills") or []}
    status = _read_status(project_dir)
    notes: list[str] = []
    for scene_id, group in sl.items_by_scene(sl.screenshot_items(plan)).items():
        raw = _live_still(project_dir, scene_id)
        label = sl.unit_label(pipeline, group[0]["scene_number"])
        if raw is None or raw.name not in stills:
            # Before the stills pass most slides simply have no still yet. At approve_stills they
            # all should: a still the asset_manifest does not point at is shown unplaced, silently.
            if gate == "approve_stills" and stills:
                notes.append(f"{label}: no generated still for it is listed in the asset manifest, "
                             "so its screenshot could not be placed — check the manifest path for "
                             "this slide")
            continue
        st = status.get(scene_id) if isinstance(status.get(scene_id), dict) else {}
        if st.get("pending") and st.get("name") == raw.name:
            notes.append(f"{label}: its screenshot is not on the still yet (the time limit for one "
                         "placement pass was reached) — it is placed before branding")
            continue
        if st.get("name") != raw.name or not st.get("composite_sha"):
            why = _failure_reason(st.get("error")) if st.get("name") == raw.name else ""
            notes.append(f"{label}: the screenshot could not be placed on the still{why} — it is "
                         "shown without it")
            continue
        try:
            shown = hashlib.sha256(store.artifact_path(job_id, raw.name).read_bytes()).hexdigest()
        except OSError:
            shown = ""
        if shown != st["composite_sha"]:
            notes.append(f"{label}: the still shown is not the version with the screenshot placed")
    return notes


def apply_gate(projects_dir: Path, job_id: str, gate: Optional[str], arts: dict[str, Any],
               *, language: str = "zh", render_boards: bool = True) -> list[str]:
    """Checks + board for a gate. Returns plain-language notes; never raises."""
    project = Path(projects_dir) / job_id
    try:
        recs = sl.load_inputs(project)
    except Exception:  # noqa: BLE001
        return []
    if not recs or not gate:
        return []
    notes: list[str] = []
    try:
        pipeline = sl.pipeline_of(project)
        notes += integrity_notes(project, recs)
        reqs = sl.load_requests(project)
        plan = sl.load_scene_plan(project)
        kind: Optional[str] = None
        notes_map: dict[str, str] = {}
        if gate == "approve_script":
            notes += sl.validate_requests(recs, reqs, pipeline)
            _append_md(job_id, "script.md", uploads_markdown(recs, reqs, pipeline))
            kind = "uploads"
            for q in reqs or []:
                if sl.is_unplaced(q):
                    n = next((r.get("n") for r in recs if r["input_id"] == q.get("input_id")), None)
                    if n is not None:
                        notes_map[str(n)] = f"not placed — say which {sl.unit_word(pipeline)}"
        elif gate == "approve_scene_plan":
            notes += sl.validate_requests(recs, reqs, pipeline)
            notes += sl.validate_layouts(plan, recs, reqs, pipeline=pipeline)
            _append_md(job_id, "scene_plan.md", uploads_markdown(recs, reqs, pipeline)
                       + layouts_markdown(plan, recs, pipeline))
            kind = "layouts" if sl.screenshot_items(plan) else "uploads"
            notes_map = _scene_note_map(notes, pipeline)
        elif gate in ("approve_hero_still", "approve_stills") and sl.is_still_pipeline(pipeline):
            # The stills themselves already show the screenshots — no extra board. The layouts are
            # re-checked here, not only at the plan gate: a stills revise can move or break a
            # layout, and this is the last gate before the brand stamp goes on.
            notes += sl.validate_requests(recs, reqs, pipeline)
            notes += sl.validate_layouts(plan, recs, reqs, pipeline=pipeline)
            notes += still_clear_notes(project, plan, recs, pipeline)
            notes += placement_notes(project, job_id, plan, arts, pipeline, gate)
        elif gate == "approve_brand" and sl.is_still_pipeline(pipeline):
            # last stop before the stamp: say so if a still is about to be branded unplaced
            notes += placement_notes(project, job_id, plan, arts, pipeline, "approve_stills")
        elif gate in ("approve_hero_still", "approve_stills"):
            notes += still_clear_notes(project, plan, recs)
            kind = "stills" if sl.screenshot_items(plan) else None
            notes_map = _scene_note_map(notes)
        elif gate in ("approve_motion_sample", "approve_assets"):
            notes += clip_clear_notes(project, plan, recs)
            kind = "clips" if sl.screenshot_items(plan) else None
            notes_map = _scene_note_map(notes)
        elif gate == "approve_final":
            from dify_launcher import store

            final_name = arts.get("final")
            final_path = store.artifact_path(job_id, final_name) if isinstance(final_name, str) else None
            notes += final_notes(project, plan, recs, final_path)
        if kind and render_boards:
            try:
                arts["screens_board"] = render_board(project, job_id, kind, plan, recs, notes_map, language)
            except Exception as e:  # noqa: BLE001
                notes.append(f"the screenshot preview could not be rendered ({str(e)[:160]})")
    except Exception as e:  # noqa: BLE001 — a check must never break the gate
        notes.append(f"screenshot checks could not run ({type(e).__name__}: {str(e)[:160]})")
    return notes


def question_with_notes(question: str, notes: list[str]) -> str:
    if not notes:
        return question
    uniq = list(dict.fromkeys(notes))
    return question + "\n\nYour screenshots — please check:\n" + "\n".join(f"- {n}" for n in uniq[:12])
