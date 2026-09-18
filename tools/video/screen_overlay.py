"""screen_overlay — lay the user's screenshots over generated Panda shots with Remotion.

Screenshots are never sent to Higgsfield (a video model redraws an image, so UI text breaks).
Instead the scene-plan director writes a layout per placement (see lib/screen_layout.py) and
this tool renders it deterministically, 0 credits:

  mode "board"    one PNG preview sheet for a gate (one cell per set of screenshots on screen
                  together, so one shown after another is not hidden under it)
                   kind "uploads"  numbered thumbnails of every upload + the user's scene
                   kind "layouts"  each screenshot scene on a blank frame, Panda area marked
                   kind "stills"   each screenshot scene over its generated still
                   kind "clips"    each screenshot scene over a frame of its clip
  mode "compose"  for every scene in a panda_render scene list that carries screenshots,
                  render <project>/overlay/<scene_id>.mp4 = the scene's clip (from 0 s, exactly
                  duration_s, cover-cropped like panda_render) with the screenshot layers on top,
                  and return the same list with those media_paths swapped. Call it right
                  before panda_render and pass panda_render the returned list. A placement whose
                  timing does not fit the duration the scene is CUT to fails the call — a shorter
                  cut is never answered by moving or trimming what the user asked for. It also
                  writes overlay/timeline.json (scene order, offsets from the same xfade math
                  panda_render uses, and each screenshot's window) so the launcher can check each
                  screenshot in its own scene instead of anywhere in the video; pass the same
                  `transition` you pass panda_render.
  mode "still"    carousel / image: place one scene's screenshots onto its generated still, at the
                  still's own size, every layer and step in its settled state. Called by the
                  launcher, not by the agent.

Remotion renders a transparent PNG sequence (PandaScreenOverlay) that ffmpeg composites onto
the normalised clip, so the Panda pixels are only touched by the final encode. For stills it
renders one transparent PNG that is alpha-composited onto the still: a PNG still keeps every
pixel outside the screenshot layers exactly as generated, and a JPEG still is re-encoded once
(q95) from the clean original, which shifts pixels outside them by a level or two.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from lib import screen_layout as sl
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_DIR = _ENGINE_ROOT / "remotion-composer"
# Panda-only bundle: src/index.tsx would also load compositions that download Google Fonts.
ENTRY = "src/panda/entry.tsx"
BOARD_TIMEOUT_S = int(os.environ.get("SCREEN_OVERLAY_BOARD_TIMEOUT_S", "300"))
RENDER_TIMEOUT_S = int(os.environ.get("SCREEN_OVERLAY_RENDER_TIMEOUT_S", "900"))
MIN_NODE_MAJOR = 22


def remotion_ready() -> tuple[bool, str]:
    """Node >= 22 on PATH, npx, and remotion-composer's node_modules installed."""
    npx = shutil.which("npx")
    node = shutil.which("node")
    if not npx or not node:
        return False, "Node.js / npx not found on PATH (Node 22+ needed for screenshot scenes)"
    try:
        out = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout.strip()
        major = int(out.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, "could not read the Node.js version"
    if major < MIN_NODE_MAJOR:
        return False, f"Node {out} is too old — screenshot scenes need Node {MIN_NODE_MAJOR}+"
    if not (COMPOSER_DIR / "node_modules" / "@remotion" / "cli").exists():
        return False, "remotion-composer is not installed (run npm install in remotion-composer)"
    return True, "ok"


def _cjk_font() -> Optional[Path]:
    """The CJK font panda_render uses, so cards match the captions."""
    vendor = _ENGINE_ROOT / "vendor"
    os.environ.setdefault("MONTAGE_BRAND_DIR", str(vendor / "brand"))
    os.environ.setdefault("MONTAGE_DATA_DIR", str(vendor / "data"))
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    try:
        from montage_svc.config import resolve_font

        return resolve_font("msyhbd.ttc")
    except Exception:  # noqa: BLE001 — font is best effort; Chromium falls back to system fonts
        return None


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


class ScreenOverlay(BaseTool):
    name = "screen_overlay"
    version = "0.1.0"
    tier = ToolTier.COMPOSE if hasattr(ToolTier, "COMPOSE") else ToolTier.CORE
    capability = "screen_overlay"
    provider = "remotion_local"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg", "cmd:ffprobe", "cmd:npx"]
    install_instructions = (
        "Node.js 22+ with `npm install` in remotion-composer/, ffmpeg + ffprobe on PATH, and the "
        "CJK font panda_render uses (msyhbd.ttc). See deploy/README.md 'Node runtime'."
    )
    capabilities = ["screen_overlay", "user_screenshots"]
    best_for = [
        "placing the user's screenshots over a generated Panda shot exactly as the approved "
        "scene-plan layout says (phone/browser/card frame, zoom, highlight, cursor, blur, cards)",
        "gate preview boards for screenshot scenes",
    ]
    not_good_for = [
        "generating imagery — screenshots are placed, never redrawn",
        "assembling the whole video — that stays panda_render",
    ]
    fallback_tools: list[str] = []

    input_schema = {
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode": {"type": "string", "enum": ["board", "compose", "still"]},
            "project_id": {"type": "string"},
            "project_dir": {"type": "string", "description": "Absolute project dir (else projects/<project_id>)."},
            "kind": {"type": "string", "enum": ["uploads", "layouts", "stills", "clips"],
                     "description": "board only"},
            "output_path": {"type": "string", "description": "board only: PNG path"},
            "notes": {"type": "object", "description": "board only: {scene_number or n: note}"},
            "scenes": {"type": "array", "description": "compose only: the exact panda_render scene list; "
                                                      "add scene_id to each item"},
            "transition": {"type": "object", "description": "compose only: the SAME transition you "
                                                            "pass panda_render, so the recorded "
                                                            "timeline matches the assembled video"},
            "scene_id": {"type": "string", "description": "still only"},
            "still_path": {"type": "string", "description": "still only: the generated still"},
            "resolution": {"type": "string", "default": "1080x1920"},
            "language": {"type": "string", "default": "zh"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=2048, vram_mb=0, disk_mb=2000,
                                       network_required=False)
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    side_effects = ["writes PNG boards and composite clips under projects/<job>/overlay/"]
    user_visible_verification = ["Look at the board / the composite clip: screenshot placed where "
                                 "the layout says, character not covered"]

    # ------------------------------------------------------------------ helpers
    def _project_dir(self, inputs: dict[str, Any]) -> Path:
        if inputs.get("project_dir"):
            return Path(inputs["project_dir"])
        from lib.paths import PROJECTS_DIR

        return Path(PROJECTS_DIR) / str(inputs["project_id"])

    def _scene_plan(self, project: Path) -> Optional[dict[str, Any]]:
        return sl.load_scene_plan(project)

    def _manifest_media(self, project: Path, scene_id: str, kind: str) -> Optional[Path]:
        return sl.scene_media(project, scene_id, kind)

    def _run(self, cmd: list[str], timeout: int, cwd: Optional[Path] = None) -> None:
        self.run_command(cmd, timeout=timeout, cwd=cwd)

    def _stage_font(self, public: Path) -> Optional[str]:
        font = _cjk_font()
        if font and font.is_file():
            _link_or_copy(font, public / f"panda-cjk{font.suffix}")
            return f"panda-cjk{font.suffix}"
        return None

    def _frame_png(self, clip: Path, out: Path, at_fraction: float = 0.6) -> Path:
        dur = self._probe_duration(clip) or 1.0
        out.parent.mkdir(parents=True, exist_ok=True)
        self._run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0.0, dur * at_fraction):.3f}",
                   "-i", str(clip), "-frames:v", "1", str(out)], timeout=120)
        return out

    def _probe_duration(self, media: Path) -> Optional[float]:
        try:
            res = self.run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", str(media)], timeout=60)
            return float(res.stdout.strip())
        except Exception:  # noqa: BLE001
            return None

    def _probe_fps(self, media: Path) -> float:
        try:
            res = self.run_command(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                    "-show_entries", "stream=r_frame_rate", "-of",
                                    "default=nw=1:nk=1", str(media)], timeout=60)
            num, _, den = res.stdout.strip().partition("/")
            fps = float(num) / float(den or 1)
            return fps if 1 <= fps <= 120 else 30.0
        except Exception:  # noqa: BLE001
            return 30.0

    # ------------------------------------------------------------------ board
    def _board(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        project = self._project_dir(inputs)
        kind = inputs.get("kind") or "layouts"
        out = Path(inputs.get("output_path") or sl.overlay_dir(project) / "boards" / f"{kind}.png")
        notes = {str(k): str(v) for k, v in (inputs.get("notes") or {}).items()}
        recs = sl.load_inputs(project)
        if not recs:
            return ToolResult(success=False, error="this project has no user screenshots")
        pipeline = sl.pipeline_of(project)
        word = sl.unit_word(pipeline)
        by_id = {r["input_id"]: r for r in recs}
        work = sl.overlay_dir(project) / "work" / f"board_{kind}"
        public = work / "public"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        public.mkdir(parents=True, exist_ok=True)
        font = self._stage_font(public)
        for r in recs:
            src = sl.input_path(project, r)
            if src.is_file():
                _link_or_copy(src, public / src.name)

        cells: list[dict[str, Any]] = []
        title = ""
        if kind == "uploads":
            title = "Your screenshots"
            reqs = {str(q.get("input_id")): q for q in (sl.load_requests(project) or [])}
            for r in sorted(recs, key=lambda x: int(x.get("n") or 0)):
                q = reqs.get(r["input_id"])
                if q is None:
                    where = "not matched yet"
                elif sl.request_scenes(q):
                    where = (sl.unit_label(pipeline, 1) if pipeline == "panda-image"
                             and sl.request_scenes(q) == [1]
                             else f"{word} " + ", ".join(str(s) for s in sl.request_scenes(q)))
                elif str(q.get("moment") or "").strip():
                    where = str(q.get("moment"))[:40]
                else:
                    where = "not placed"
                note = notes.get(str(r.get("n")), "")
                cells.append({
                    "label": (f"{r.get('n')} · {str(r.get('name'))[:24]}" if note
                              else f"{r.get('n')} · {str(r.get('name'))[:24]} → {where}"),
                    "canvas": {"width": 1080, "height": 1080},
                    "background": {"type": "color", "color": "#ffffff"},
                    "layers": [{
                        "src": sl.input_path(project, r).name,
                        "natural": {"width": int(r.get("width") or 1), "height": int(r.get("height") or 1)},
                        "layout": {"zone": {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.9}, "frame": "none"},
                    }],
                    "note": note,
                })
            columns, cell_w = 5, 240
        else:
            plan = self._scene_plan(project)
            items = sl.screenshot_items(plan)
            if not items:
                return ToolResult(success=False, error="the scene plan places no screenshots")
            W, H = sl.canvas_for(plan, inputs.get("resolution"), pipeline)
            title = {"layouts": "Layouts · Panda area marked",
                     "stills": "Over the stills",
                     "clips": "Over the clips"}.get(kind, "Screenshots")
            still_pipeline = sl.is_still_pipeline(pipeline)
            for scene_id, group in sl.items_by_scene(items).items():
                n = group[0]["scene_number"]
                background: dict[str, Any] = {"type": "color", "color": "#ffffff"}
                cell_note = notes.get(str(n), "")
                if kind == "stills":
                    still = self._manifest_media(project, scene_id, "image")
                    if still:
                        _link_or_copy(still, public / f"bg_{hashlib.md5(scene_id.encode()).hexdigest()[:8]}{still.suffix}")
                        background = {"type": "image",
                                      "src": f"bg_{hashlib.md5(scene_id.encode()).hexdigest()[:8]}{still.suffix}"}
                    else:
                        cell_note = cell_note or "no still found for this scene"
                elif kind == "clips":
                    clip = self._manifest_media(project, scene_id, "video")
                    if clip:
                        name = f"bg_{hashlib.md5(scene_id.encode()).hexdigest()[:8]}.png"
                        self._frame_png(clip, public / name)
                        background = {"type": "image", "src": name}
                    else:
                        cell_note = cell_note or "no clip found for this scene"
                unit = sl.unit_label(pipeline, n)
                duration = float(group[0]["duration"])
                # One cell per set of screenshots actually on screen together: a scene that shows
                # one screenshot and then another gives two cells, so the later one cannot hide the
                # earlier one. A still (or screenshots shown together) stays a single cell.
                windows = ([(0.0, duration, group)] if still_pipeline
                           else sl.display_windows(group, duration) or [(0.0, duration, group)])
                for w_from, w_to, live in windows:
                    layers, subjects = [], []
                    for it in live:
                        rec = by_id.get(it["input_id"])
                        if rec is None or not sl.valid_box(it["layout"].get("zone")):
                            continue
                        layers.append(sl.layer_props(it, rec, sl.input_path(project, rec).name))
                        if sl.valid_box(it["layout"].get("subject_zone")):
                            subjects.append(sl.norm_box(it["layout"]["subject_zone"]))
                    names = ", ".join(str(by_id[it["input_id"]].get("n")) for it in live
                                      if it["input_id"] in by_id)
                    label = f"{unit[:1].upper()}{unit[1:]} · screenshot {names}"
                    cell: dict[str, Any] = {
                        "label": label,
                        "canvas": {"width": W, "height": H},
                        "sceneDuration": duration,
                        "background": background,
                        "layers": layers,
                        "subjectZones": subjects if kind == "layouts" else [],
                        "note": cell_note,
                    }
                    if not still_pipeline and len(windows) > 1:
                        at = sl.preview_time(live, duration, (w_from, w_to))
                        cell["label"] = f"{label} · {w_from:g}–{w_to:g} s"
                        cell["atSeconds"] = at        # draw the layers as they are at that moment
                    cells.append(cell)
            columns, cell_w = (4, 300) if H >= W else (3, 420)

        props = {"title": title, "cells": cells, "columns": columns, "cellWidth": cell_w,
                 "language": inputs.get("language") or "zh"}
        if font:
            props["fontSrc"] = font
        props_path = work / "props.json"
        props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(["npx", "remotion", "still", ENTRY, "PandaScreenBoard", str(out.resolve()),
                       f"--props={props_path.resolve()}", f"--public-dir={public.resolve()}"],
                      timeout=BOARD_TIMEOUT_S, cwd=COMPOSER_DIR)
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"board render failed: {e}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not out.is_file():
            return ToolResult(success=False, error="board render produced no file")
        return ToolResult(success=True, data={"output_path": str(out), "kind": kind, "cells": len(cells)},
                          artifacts=[str(out)], duration_seconds=round(time.time() - start, 2))

    # ------------------------------------------------------------------ compose
    def _compose(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        project = self._project_dir(inputs)
        scenes_in = inputs.get("scenes")
        if not isinstance(scenes_in, list) or not scenes_in:
            return ToolResult(success=False, error="compose needs the panda_render scene list")
        recs = {r["input_id"]: r for r in sl.load_inputs(project)}
        plan = self._scene_plan(project)
        grouped = sl.items_by_scene(sl.screenshot_items(plan))
        # A timeline from an earlier compose describes a render that is about to be replaced; drop
        # it now so a failed compose cannot leave one that looks current.
        stale = sl.overlay_dir(project) / "timeline.json"
        if stale.exists():
            stale.unlink()
        if not grouped:
            return ToolResult(success=True, data={"scenes": scenes_in, "rendered": []},
                              duration_seconds=round(time.time() - start, 2))
        W, H = sl.canvas_for(plan, inputs.get("resolution") or "1080x1920")
        language = inputs.get("language") or "zh"
        out_dir = sl.overlay_dir(project)
        scenes_out: list[dict[str, Any]] = []
        # The scene id each item was resolved to — given, or inferred from its media path. Kept
        # beside the list (not written into the items panda_render receives) so the timeline
        # records an inferred scene with its screenshots instead of as an anonymous one.
        scene_ids: list[str] = []
        rendered: list[dict[str, Any]] = []
        path_to_scene = self._path_scene_index(project)

        for i, sc in enumerate(scenes_in):
            item = dict(sc)
            scene_id = str(sc.get("scene_id") or "")
            media = Path(str(sc.get("media_path") or ""))
            if not scene_id:
                scene_id = path_to_scene.get(str(media.resolve()), "")
            scene_ids.append(scene_id)
            group = grouped.get(scene_id)
            if not group:
                scenes_out.append(item)
                continue
            if not media.is_file():
                return ToolResult(success=False, error=f"scene {scene_id}: media not found: {media}")
            duration = float(sc.get("duration_s") or group[0]["duration"])
            # The cut can be shorter than the scene the plan wrote, which would push a placement
            # (or its steps) past the end of the composed scene. Say so and stop — never move or
            # shorten what the user asked for, and never report success for a screenshot that
            # would not be on screen.
            bad = sl.timing_errors(group, duration, recs.values())
            if bad:
                return ToolResult(success=False, error=(
                    f"scene {scene_id}: the screenshot timing does not fit this cut — "
                    + "; ".join(bad)
                    + ". Fix the scene plan or the cut and compose again; screen_overlay will not "
                      "move a placement the user asked for."))
            try:
                composite = self._render_scene(project, scene_id, group, recs, media, duration,
                                               W, H, language)
            except Exception as e:  # noqa: BLE001
                return ToolResult(success=False, error=f"scene {scene_id}: overlay render failed: {e}")
            item["media_path"] = str(composite)
            item["duration_s"] = duration
            scenes_out.append(item)
            rendered.append({"scene_id": scene_id, "path": str(composite),
                             "layout_hash": sl.layout_hash(group, recs.values())})
        timeline = self._write_timeline(project, scenes_out, grouped, recs,
                                        inputs.get("transition"), scene_ids)
        return ToolResult(success=True,
                          data={"scenes": scenes_out, "rendered": rendered, "timeline": timeline},
                          artifacts=[r["path"] for r in rendered],
                          duration_seconds=round(time.time() - start, 2))

    def _write_timeline(self, project: Path, scenes: list[dict[str, Any]],
                        grouped: dict[str, list[dict[str, Any]]],
                        recs: dict[str, dict[str, Any]],
                        transition: Any,
                        scene_ids: Optional[list[str]] = None) -> dict[str, Any]:
        """Record where each scene — and each screenshot — lands in the assembled video.

        Without this the launcher can only look for a screenshot somewhere in the final video,
        which passes a screenshot that ended up in the wrong scene. The offsets are panda_render's
        own xfade math (``expected_timeline_duration``): a transition overlaps its two scenes, so
        every scene after the first starts earlier by the overlap. When the caller did not pass the
        transition we record panda_render's default AND that we assumed it, and the launcher checks
        the total against the real file before trusting any window.
        """
        tr = transition if isinstance(transition, dict) else {}
        kind = str(tr.get("type") or "xfade")
        overlap = max(0.0, float(tr.get("duration_s", 0.5))) if kind == "xfade" else 0.0
        rows: list[dict[str, Any]] = []
        t = 0.0
        for i, sc in enumerate(scenes):
            dur = float(sc.get("duration_s") or 0.0)
            scene_id = (scene_ids[i] if scene_ids and i < len(scene_ids) and scene_ids[i]
                        else str(sc.get("scene_id") or ""))
            shots = []
            for it in grouped.get(scene_id, []):
                rec = recs.get(it["input_id"]) or {}
                frm, to = sl.show_window(it.get("layout") or {}, dur)
                s_from, s_to = sl.settled_span(it.get("layout") or {}, dur, (frm, to))
                shots.append({"input_id": it["input_id"], "n": rec.get("n"),
                              "from_s": round(max(0.0, min(frm, dur)), 3),
                              "to_s": round(max(0.0, min(to, dur)), 3),
                              "settled_from_s": round(s_from, 3), "settled_to_s": round(s_to, 3)})
            rows.append({"index": i, "scene_id": scene_id, "start_s": round(t, 3),
                         "duration_s": round(dur, 3), "screenshots": shots})
            t += dur - (overlap if i < len(scenes) - 1 else 0.0)
        out = {"version": "1.0",
               "transition": {"type": kind, "duration_s": overlap},
               "transition_assumed": not isinstance(transition, dict),
               "total_s": round(t, 3), "scenes": rows}
        path = sl.overlay_dir(project) / "timeline.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return out

    def _path_scene_index(self, project: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in sl.load_manifest_rows(project):
            if row.get("path") and row.get("scene_id"):
                p = Path(str(row["path"]))
                p = p if p.is_absolute() else project / p
                try:
                    out[str(p.resolve())] = str(row["scene_id"])
                except OSError:
                    continue
        return out

    def _render_scene(self, project: Path, scene_id: str, group: list[dict[str, Any]],
                      recs: dict[str, dict[str, Any]], media: Path, duration: float,
                      W: int, H: int, language: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in scene_id)[:60] or "scene"
        out_dir = sl.overlay_dir(project)
        work = out_dir / "work" / f"compose_{safe}"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        public = work / "public"
        seq = work / "seq"
        public.mkdir(parents=True, exist_ok=True)
        seq.mkdir(parents=True, exist_ok=True)
        try:
            is_image = media.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
            fps = 30.0 if is_image else self._probe_fps(media)
            frames = max(1, round(duration * fps))
            bg = work / "background.mp4"
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                  f"format=yuv420p,setsar=1")
            if is_image:
                cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-t", f"{duration}",
                       "-i", str(media), "-vf", f"{vf},fps={fps}", "-an",
                       "-c:v", "libx264", "-crf", "12", "-preset", "medium", str(bg)]
            else:
                cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(media), "-vf",
                       f"{vf},tpad=stop_mode=clone:stop_duration={duration},"
                       f"trim=duration={duration},setpts=PTS-STARTPTS", "-an",
                       "-c:v", "libx264", "-crf", "12", "-preset", "medium", str(bg)]
            self._run(cmd, timeout=RENDER_TIMEOUT_S)

            layers = []
            for it in group:
                rec = recs.get(it["input_id"])
                if rec is None:
                    raise ValueError(f"unknown screenshot id {it['input_id']}")
                src = sl.input_path(project, rec)
                _link_or_copy(src, public / src.name)
                layers.append(sl.layer_props(it, rec, src.name))
            props = {"width": W, "height": H, "fps": fps, "durationInFrames": frames,
                     "sceneDuration": duration, "background": {"type": "none"},
                     "layers": layers, "language": language}
            font = self._stage_font(public)
            if font:
                props["fontSrc"] = font
            props_path = work / "props.json"
            props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
            self._run(["npx", "remotion", "render", ENTRY, "PandaScreenOverlay", str(seq.resolve()),
                       "--sequence", "--image-format=png", f"--props={props_path.resolve()}",
                       f"--public-dir={public.resolve()}"],
                      timeout=RENDER_TIMEOUT_S, cwd=COMPOSER_DIR)
            pngs = sorted(seq.glob("element-*.png"))
            if len(pngs) != frames:
                raise RuntimeError(f"expected {frames} overlay frames, got {len(pngs)}")
            digits = len(pngs[0].stem.split("-")[-1])
            out = out_dir / f"{safe}.mp4"
            self._run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(bg), "-framerate", f"{fps}",
                       "-i", str(seq / f"element-%0{digits}d.png"), "-filter_complex",
                       "[0:v][1:v]overlay=0:0:format=auto:eof_action=repeat,format=yuv420p[v]",
                       "-map", "[v]", "-frames:v", str(frames), "-r", f"{fps}", "-an",
                       "-c:v", "libx264", "-crf", "12", "-preset", "medium", str(out)],
                      timeout=RENDER_TIMEOUT_S)
            meta = {"scene_id": scene_id, "layout_hash": sl.layout_hash(group, recs.values()),
                    "duration_s": duration, "fps": fps, "frames": frames,
                    "source_media": str(media), "canvas": [W, H]}
            (out_dir / f"{safe}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
            return out
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------------ still
    def _still(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        project = self._project_dir(inputs)
        scene_id = str(inputs.get("scene_id") or "")
        still = Path(str(inputs.get("still_path") or ""))
        out = inputs.get("output_path")
        if not scene_id or not out or not still.is_file():
            return ToolResult(success=False,
                              error="still needs scene_id, output_path and an existing still_path")
        recs = {r["input_id"]: r for r in sl.load_inputs(project)}
        group = sl.items_by_scene(sl.screenshot_items(self._scene_plan(project))).get(scene_id)
        if not group:
            return ToolResult(success=False, error=f"scene {scene_id} places no screenshots")
        try:
            path = self._render_still(project, scene_id, group, recs, still, Path(str(out)),
                                      inputs.get("language") or "zh")
        except subprocess.TimeoutExpired as e:
            # Cause first: str(CalledProcessError) starts with the whole npx command line and
            # absolute server paths, which is all that survives the launcher's status.json and
            # gate-note truncation — the reader learned nothing about what actually failed.
            return ToolResult(success=False,
                              error=f"scene {scene_id}: the Remotion still render timed out after "
                                    f"{float(e.timeout or 0):.0f} s")
        except subprocess.CalledProcessError as e:
            detail = str(getattr(e, "detail", "") or e.stderr or e.stdout or "")
            detail = re.sub(r"\x1b\[[0-9;]*m", "", detail).strip()
            return ToolResult(success=False,
                              error=f"scene {scene_id}: Remotion exited {e.returncode}: "
                                    f"{detail[:400] or 'no output'}")
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False,
                              error=f"scene {scene_id}: still render failed: "
                                    f"{type(e).__name__}: {str(e)[:300]}")
        return ToolResult(success=True,
                          data={"output_path": str(path), "scene_id": scene_id,
                                "layout_hash": sl.layout_hash(group, recs.values())},
                          artifacts=[str(path)], duration_seconds=round(time.time() - start, 2))

    def _render_still(self, project: Path, scene_id: str, group: list[dict[str, Any]],
                      recs: dict[str, dict[str, Any]], still: Path, out: Path,
                      language: str) -> Path:
        import tempfile

        from PIL import Image, ImageOps

        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in scene_id)[:60] or "scene"
        # A work dir per CALL: two placement passes for the same scene (two syncs of one job) used
        # to delete each other's staging dir mid-render and record a phantom render failure.
        base_work = sl.overlay_dir(project) / "work"
        base_work.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(dir=base_work, prefix=f"still_{safe}_"))
        public = work / "public"
        public.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(still) as raw_im:
                # Measure and composite on the image as it is DISPLAYED: a still carrying an EXIF
                # orientation tag would otherwise get the screenshot drawn on the stored (rotated)
                # pixel grid. 16-bit / float stills would clip to pure black and white on a plain
                # RGBA convert, so scale them down to 8-bit first.
                im = ImageOps.exif_transpose(raw_im)
                had_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
                if im.mode.startswith("I;16"):
                    im = im.convert("I")
                if im.mode in ("I", "F"):
                    im = im.point(lambda v: v * (1 / 256)).convert("L")
                base = im.convert("RGBA")
            W, H = base.size
            layers = []
            for it in group:
                rec = recs.get(it["input_id"])
                if rec is None:
                    raise ValueError(f"unknown screenshot id {it['input_id']}")
                src = sl.input_path(project, rec)
                _link_or_copy(src, public / src.name)
                layers.append(sl.layer_props(it, rec, src.name))
            props = {"width": W, "height": H, "fps": 30, "durationInFrames": 1,
                     "sceneDuration": group[0]["duration"], "background": {"type": "none"},
                     "still": True, "layers": layers, "language": language}
            font = self._stage_font(public)
            if font:
                props["fontSrc"] = font
            props_path = work / "props.json"
            props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
            layer_png = work / "layers.png"
            self._run(["npx", "remotion", "still", ENTRY, "PandaScreenOverlay", str(layer_png.resolve()),
                       "--image-format=png", f"--props={props_path.resolve()}",
                       f"--public-dir={public.resolve()}"],
                      timeout=BOARD_TIMEOUT_S, cwd=COMPOSER_DIR)
            with Image.open(layer_png) as lp:
                layer = lp.convert("RGBA")
            if layer.size != base.size:
                raise RuntimeError(f"overlay is {layer.size}, still is {base.size}")
            base.alpha_composite(layer)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f"{out.stem}.{work.name[-8:]}.tmp{out.suffix}")
            if out.suffix.lower() in (".jpg", ".jpeg"):
                # Pillow reads icc_profile/exif for JPEG only from the save arguments, so they have
                # to be carried over explicitly or the composite loses the still's colour profile.
                extra = {k: v for k, v in (("icc_profile", base.info.get("icc_profile")),
                                           ("exif", base.info.get("exif"))) if v}
                base.convert("RGB").save(tmp, "JPEG", quality=95, **extra)
            else:
                (base if had_alpha else base.convert("RGB")).save(tmp, "PNG")
            os.replace(tmp, out)
            return out
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------------ entry
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        mode = inputs.get("mode")
        if not (inputs.get("project_dir") or inputs.get("project_id")):
            return ToolResult(success=False, error="project_id or project_dir is required")
        ok, why = remotion_ready()
        if not ok:
            return ToolResult(success=False, error=f"Remotion unavailable: {why}")
        if mode == "board":
            return self._board(inputs)
        if mode == "compose":
            return self._compose(inputs)
        if mode == "still":
            return self._still(inputs)
        return ToolResult(success=False, error="mode must be 'board', 'compose' or 'still'")
