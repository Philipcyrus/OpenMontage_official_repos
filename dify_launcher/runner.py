"""Runners drive a job through the pipeline gates.

A runner advances a job to its NEXT human-approval gate, then stops. Each HTTP call
(start / respond) moves the job forward one leg. This mirrors how the real agent works:
it runs until a checkpoint writes `awaiting_human`, then pauses for Dify.

Two runners:
  - MockRunner        : no LLM, no Higgsfield. Fakes script + a TEXT scene plan, generates
                        mock media in the assets stage, and REALLY renders a clean master via
                        panda_render. Lets us test the whole Dify handshake + gates, no EC2.
  - ClaudeCodeRunner  : the EC2 path — invokes Claude Code headless against the engine repo.
                        Skeleton only; swap it in where the box has `claude` + OpenRouter + MCP.

Gate sequence (matches pipeline_defs/panda-video.yaml — upstream shape + Panda cost gates):
    start ─▶ GATE 1 approve_script ─▶ GATE 2 approve_scene_plan (TEXT) ─▶ GATE 3 approve_stills
          ─▶ [GATE 3.5 approve_motion_sample] ─▶ GATE 4 approve_assets ─▶ GATE 5 approve_final
          ─▶ GATE 6 approve_brand ─▶ done
scene_plan produces a TEXT plan only (no media). The assets stage runs in up to THREE human-
reviewed phases (all stage="assets"): first STILLS ONLY (cheap — approve the look before any
video); then, when the job option motion_sample is on (default), ONE hero still is animated into
a MOTION SAMPLE (approve the motion/animation before batching all clips — the biggest cost/time
gate); then the full media (all clips + voice + music) recorded in asset_manifest. The pauses are
distinguished by the checkpoint's partial_progress.phase ("stills" | "motion_sample" | full).
approve_brand is a launcher-only gate after the last content gate: approve stamps BGC copies,
skip keeps UGC, revise stays. Branding does not flow through animation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from dify_launcher import store

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _is_storyboard_name(name: Any) -> bool:
    """Defensive: treat any 'storyboard' artifact (e.g. left over from an old job) as not a scene still."""
    return "storyboard" in Path(str(name)).name.lower()

# ordered human-approval gates. Note: approve_stills + approve_motion_sample + approve_assets are
# pauses of the SINGLE `assets` stage (stills cost gate, motion-sample cost gate, then full media)
# — see _sync/_do_stills/_do_motion_sample. approve_motion_sample only occurs when the job option
# motion_sample is on (default true).
GATES = ["approve_script", "approve_scene_plan", "approve_stills",
         "approve_motion_sample", "approve_assets", "approve_final", "approve_brand"]
# gates from the previous storyboard-stills flow — resuming one is refused with a migration note
_LEGACY_GATES = {"approve_storyboard", "approve_clips"}


def _motion_sample_enabled(state: dict[str, Any]) -> bool:
    """Whether to insert the one-clip motion-sample cost gate (job option, default ON)."""
    v = (state.get("options") or {}).get("motion_sample", True)
    return str(v).lower() not in ("false", "0", "no", "off", "")


def _budget_cap(state: dict[str, Any]) -> Optional[int]:
    """Approved Higgsfield credit ceiling for the run (job option `max_higgsfield_credits`).
    None = no cap (unlimited — today's behavior). Credits are the authoritative enforcement unit."""
    v = (state.get("options") or {}).get("max_higgsfield_credits")
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_DEFAULT_PIPELINE = os.environ.get("PANDA_PIPELINE_TYPE", "panda-video")


def _pipeline_of(state: dict[str, Any]) -> str:
    """Per-job pipeline name. Falls back to PANDA_PIPELINE_TYPE / panda-video."""
    p = state.get("pipeline") or _DEFAULT_PIPELINE
    return str(p).strip() or _DEFAULT_PIPELINE


_STILLS_TERMINAL = frozenset({"panda-carousel", "panda-image"})


def _is_carousel(state: dict[str, Any]) -> bool:
    return _pipeline_of(state) == "panda-carousel"


def _is_image(state: dict[str, Any]) -> bool:
    return _pipeline_of(state) == "panda-image"


def _is_stills_terminal(state: dict[str, Any]) -> bool:
    """Carousel and single-image jobs finish at approve_stills (no motion/clips/compose)."""
    return _pipeline_of(state) in _STILLS_TERMINAL


def _script_gate_enabled(state: dict[str, Any]) -> bool:
    """False when options.gates is set and omits script (carousel gate-collapse). Default True."""
    gates = (state.get("options") or {}).get("gates")
    if not gates:
        return True
    names = {str(g).lower().replace("approve_", "").strip() for g in gates}
    return "script" in names


# stills revise at GATE 3 (carousel + video): explicit mode, else infer from the note
_FRESH_NOTE_RE = re.compile(
    r"\b(regenerate|redo|new|from scratch|different scene|start over|fresh)\b", re.I)
_EDIT_NOTE_RE = re.compile(
    r"\b(change|fix|remove|keep|edit|adjust|replace)\b", re.I)


def _stills_revise_mode(response: dict[str, Any]) -> str:
    """Resolve stills revise mode: explicit `fresh`/`edit`, else infer. Default fresh."""
    raw = (response or {}).get("mode")
    if raw is not None and str(raw).strip():
        m = str(raw).strip().lower()
        if m in ("fresh", "edit"):
            return m
    note = str((response or {}).get("answer") or "")
    shots = (response or {}).get("shots") or []
    if _FRESH_NOTE_RE.search(note):
        return "fresh"
    if shots and _EDIT_NOTE_RE.search(note):
        return "edit"
    return "fresh"


def _revise_shot_indices(response: dict[str, Any], n: int) -> list[int]:
    """1-based `shots` from /respond; empty means all. Returns 0-based indices in range."""
    shots = (response or {}).get("shots") or []
    if not shots or n <= 0:
        return list(range(max(n, 0)))
    out: list[int] = []
    for s in shots:
        try:
            i = int(s)
        except (TypeError, ValueError):
            continue
        if i >= 1:
            i -= 1
        if 0 <= i < n and i not in out:
            out.append(i)
    return out or list(range(n))


def _still_basename(name: Any) -> str:
    return Path(str(name)).name


def _still_abs_paths(job_id: str, state: Optional[dict[str, Any]], shots: list[Any],
                     projects_dir: Optional[Path] = None) -> list[str]:
    """Absolute paths of current stills (flagged shots if set, else all)."""
    names = [_still_basename(n) for n in (state or {}).get("artifacts", {}).get("stills") or []]
    if not names:
        return []
    indices = _revise_shot_indices({"shots": shots}, len(names))
    engine_images = (projects_dir / job_id / "assets" / "images") if projects_dir else None
    paths: list[str] = []
    for i in indices:
        basename = names[i]
        if engine_images is not None:
            eng = engine_images / basename
            if eng.is_file():
                paths.append(str(eng.resolve()))
                continue
        p = store.artifact_path(job_id, basename)
        paths.append(str(p.resolve()) if p.exists() else str(p))
    return paths


# carousel slide canvas — caller sets options.aspect_ratio (default 4:5). Do not coerce to 4:5/1:1.
_CAROUSEL_PIXEL_SIZES = {
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "3:4": (1080, 1440),
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "4:3": (1440, 1080),
}


def _stills_aspect(options: Optional[dict[str, Any]] = None,
                   state: Optional[dict[str, Any]] = None,
                   pipeline: Optional[str] = None) -> str:
    """Job option `aspect_ratio`. Default 1:1 for panda-image, 4:5 otherwise. Pass-through."""
    p = pipeline or _pipeline_of(state or {})
    default = "1:1" if p == "panda-image" else "4:5"
    opts = options if options is not None else ((state or {}).get("options") or {})
    raw = str((opts or {}).get("aspect_ratio") or default).strip()
    return raw or default


def _carousel_aspect(options: Optional[dict[str, Any]] = None,
                     state: Optional[dict[str, Any]] = None) -> str:
    """Job option `aspect_ratio`, default 4:5 (carousel / video). Pass-through."""
    return _stills_aspect(options=options, state=state)


def _carousel_pixel_size(ratio: str) -> tuple[int, int]:
    """Mock placeholder size for a carousel ratio. Unknown W:H → 1080 on the short side."""
    key = str(ratio or "4:5").strip().lower().replace(" ", "")
    if key in _CAROUSEL_PIXEL_SIZES:
        return _CAROUSEL_PIXEL_SIZES[key]
    m = re.match(r"^(\d+)x(\d+)$", key)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w > 0 and h > 0:
            return (w, h)
    m = re.match(r"^(\d+):(\d+)$", key)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            if a <= b:
                return (1080, max(1, round(1080 * b / a)))
            return (max(1, round(1080 * a / b)), 1080)
    return _CAROUSEL_PIXEL_SIZES["4:5"]


def _script_to_markdown(script: dict[str, Any]) -> str:
    """Human-readable script.md for Dify's file-preview slot (JSON stays on artifacts.script)."""
    title = str(script.get("title") or "Script")
    lines = [f"# {title}", ""]
    dur = script.get("total_duration_seconds")
    if dur is not None:
        lines.append(f"_Duration: {dur}s_")
        lines.append("")
    for i, sec in enumerate(script.get("sections") or [], 1):
        if not isinstance(sec, dict):
            continue
        label = sec.get("label") or sec.get("id") or f"Section {i}"
        start, end = sec.get("start_seconds"), sec.get("end_seconds")
        timing = ""
        if start is not None or end is not None:
            timing = f" ({start}s–{end}s)"
        lines.append(f"## {label}{timing}")
        text = str(sec.get("text") or "").strip()
        if text:
            lines.append(text)
        directions = str(sec.get("speaker_directions") or "").strip()
        if directions:
            lines.append("")
            lines.append(f"*{directions}*")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _scene_plan_to_markdown(plan: dict[str, Any]) -> str:
    """Human-readable scene_plan.md for Dify's file-preview slot."""
    lines = ["# Scene plan", ""]
    meta = plan.get("metadata") if isinstance(plan.get("metadata"), dict) else {}
    ratio = meta.get("aspect_ratio")
    if ratio:
        lines.append(f"_Aspect ratio: {ratio}_")
        lines.append("")
    for i, sc in enumerate(plan.get("scenes") or [], 1):
        if not isinstance(sc, dict):
            continue
        sid = sc.get("id") or f"scene-{i}"
        lines.append(f"## {sid}")
        desc = str(sc.get("description") or "").strip()
        if desc:
            lines.append(desc)
        bits = []
        if sc.get("framing"):
            bits.append(f"Framing: {sc['framing']}")
        if sc.get("movement"):
            bits.append(f"Movement: {sc['movement']}")
        if bits:
            lines.append("")
            lines.append("; ".join(str(b) for b in bits))
        caps = sc.get("captions") if isinstance(sc.get("captions"), dict) else {}
        if caps:
            lines.append("")
            if caps.get("zh"):
                lines.append(f"- zh: {caps['zh']}")
            if caps.get("en"):
                lines.append(f"- en: {caps['en']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_text_previews(job_id: str, arts: dict[str, Any],
                         gate: Optional[str] = None) -> dict[str, Any]:
    """Write script.md / scene_plan.md from inline JSON. `preview` is THIS gate's file only.

    Does not replace artifacts.script / artifacts.scene_plan dicts. Does not put .md in stills.
    A structured script dict still wins over a stray .md (never treat cost_report.md as script).
    """
    store.ensure_job(job_id)
    script = arts.get("script")
    if isinstance(script, dict):
        store.artifact_path(job_id, "script.md").write_text(
            _script_to_markdown(script), encoding="utf-8")
        arts["script_md"] = "script.md"
    elif isinstance(script, str) and Path(str(script)).name.lower() == "script.md":
        if store.artifact_path(job_id, "script.md").is_file():
            arts["script_md"] = "script.md"
    plan = arts.get("scene_plan")
    if isinstance(plan, dict):
        store.artifact_path(job_id, "scene_plan.md").write_text(
            _scene_plan_to_markdown(plan), encoding="utf-8")
        arts["scene_plan_md"] = "scene_plan.md"
    preview_name = None
    if gate == "approve_script" and arts.get("script_md"):
        preview_name = "script.md"
    elif gate == "approve_scene_plan" and arts.get("scene_plan_md"):
        preview_name = "scene_plan.md"
    if preview_name:
        arts["preview"] = [preview_name]
    else:
        arts.pop("preview", None)
    return arts


def _apply_previews(job_id: str, arts: dict[str, Any],
                    gate: Optional[str] = None) -> dict[str, Any]:
    """Resolve the single Dify `preview` slot for the current gate.

    Dual-surfaces the text .md copies (script.md / scene_plan.md) and picks ONE preview:
    script.md at approve_script, scene_plan.md at approve_scene_plan, and nothing at any
    other gate. The stills gate surfaces the individual images via artifacts.stills (no
    combined contact sheet). Inline JSON on artifacts.script / artifacts.scene_plan is left
    untouched (a structured dict still wins over a stray .md).
    """
    # Always write the .md copies (so downloads exist); no preview set here.
    _write_text_previews(job_id, arts, None)
    if gate == "approve_script" and arts.get("script_md"):
        arts["preview"] = ["script.md"]
    elif gate == "approve_scene_plan" and arts.get("scene_plan_md"):
        arts["preview"] = ["scene_plan.md"]
    else:
        arts.pop("preview", None)
    return arts


class BrandError(ValueError):
    """Raised by brand_job when the job cannot be branded. `.status_code` is the HTTP code."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _is_superseded_still(path: Any) -> bool:
    """True for archived revise leftovers (history/ or `*.pre-*` backups), not live stills."""
    p = Path(str(path))
    parts = {x.lower() for x in p.parts}
    if "history" in parts or "superseded-stills" in parts:
        return True
    return ".pre-" in p.name.lower()


def _artifact_files_ready(job_id: str, names: list[str]) -> bool:
    return bool(names) and all(store.artifact_path(job_id, n).is_file() for n in names)


def _brand_video_master(src: Path, dest: Path, prof: dict[str, Any]) -> None:
    """Overlay the BGC wordmark on a copy of the UGC master. Keeps audio."""
    from montage_svc.render import ffmpeg_ops as ff  # noqa: WPS433
    from montage_svc.render.overlays import scene_overlay  # noqa: WPS433

    w, h = ff.probe_size(src)
    dur = ff.probe_duration(src)
    with tempfile.TemporaryDirectory(prefix="dify-brand-") as td:
        png = Path(td) / "logo.png"
        drawn = scene_overlay(prof, w, h, None, [], png, with_logo=True)
        if not drawn or not png.is_file():
            raise BrandError("BGC logo overlay produced no image", 500)
        # Cover the whole cut (duration + slack) so the last frames keep the mark.
        try:
            ff.overlay_timed(src, [(png, 0.0, max(dur + 1.0, 0.05))], dest)
        except ff.RenderError as e:
            raise BrandError(f"video brand overlay failed: {e}", 500) from e


def _open_brand_gate(state: dict[str, Any]) -> dict[str, Any]:
    """Pause for BGC overlay choice. UGC artifacts are already on the job."""
    arts = dict(state.get("artifacts") or {})
    arts.setdefault("branded", False)
    state.update(
        status="awaiting_human", stage="brand", gate="approve_brand",
        question="Apply the BGC wordmark to copies of the approved stills/final? "
                 "Approve to brand, skip to keep UGC, or revise to decide later. "
                 "Branding does not flow through animation.",
        artifacts=arts,
    )
    return state


def _mark_brand_done(state: dict[str, Any], resolved: str) -> dict[str, Any]:
    state["brand_resolved"] = resolved
    state.update(status="done", gate=None, question=None)
    return state


def _apply_brand(state: dict[str, Any], profile: str = "bgc") -> dict[str, Any]:
    """Stamp BGC copies of stills and/or the video master. Does not change status."""
    profile = (profile or "bgc").strip().lower()
    if profile != "bgc":
        raise BrandError("only profile 'bgc' is supported for the brand pass", 400)

    arts = dict(state.get("artifacts") or {})
    job_id = state["job_id"]
    stills = [_still_basename(n) for n in (arts.get("stills") or [])
              if not _is_superseded_still(n)]
    final_name = _still_basename(arts.get("final") or "")
    final_src = store.artifact_path(job_id, final_name) if final_name.endswith(".mp4") else None
    has_final = bool(final_src and final_src.is_file())
    branded_final_name = "final.bgc.mp4"
    branded_final_path = store.artifact_path(job_id, branded_final_name)

    if not stills and not has_final:
        raise BrandError("no stills or final to brand", 409)

    existing_stills = [_still_basename(n) for n in (arts.get("branded_stills") or [])]
    stills_ready = _artifact_files_ready(job_id, existing_stills)
    video_ready = has_final and branded_final_path.is_file()

    if (not stills or stills_ready) and (not has_final or video_ready):
        if stills_ready:
            arts["branded_stills"] = existing_stills
        if video_ready:
            arts["branded_final"] = branded_final_name
        arts["branded"] = True
        state["artifacts"] = arts
        return state

    _VENDOR = _ENGINE_ROOT / "vendor"
    os.environ.setdefault("MONTAGE_BRAND_DIR", str(_VENDOR / "brand"))
    os.environ.setdefault("MONTAGE_DATA_DIR", str(_VENDOR / "data"))
    if str(_VENDOR) not in sys.path:
        sys.path.insert(0, str(_VENDOR))
    from montage_svc.storage import ensure_profiles, load_profile  # noqa: WPS433
    from montage_svc.render.overlays import draw_logo  # noqa: WPS433
    from PIL import Image

    ensure_profiles()
    prof = load_profile("bgc")

    if stills and not stills_ready:
        branded: list[str] = []
        for name in stills:
            src = store.artifact_path(job_id, name)
            if not src.is_file():
                raise BrandError(f"still {name!r} is missing from the job store", 409)
            out_name = f"{Path(name).stem}.bgc.png"
            img = Image.open(src).convert("RGBA")
            draw_logo(img, prof)
            img.save(store.artifact_path(job_id, out_name))
            branded.append(out_name)
        arts["branded_stills"] = branded
    elif stills_ready:
        arts["branded_stills"] = existing_stills

    if has_final and not video_ready:
        assert final_src is not None
        _brand_video_master(final_src, branded_final_path, prof)
        arts["branded_final"] = branded_final_name
    elif video_ready:
        arts["branded_final"] = branded_final_name

    arts["branded"] = True
    state["artifacts"] = arts
    return state


def _resolve_brand_gate(state: dict[str, Any], decision: str) -> dict[str, Any]:
    """approve = stamp then done; skip = UGC done; revise = stay at approve_brand."""
    decision = (decision or "approve").strip().lower()
    if decision == "revise":
        return _open_brand_gate(state)
    if decision == "skip":
        arts = dict(state.get("artifacts") or {})
        arts["branded"] = False
        state["artifacts"] = arts
        return _mark_brand_done(state, "skipped")
    if decision == "approve":
        _apply_brand(state)
        return _mark_brand_done(state, "applied")
    raise ValueError(f"approve_brand expects approve|skip|revise, got {decision!r}")


def brand_job(state: dict[str, Any], profile: str = "bgc") -> dict[str, Any]:
    """Stamp the BGC wordmark onto approved stills and/or the video master.

    Job must be `done` (after skip, or a second pass). At `approve_brand` use
    POST /respond instead. Idempotent per output. UGC originals stay.
    """
    if state.get("gate") == "approve_brand":
        raise BrandError("at approve_brand use POST /respond (approve|skip|revise), not /brand", 409)
    if state.get("status") != "done":
        raise BrandError(f"job is {state.get('status')!r}, not done — finish the brand gate first", 409)
    return _apply_brand(state, profile)


# Placeholder per-asset Higgsfield credits the MockRunner uses to exercise budget enforcement
# (the real runner uses each asset's actual get_cost credits from the manifest).
_MOCK_STILL_CREDITS = 4
_MOCK_CLIP_CREDITS = 14

_MIGRATION_MSG = (
    "This job was created under the previous storyboard-stills flow (gate "
    "'{gate}'), which no longer exists after the scene_plan revert to the upstream "
    "text-plan + assets architecture. The job is still readable, but cannot be resumed. "
    "Please start a new job."
)


def _legacy_migration(state: dict[str, Any]) -> dict[str, Any]:
    """A saved job sitting at a removed gate: leave it readable, fail with a clear message."""
    state.update(status="failed", gate=None,
                 question=_MIGRATION_MSG.format(gate=state.get("gate")))
    return state


class Runner:
    """Interface. advance() takes the job state + an optional human response and returns
    the updated state, stopping at the next gate (or done)."""

    def start(self, state: dict[str, Any]) -> dict[str, Any]:  # noqa: D401
        raise NotImplementedError

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# MockRunner — testable end to end with no LLM / no Higgsfield
# ---------------------------------------------------------------------------

class MockRunner(Runner):
    def start(self, state: dict[str, Any]) -> dict[str, Any]:
        state.setdefault("pipeline", _pipeline_of(state))
        if _is_image(state):
            return self._do_scene_plan(state, {})
        if not _script_gate_enabled(state):
            self._do_script(state, {})
            return self._do_scene_plan(state, {})
        return self._do_script(state, {})

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        decision = (response or {}).get("decision", "approve")
        gate = state.get("gate")

        if gate in _LEGACY_GATES:
            return _legacy_migration(state)

        # a raised budget cap may accompany any response — persist it onto the job options
        new_cap = (response or {}).get("max_higgsfield_credits")
        if new_cap is not None:
            state.setdefault("options", {})["max_higgsfield_credits"] = new_cap

        # cancel: only meaningful at the budget hold — stop the job, spend nothing more
        if decision == "cancel":
            state.update(status="failed", gate=None,
                         question="Job cancelled at the budget gate — no further Higgsfield credits spent.")
            return state

        if gate == "approve_brand":
            return _resolve_brand_gate(state, decision)

        # revise: regenerate the CURRENT gate's artifact, stay at the same gate
        if decision == "revise":
            regen = {
                "approve_script": self._do_script,
                "approve_scene_plan": self._do_scene_plan,
                "approve_stills": self._do_stills,
                "approve_motion_sample": self._do_motion_sample,
                "approve_assets": self._do_assets,
                "budget_exceeded": self._do_assets,          # revise the requested generation to fit
                "approve_final": self._do_production,
            }.get(gate)
            if not regen:
                raise ValueError(f"cannot revise from gate {gate!r}")
            return regen(state, response)

        # approve: advance to the next stage/gate
        if gate == "budget_exceeded":
            return self._do_assets(state, response)          # cap raised → retry the batch (re-checks)
        if gate == "approve_script":
            return self._do_scene_plan(state, response)
        if gate == "approve_scene_plan":
            return self._do_stills(state, response)      # assets phase 1: stills only
        if gate == "approve_stills":
            if _is_stills_terminal(state):
                return self._finish_stills_job(state)   # terminal — no motion / clips / compose
            # assets phase 2: one motion sample first (if enabled), else straight to full media
            if _motion_sample_enabled(state):
                return self._do_motion_sample(state, response)
            return self._do_assets(state, response)
        if gate == "approve_motion_sample":
            return self._do_assets(state, response)       # assets phase 3: all clips + audio + manifest
        if gate == "approve_assets":
            return self._do_production(state, response)
        if gate == "approve_final":
            return _open_brand_gate(state)
        raise ValueError(f"cannot resume from gate {gate!r}")

    # --- GATE 1: script ----------------------------------------------------
    def _do_script(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        brief = state.get("brief", "")
        note = (response or {}).get("answer")
        explain = "Explain the tip." + (f" (revised: {note})" if note else "")
        script_obj = {
            "version": "1.0",
            "title": "Script (mock)",
            "total_duration_seconds": 9.0,
            "sections": [
                {"id": "s1", "label": "Open", "text": "Open on the Panda mascot.",
                 "start_seconds": 0, "end_seconds": 3,
                 "speaker_directions": f"Brief: {brief}"},
                {"id": "s2", "label": "Explain", "text": explain,
                 "start_seconds": 3, "end_seconds": 6},
                {"id": "s3", "label": "CTA", "text": "CTA.",
                 "start_seconds": 6, "end_seconds": 9},
            ],
        }
        arts = {**state.get("artifacts", {}), "script": script_obj}
        _write_text_previews(job_id, arts, "approve_script")
        state.update(
            stage="script", status="awaiting_human", gate="approve_script",
            question="Approve the script, or request a revision.",
            artifacts=arts,
        )
        return state

    # --- GATE 2: TEXT scene plan (no media generated here) -----------------
    def _do_scene_plan(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Produce ONLY a schema-valid, TEXT scene plan (upstream contract). No stills, no
        media — each scene declares required_assets the assets stage fulfils later."""
        job_id = state["job_id"]
        brief = state.get("brief", "")
        note = (response or {}).get("answer")
        stills_only = _is_stills_terminal(state)
        n = 1 if _is_image(state) else 3
        scenes = []
        for i in range(n):
            role = ("hook", "content", "cta")[i] if i < 3 else "content"
            if _is_image(state):
                role = "cta"
            asset = {"type": "image", "description": f"Panda keyframe for scene {i+1}",
                     "source": "generate"}
            scene = {
                "id": f"scene-{i+1}",
                "type": "generated",
                "description": (f"Scene {i+1} for: {brief}"
                                + (f" (revised: {note})" if note else "")),
                "start_seconds": float(i * 3),
                "end_seconds": float((i + 1) * 3),
                "framing": "medium",
                "movement": "static",
                "narrative_role": {"hook": "establish_context", "content": "deliver_payload",
                                   "cta": "call_to_action"}[role],
                "required_assets": [asset] if stills_only else [
                    asset,
                    {"type": "video", "description": f"Motion clip for scene {i+1}",
                     "source": "generate"},
                ],
            }
            if stills_only:
                scene["captions"] = {
                    "en": f"Slide {i+1} {role}",
                    "zh": f"第{i+1}页 {role}",
                }
            scenes.append(scene)
        scene_plan = {"version": "1.0", "scenes": scenes}
        if stills_only:
            scene_plan["metadata"] = {"aspect_ratio": _stills_aspect(state=state)}
        store.artifact_path(job_id, "scene_plan.json").write_text(
            json.dumps(scene_plan, indent=2), encoding="utf-8")
        # Surface the plan inline (dict) so Dify can review it as TEXT — no stills here.
        # Also write scene_plan.md and set preview to that file (current gate only).
        arts = {k: v for k, v in state.get("artifacts", {}).items() if k != "stills"}
        arts["scene_plan"] = scene_plan
        _write_text_previews(job_id, arts, "approve_scene_plan")
        state.update(
            stage="scene_plan", status="awaiting_human", gate="approve_scene_plan",
            question="Approve the scene plan (text), or request a revision.",
            artifacts=arts,
        )
        return state

    # --- GATE 3: assets PHASE 1 — stills only (cheap, pre-video cost gate) --
    def _do_stills(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Generate STILLS ONLY from the approved scene plan — one per scene, NO video yet.
        This is the cost/visual checkpoint: approve the look (on-model panda, composition)
        before any expensive image->video spend. Real gen is Higgsfield image generation; the
        mock makes placeholder stills. On the box this checkpoint carries
        partial_progress.phase='stills' so the launcher surfaces it as approve_stills.

        Revise at this gate is dual-mode: `mode=fresh` regenerates placeholders; `mode=edit`
        draws the note onto copies of the existing PNGs (keeps size/colors). Honor `shots`."""
        job_id = state["job_id"]
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        n = len(scenes) or 3
        existing = [_still_basename(x) for x in (state.get("artifacts", {}).get("stills") or [])]
        decision = (response or {}).get("decision", "approve")
        if decision == "revise" and existing:
            indices = _revise_shot_indices(response, len(existing))
            if _stills_revise_mode(response) == "edit":
                stills = self._edit_existing_stills(
                    job_id, existing, indices, (response or {}).get("answer") or "")
            else:
                stills = self._placeholder_stills(
                    job_id, max(n, len(existing)), state=state,
                    indices=indices, existing=existing)
        else:
            stills = self._placeholder_stills(job_id, n, state=state)
        # entering the stills phase drops any clips/manifest from a prior pass
        arts = {k: v for k, v in state.get("artifacts", {}).items()
                if k not in ("clips", "asset_manifest")}
        arts["stills"] = stills
        _apply_previews(job_id, arts, "approve_stills")
        state.update(
            stage="assets", status="awaiting_human", gate="approve_stills",
            question="Approve the stills (one per scene) — on-model and well-composed? — or "
                     "request a revision. No video is generated until the stills are approved.",
            artifacts=arts,
        )
        return state

    # --- GATE 3.5: assets PHASE 2 — one MOTION SAMPLE (approve motion before batching) --
    def _do_motion_sample(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Animate ONE hero still into a sample clip so the reviewer approves the motion/animation
        feel BEFORE all clips are generated (the biggest cost/time gate). Real gen is one
        Higgsfield image_to_video; the mock renders a short clip from the first still. On the box
        this checkpoint carries partial_progress.phase='motion_sample' -> approve_motion_sample."""
        job_id = state["job_id"]
        stills = list(state.get("artifacts", {}).get("stills", []))
        if not stills:
            stills = self._placeholder_stills(job_id, 3)
        hero = stills[0]                                          # hero = scene 1 in the mock
        sample_name = "motion_sample.mp4"
        self._render_clean([str(store.artifact_path(job_id, hero))],
                           str(store.artifact_path(job_id, sample_name)))
        # entering the motion-sample phase drops any full clips/manifest from a prior pass
        arts = {k: v for k, v in state.get("artifacts", {}).items()
                if k not in ("clips", "asset_manifest")}
        arts["stills"] = stills
        arts["motion_sample"] = sample_name
        _apply_previews(job_id, arts, "approve_motion_sample")
        state.update(
            stage="assets", status="awaiting_human", gate="approve_motion_sample",
            question="Approve the MOTION on this one sample clip (camera, animation, how the panda "
                     "moves) before all clips are generated — or request a revision of the motion.",
            artifacts=arts,
        )
        return state

    # --- GATE 4: assets PHASE 3 — animate approved stills + audio + manifest
    def _do_assets(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Animate the APPROVED stills into motion clips (and, on the box, voice/music) and
        record everything in asset_manifest. Real gen is the Higgsfield MCP (image_to_video) +
        ElevenLabs; the mock renders a short clip per still. Reviewer approves the set or revises
        specific shots (response.shots) — only those regenerate."""
        job_id = state["job_id"]
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        stills = list(state.get("artifacts", {}).get("stills", []))
        n = len(stills) or (len(scenes) or 3)
        if len(stills) != n:                                     # safety: stills come from GATE 3
            stills = self._placeholder_stills(job_id, n)
        only = set((response or {}).get("shots", []))  # optional: regenerate specific shots

        # HARD PRE-GENERATION BUDGET BLOCK: if animating the batch would push cumulative Higgsfield
        # spend past the approved credit cap, generate NOTHING and pause for a human decision.
        cap = _budget_cap(state)
        if cap is not None:
            spent = len(stills) * _MOCK_STILL_CREDITS \
                + (_MOCK_CLIP_CREDITS if state.get("artifacts", {}).get("motion_sample") else 0)
            requested = n * _MOCK_CLIP_CREDITS               # clips about to be animated
            if spent + requested > cap:
                return self._budget_hold(state, cap, spent, requested)

        clips = list(state.get("artifacts", {}).get("clips", []))
        if len(clips) != n:
            clips = [None] * n
        for i in range(n):
            if only and i not in only and clips[i]:
                continue  # keep already-approved shot
            clip_name = f"clip_{i:02d}.mp4"
            self._render_clean(
                [str(store.artifact_path(job_id, stills[i]))],
                str(store.artifact_path(job_id, clip_name)),
            )
            clips[i] = clip_name

        assets = []
        for i in range(n):
            sid = scenes[i]["id"] if i < len(scenes) else f"scene-{i+1}"
            assets.append({"id": f"img-{i:02d}", "type": "image", "path": stills[i],
                           "source_tool": "mock_still", "scene_id": sid, "provider": "higgsfield",
                           "credits": _MOCK_STILL_CREDITS, "credits_source": "estimated"})
            assets.append({"id": f"vid-{i:02d}", "type": "video", "path": clips[i],
                           "source_tool": "mock_clip", "scene_id": sid, "provider": "higgsfield",
                           "credits": _MOCK_CLIP_CREDITS, "credits_source": "estimated"})
        asset_manifest = {"version": "1.0", "assets": assets, "total_cost_usd": 0.0}
        store.artifact_path(job_id, "asset_manifest.json").write_text(
            json.dumps(asset_manifest, indent=2), encoding="utf-8")

        arts = {**state.get("artifacts", {}), "stills": stills, "clips": clips,
                "asset_manifest": asset_manifest}
        _apply_previews(job_id, arts, "approve_assets")
        state.update(
            stage="assets", status="awaiting_human", gate="approve_assets",
            question="Approve the generated media (clips + audio), or request revision of "
                     "specific shots (send {\"decision\":\"revise\",\"shots\":[i,...]}).",
            artifacts=arts,
        )
        return state

    # --- BUDGET HOLD: hard pre-generation block inside the assets lifecycle -
    def _budget_hold(self, state: dict[str, Any], cap: int, spent: int, requested: int) -> dict[str, Any]:
        """Nothing was generated. Pause and require the human to raise the cap, revise, or cancel."""
        projected = spent + requested
        arts = dict(state.get("artifacts") or {})
        _apply_previews(state["job_id"], arts, "budget_exceeded")
        state.update(
            stage="assets", status="awaiting_human", gate="budget_exceeded",
            question=(f"BUDGET HOLD — generating the requested clips would use ~{requested} more "
                      f"Higgsfield credits ({spent} already spent → ~{projected} total), over the "
                      f"approved cap of {cap}. NO clips were generated. Respond with one of: raise "
                      "the cap {\"decision\":\"approve\",\"max_higgsfield_credits\":<n>}; revise the "
                      "plan {\"decision\":\"revise\",\"answer\":\"…\"}; or cancel {\"decision\":\"cancel\"}."),
            artifacts=arts,
        )
        return state

    # --- edit/compose (no gates) -> GATE 4 clean master --------------------
    def _do_production(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        clips = state.get("artifacts", {}).get("clips", [])
        scene_paths = [str(store.artifact_path(job_id, c)) for c in clips]
        if not scene_paths:
            scene_paths = [self._placeholder_stills(job_id, 2)[0]]

        out = store.artifact_path(job_id, "final.mp4")
        self._render_clean(scene_paths, str(out))

        arts = {**state.get("artifacts", {}), "final": "final.mp4", "branded": False}
        _apply_previews(job_id, arts, "approve_final")
        state.update(
            stage="compose", status="awaiting_human", gate="approve_final",
            question="Approve the finished (unbranded) video, or request a revision. "
                     "Branding is the next gate and does not flow through animation.",
            artifacts=arts,
        )
        return state

    # --- helpers -----------------------------------------------------------
    def _finish_stills_job(self, state: dict[str, Any]) -> dict[str, Any]:
        """Approve stills on carousel/image: write image-only asset_manifest, then brand gate."""
        job_id = state["job_id"]
        stills = list(state.get("artifacts", {}).get("stills") or [])
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        assets = []
        for i, name in enumerate(stills):
            sid = scenes[i]["id"] if i < len(scenes) else f"scene-{i+1}"
            assets.append({"id": f"img-{i:02d}", "type": "image", "path": name,
                           "source_tool": "mock_still", "scene_id": sid, "provider": "higgsfield",
                           "credits": _MOCK_STILL_CREDITS, "credits_source": "estimated"})
        asset_manifest = {"version": "1.0", "assets": assets, "total_cost_usd": 0.0}
        store.artifact_path(job_id, "asset_manifest.json").write_text(
            json.dumps(asset_manifest, indent=2), encoding="utf-8")
        arts = {**state.get("artifacts", {}), "stills": stills,
                "asset_manifest": asset_manifest, "branded": False}
        _apply_previews(job_id, arts, None)
        state.update(
            artifacts=arts,
        )
        return _open_brand_gate(state)

    def _placeholder_stills(self, job_id: str, n: int,
                            state: Optional[dict[str, Any]] = None,
                            indices: Optional[list[int]] = None,
                            existing: Optional[list[str]] = None) -> list[str]:
        from PIL import Image, ImageDraw
        colors = [(11, 11, 11), (253, 197, 13), (30, 30, 30)]
        if state and _is_stills_terminal(state):
            size = _carousel_pixel_size(_stills_aspect(state=state))
        else:
            size = (1080, 1920)
        if existing:
            names = [_still_basename(x) for x in existing]
            while len(names) < n:
                names.append(f"still_{len(names):02d}.png")
        else:
            names = [f"still_{i:02d}.png" for i in range(n)]
        targets = list(range(len(names))) if indices is None else indices
        for i in targets:
            if not (0 <= i < len(names)):
                continue
            img = Image.new("RGB", size, colors[i % len(colors)])
            d = ImageDraw.Draw(img)
            d.text((60, size[1] // 2), f"Scene {i+1}", fill=(255, 255, 255))
            p = store.artifact_path(job_id, names[i])
            img.save(p)
            names[i] = p.name
        return names

    def _edit_existing_stills(self, job_id: str, names: list[str],
                              indices: list[int], note: str) -> list[str]:
        """Image-to-image mock: stamp the revision note onto copies of existing stills.
        Keeps original size and colors; only the flagged indices are rewritten."""
        from PIL import Image, ImageDraw
        out = [_still_basename(x) for x in names]
        for i in indices:
            if not (0 <= i < len(out)):
                continue
            p = store.artifact_path(job_id, out[i])
            img = Image.open(p).convert("RGB")
            w, h = img.size
            d = ImageDraw.Draw(img)
            bar_h = min(80, max(40, h // 16))
            d.rectangle([0, h - bar_h, w, h], fill=(0, 0, 0))
            d.text((20, h - bar_h + 10), f"EDIT: {(note or '')[:120]}", fill=(255, 255, 255))
            img.save(p)
        return out

    def _render_clean(self, scene_paths: list[str], out_path: str) -> None:
        """Real render via the folded panda_render tool (clean/ugc, no branding)."""
        if str(_ENGINE_ROOT) not in sys.path:
            sys.path.insert(0, str(_ENGINE_ROOT))
        from tools.video.panda_render import PandaRender
        res = PandaRender().execute({
            "scenes": [{"media_path": p, "duration_s": 2.5} for p in scene_paths],
            "fps": 30, "grade": "none",
            "output_path": out_path,
        })
        if not res.success:
            raise RuntimeError(f"panda_render failed: {getattr(res, 'error', '?')}")


# ---------------------------------------------------------------------------
# ClaudeCodeRunner — the EC2 path (real agent)
# ---------------------------------------------------------------------------

# OpenMontage stage  ->  launcher gate name (matches pipeline_defs/panda-video.yaml).
# NOTE: the `assets` stage is deliberately absent here — it surfaces TWO gates chosen by the
# checkpoint's partial_progress.phase: "stills" -> approve_stills (pre-video), else
# approve_assets (full media). See _sync() and _gate_stage().
_STAGE_GATE = {
    "script": "approve_script",
    "scene_plan": "approve_scene_plan",   # TEXT plan (no media)
    "compose": "approve_final",
}


class ClaudeCodeRunner(Runner):
    """Drives the REAL agent: Claude Code headless against the engine repo.

    Each start()/resume() runs the agent until it writes an `awaiting_human` checkpoint (or
    the pipeline finishes), then mirrors the checkpoint + artifacts into the launcher job
    store. OpenMontage's resume is checkpoint-based, so we don't depend on a CLI session —
    every leg is a fresh `claude -p` that reads the latest checkpoint and continues.

    Config (env):
      CLAUDE_BIN          claude CLI path (default "claude")
      CLAUDE_EXTRA_ARGS   extra CLI args, space-split (e.g. "--dangerously-skip-permissions")
      CLAUDE_TIMEOUT_S    per-leg timeout seconds (default 3600)
      PANDA_PIPELINE_TYPE pipeline manifest name (default "panda-video")
      OPENMONTAGE_PROJECTS_DIR  checkpoints/projects root (default engine/projects)
      LLM (OpenRouter): ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN inherited from process env

    >>> VERIFY ON THE BOX <<<  the exact `claude` flags, the agent's stop-at-gate behavior,
    and the artifact key/paths the panda-video skills emit (see _mirror_artifacts). The
    checkpoint calls below use the real lib/checkpoint API.
    """

    def __init__(self) -> None:
        if str(_ENGINE_ROOT) not in sys.path:
            sys.path.insert(0, str(_ENGINE_ROOT))
        from lib.paths import PROJECTS_DIR
        self._projects_dir = PROJECTS_DIR
        self._bin = os.environ.get("CLAUDE_BIN", "claude")
        self._extra = os.environ.get("CLAUDE_EXTRA_ARGS", "").split()
        self._timeout = int(os.environ.get("CLAUDE_TIMEOUT_S", "3600"))

    # -- lifecycle ----------------------------------------------------------
    def start(self, state: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        pipeline = _pipeline_of(state)
        state["pipeline"] = pipeline
        from lib import checkpoint as cp
        title_prefix = {"panda-carousel": "Panda carousel", "panda-image": "Panda image"}.get(
            pipeline, "Panda video")
        cp.init_project(job_id, title=(state.get("brief") or title_prefix)[:80],
                        pipeline_type=pipeline)
        start_label = "scene_plan" if pipeline == "panda-image" else "script"
        self._run_agent(self._start_prompt(job_id, state.get("brief", ""),
                                           state.get("options") or {}, pipeline),
                        job_id, start_label)
        state = self._sync(state)
        # Gate-collapse: options.gates omits script → auto-approve GATE 1 and continue.
        if (state.get("status") == "awaiting_human" and state.get("gate") == "approve_script"
                and not _script_gate_enabled(state)):
            self._approve_stage(job_id, "script", pipeline)
            self._run_agent(self._continue_prompt(job_id, pipeline), job_id, "scene_plan")
            state = self._sync(state)
        return state

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        if state.get("gate") in _LEGACY_GATES:
            return _legacy_migration(state)
        from lib import checkpoint as cp
        gate = state.get("gate")
        decision = (response or {}).get("decision", "approve")

        if gate == "approve_brand":
            return _resolve_brand_gate(state, decision)

        stage = self._gate_stage(gate)

        # BUDGET HOLD — the agent blocked a generation that would exceed max_higgsfield_credits.
        # The human must raise the cap, revise the requested generation, or cancel. Nothing was spent.
        if gate == "budget_exceeded":
            new_cap = (response or {}).get("max_higgsfield_credits")
            if new_cap is not None:                       # persist a raised cap for later legs
                state.setdefault("options", {})["max_higgsfield_credits"] = new_cap
            if decision == "cancel":
                state.update(status="failed", gate=None,
                             question="Job cancelled at the budget gate — no further Higgsfield credits spent.")
                return state
            if decision == "approve":
                self._run_agent(self._budget_raised_prompt(job_id, new_cap), job_id, "assets_media")
            else:  # revise — reduce/cheapen the requested generation to fit the cap
                self._run_agent(self._revise_prompt(
                    job_id, "assets (BUDGET HOLD — reduce or cheapen the requested Higgsfield generation "
                            "to fit the approved max_higgsfield_credits cap, re-check the budget hard-rule, "
                            "then continue)", response or {}),
                    job_id, "assets_revise")
            return self._sync(state)

        # Approving a within-assets phase (stills / motion sample) must NOT complete the assets
        # stage — it only unlocks the next phase WITHIN the same stage. Continue the agent from the
        # latest checkpoint so it does the next phase and stops again at the next assets sub-gate.
        if gate == "approve_stills":
            if decision != "approve":
                self._run_agent(self._revise_prompt(
                    job_id, "assets (STILLS phase — revise the flagged stills and stop again "
                            "at the stills gate; do NOT generate video yet)",
                    response or {}, state=state),
                    job_id, "stills_revise")
            elif _is_stills_terminal(state):
                # carousel / image are stills-terminal: complete assets and let _sync mark done
                self._approve_stage(job_id, "assets", _pipeline_of(state))
            elif _motion_sample_enabled(state):
                # one hero clip first — approve the motion before batching all clips
                self._run_agent(self._motion_sample_prompt(job_id), job_id, "motion_sample")
            else:
                # motion sample disabled: animate all approved stills straight away
                self._run_agent(self._stills_approved_prompt(job_id), job_id, "assets_media")
            return self._sync(state)

        if gate == "approve_motion_sample":
            if decision == "approve":
                self._run_agent(self._motion_approved_prompt(job_id), job_id, "assets_media")
            else:
                self._run_agent(self._revise_prompt(
                    job_id, "assets (MOTION SAMPLE phase — regenerate ONLY the sample clip per the "
                            "feedback, keep partial_progress.phase='motion_sample', do NOT batch the "
                            "remaining clips yet)", response or {}),
                    job_id, "motion_sample_revise")
            return self._sync(state)

        if decision == "approve":
            self._approve_stage(job_id, stage, _pipeline_of(state))
            # Approving the LAST gate finishes the job — there is no next stage to run, so do
            # NOT spin up a pointless agent turn; just report done.
            nxt = cp.get_next_stage(self._projects_dir, job_id, _pipeline_of(state))
            if nxt is None:
                return self._sync(state)
            prompt = self._continue_prompt(job_id, _pipeline_of(state))
            label = nxt
        else:
            prompt = self._revise_prompt(job_id, stage, response or {})
            label = f"{stage}_revise"
        self._run_agent(prompt, job_id, label)
        return self._sync(state)

    # -- agent invocation ---------------------------------------------------
    _TRANSIENT = ("connection closed", "api error", "overloaded", "rate limit",
                  "timeout", "timed out", " 500", " 502", " 503", " 529")

    def _run_agent(self, prompt: str, job_id: str = "", label: str = "") -> None:
        """Run `claude -p` once per leg. Retries on TRANSIENT API/network errors (a dropped
        connection shouldn't kill a long leg); the agent resumes from the latest checkpoint,
        so re-running the same prompt is safe. Records the leg's wall-time (all attempts) to
        the project's timing.jsonl for the per-project generation-time report."""
        import subprocess
        import time
        attempts = int(os.environ.get("CLAUDE_MAX_ATTEMPTS", "3"))
        last = ""
        started = time.monotonic()
        try:
            for i in range(attempts):
                proc = subprocess.run(
                    [self._bin, "-p", prompt, *self._extra],
                    cwd=str(_ENGINE_ROOT), capture_output=True, text=True, timeout=self._timeout,
                )
                if proc.returncode == 0:
                    return
                last = (proc.stderr or proc.stdout or "").strip()
                low = last.lower()
                transient = any(s in low for s in self._TRANSIENT)
                if not transient or i == attempts - 1:
                    break
                time.sleep(3 * (i + 1))  # brief backoff, then let the agent resume from checkpoint
            tail = last.splitlines()[-8:]
            raise RuntimeError("claude failed: " + " | ".join(tail))
        finally:
            self._record_timing(job_id, label, round(time.monotonic() - started, 2))

    def _record_timing(self, job_id: str, label: str, seconds: float) -> None:
        """Append one leg's wall-time to projects/{job}/artifacts/timing.jsonl. Never raises."""
        if not job_id:
            return
        try:
            from datetime import datetime, timezone
            adir = self._projects_dir / job_id / "artifacts"
            adir.mkdir(parents=True, exist_ok=True)
            entry = {"ts": datetime.now(timezone.utc).isoformat(),
                     "stage": label or "unknown", "seconds": seconds}
            with open(adir / "timing.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    # -- checkpoint <-> launcher state --------------------------------------
    def _sync(self, state: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        from lib import checkpoint as cp
        latest = cp.get_latest_checkpoint(self._projects_dir, job_id)
        if not latest:
            state.update(status="failed", question="agent produced no checkpoint")
            return state
        stage, status = latest.get("stage"), latest.get("status")
        arts = self._mirror_artifacts(job_id, latest.get("artifacts", {}))
        self._write_cost_report(job_id)     # refresh the report files (API-only; not attached to arts)
        if status == "failed":
            _apply_previews(job_id, arts, None)
            state.update(status="failed", stage=stage, gate=None,
                         question=latest.get("error", "stage failed"), artifacts=arts)
        elif status == "awaiting_human":
            if stage == "assets":
                # the assets stage pauses at: stills, one motion sample, a CONDITIONAL budget hold
                # (only when a generation would exceed max_higgsfield_credits), then full media.
                phase = (latest.get("partial_progress") or {}).get("phase")
                gate = {"stills": "approve_stills",
                        "motion_sample": "approve_motion_sample",
                        "budget_hold": "budget_exceeded"}.get(phase, "approve_assets")
            else:
                gate = _STAGE_GATE.get(stage, f"approve_{stage}")
            _apply_previews(job_id, arts, gate)
            state.update(status="awaiting_human", stage=stage, gate=gate,
                         question=f"Approve {stage}, or request a revision.", artifacts=arts)
        else:  # completed
            nxt = cp.get_next_stage(self._projects_dir, job_id, _pipeline_of(state))
            _apply_previews(job_id, arts, None)
            if nxt is None:
                state["artifacts"] = arts
                if state.get("brand_resolved"):
                    state.update(status="done", stage=stage, gate=None, question=None)
                    return state
                return _open_brand_gate(state)
            else:
                state.update(status="running", stage=stage, gate=None,
                             question=f"stage {stage} completed; next: {nxt}", artifacts=arts)
        return state

    def _mirror_artifacts(self, job_id: str, artifacts: dict[str, Any]) -> dict[str, Any]:
        """Copy the job's artifact files into the launcher store, grouped by kind for Dify.

        Real panda-video checkpoints describe assets in rich structured text — filenames are
        embedded in prose (e.g. "...keyframe for the Hook beat: scene-1.png"), NOT exposed as
        clean path fields — so walking checkpoint strings misses them. Instead mirror by
        scanning the engine's CANONICAL project layout on disk, which init_project always
        creates: assets/images -> stills, assets/video -> clips, renders -> final. We ALSO
        merge any explicit file paths the checkpoint does contain (by basename), so a pipeline
        that emits real paths still works. Raw checkpoint data stays under _checkpoint_artifacts.
        """
        store.ensure_job(job_id)
        proj = self._projects_dir / job_id
        out: dict[str, Any] = {}
        seen: set[str] = set()

        def _copy(p: Path) -> Optional[str]:
            try:
                if not p.is_file():
                    return None
            except OSError:
                return None
            if p.name not in seen:
                store.artifact_path(job_id, p.name).write_bytes(p.read_bytes())
                seen.add(p.name)
            return p.name

        def _scan(d: Path, exts: tuple[str, ...]) -> list[Path]:
            if not d.is_dir():
                return []
            return sorted(p for p in d.iterdir()
                          if p.is_file() and p.suffix.lower() in exts
                          and not p.name.startswith("_"))

        # explicit file paths embedded in the checkpoint (guarded against free text)
        def _paths_in(v: Any) -> list[Path]:
            found: list[Path] = []
            if isinstance(v, str):
                if v and "\n" not in v and len(v) <= 400:
                    p = Path(v) if Path(v).is_absolute() else proj / v
                    try:
                        if p.is_file():
                            found.append(p)
                    except OSError:
                        pass
            elif isinstance(v, dict):
                for vv in v.values():
                    found += _paths_in(vv)
            elif isinstance(v, list):
                for vv in v:
                    found += _paths_in(vv)
            return found

        # Defensive: a stray contact-sheet / storyboard image (e.g. from an older job) is not a
        # scene still — keep it out of stills. Archived revise leftovers (history/superseded-stills,
        # *.pre-*) must not surface as live stills or get /brand stamped.
        imgs = [p for p in _scan(proj / "assets" / "images", (".png", ".jpg", ".jpeg"))
                if "contact" not in p.name.lower() and "sheet" not in p.name.lower()
                and not _is_storyboard_name(p.name)
                and not _is_superseded_still(p)]
        vids = _scan(proj / "assets" / "video", (".mp4", ".mov", ".webm"))
        renders = _scan(proj / "renders", (".mp4", ".mov"))

        for p in _paths_in(artifacts):
            ext = p.suffix.lower()
            if (ext in (".png", ".jpg", ".jpeg") and p not in imgs and not _is_superseded_still(p)
                    and not _is_storyboard_name(p.name)):
                imgs.append(p)
            elif ext in (".mp4", ".mov", ".webm"):
                if p.parent.name == "renders" or "final" in p.name.lower():
                    if p not in renders:
                        renders.append(p)
                elif p not in vids:
                    vids.append(p)

        stills = [n for n in (_copy(p) for p in imgs) if n and not _is_storyboard_name(n)]
        clips = [n for n in (_copy(p) for p in vids) if n]
        final = None
        if renders:
            pref = [p for p in renders if "final" in p.name.lower()] or renders
            final = _copy(pref[-1])

        # Surface structured TEXT artifacts INLINE for review at their gates so the human in Dify
        # actually sees the content: script (dialogue/sections at GATE 1 approve_script),
        # scene_plan (text plan at GATE 2), asset_manifest (media inventory at GATE 3/4). Prefer
        # the checkpoint's inline dict; fall back to the on-disk artifacts/<name>.json the skills
        # write. Without this, the script gate would surface only a gate label and no script body,
        # so the reviewer has nothing to approve and the flow appears to skip the human.
        for aname in ("script", "scene_plan", "asset_manifest"):
            val = artifacts.get(aname)
            if not isinstance(val, dict):
                ap = proj / "artifacts" / f"{aname}.json"
                try:
                    val = json.loads(ap.read_text(encoding="utf-8")) if ap.is_file() else None
                except (OSError, ValueError):
                    val = None
            if isinstance(val, dict):
                out[aname] = val
        # stills checkpoint often omits scene_plan; reload it so it stays available for context
        if "scene_plan" not in out:
            sp_cp = proj / "checkpoint_scene_plan.json"
            try:
                data = json.loads(sp_cp.read_text(encoding="utf-8")) if sp_cp.is_file() else {}
                val = (data.get("artifacts") or {}).get("scene_plan")
                if isinstance(val, dict) and val.get("scenes"):
                    out["scene_plan"] = val
            except (OSError, ValueError):
                pass
        # Fallback ONLY when there is no structured script (e.g. a script written as a markdown)
        # file): surface it as a downloadable link so it's still reviewable. Match ONLY a file
        # literally named script.* — never another stray .md (e.g. cost_report.md), which would
        # otherwise be mislabeled as the script at gates whose checkpoint carries no script
        # artifact (regression seen at the scene_plan gate: artifacts.script -> cost_report.md).
        if "script" not in out:
            script_md = [p for p in _scan(proj / "artifacts", (".md",))
                         if p.stem.lower() == "script"]
            if script_md:
                n = _copy(script_md[-1])
                if n:
                    out["script"] = n
        if stills:
            out["stills"] = stills
        if clips:
            out["clips"] = clips
        if final:
            out["final"] = final
            out["branded"] = False
        out["_checkpoint_artifacts"] = artifacts  # raw non-file data for Dify context
        # Dual-surface: keep inline JSON, also write .md copies (preview key is set in _sync
        # once the gate is known so Dify's file slot shows the current text gate only).
        _write_text_previews(job_id, out, gate=None)
        return out

    def _write_cost_report(self, job_id: str) -> None:
        """Build the per-project cost/time report (Higgsfield credits, ElevenLabs usage,
        generation time) and mirror it into the job store so it's available ON DEMAND via
        `GET /jobs/{id}/cost` and the downloadable `cost_report.md` artifact. It is deliberately
        NOT attached to the polled job state — cost is API-only, not injected into every gate
        response. Non-fatal: a report failure must never break a run."""
        try:
            from lib import cost_report as cr
            cr.write_report(job_id)
        except Exception:  # noqa: BLE001 — the report must never break a run
            return
        try:
            proj_art = self._projects_dir / job_id / "artifacts"
            for name in ("cost_report.md", "cost_report.json"):
                src = proj_art / name
                if src.is_file():
                    store.artifact_path(job_id, name).write_bytes(src.read_bytes())
        except OSError:
            pass

    # -- approvals + prompts ------------------------------------------------
    def _gate_stage(self, gate: Optional[str]) -> Optional[str]:
        if gate in ("approve_stills", "approve_motion_sample", "budget_exceeded", "approve_assets"):
            return "assets"                 # all are pauses of the single assets stage
        if gate == "approve_brand":
            return "brand"
        for s, g in _STAGE_GATE.items():
            if g == gate:
                return s
        return None

    def _approve_stage(self, job_id: str, stage: Optional[str],
                       pipeline_type: Optional[str] = None) -> None:
        if not stage:
            return
        from lib import checkpoint as cp
        existing = cp.read_checkpoint(self._projects_dir, job_id, stage) or {}
        cp.write_checkpoint(
            self._projects_dir, job_id, stage, "completed",
            existing.get("artifacts", {}), pipeline_type=pipeline_type or _DEFAULT_PIPELINE,
            human_approval_required=True, human_approved=True,
        )

    def _start_prompt(self, job_id: str, brief: str, options: Optional[dict[str, Any]] = None,
                      pipeline: str = "panda-video") -> str:
        options = options or {}
        if pipeline == "panda-carousel":
            return self._carousel_start_prompt(job_id, brief, options)
        if pipeline == "panda-image":
            return self._image_start_prompt(job_id, brief, options)
        lang = str(options.get("language", "en")).lower()
        narrator = str(options.get("narrator", "panda")).lower()
        voice_id = options.get("voice_id")            # explicit override from Dify
        music = options.get("music", True)            # BGM: mood string, True (default bed), or False
        runtime = str(options.get("render_runtime", "auto")).lower()  # auto|ffmpeg|remotion|hyperframes
        motion_sample = str(options.get("motion_sample", True)).lower() \
            not in ("false", "0", "no", "off", "")    # one-clip motion cost gate (default on)
        cap = _budget_cap({"options": options})       # max_higgsfield_credits, or None (no cap)

        if cap is not None:
            budget_line = (
                f"BUDGET — HARD CAP of {cap} Higgsfield credits for this project. Before ANY "
                "Higgsfield generation (stills, motion sample, or clips), follow the BUDGET HARD RULE "
                "in skills/meta/higgsfield-mcp-bridge.md: sum the credits already recorded in "
                "asset_manifest PLUS the get_cost of the batch you are about to generate; if that "
                f"total would exceed {cap}, DO NOT call the generation tool — write the assets "
                "checkpoint status='awaiting_human' with partial_progress={\"phase\":\"budget_hold\"} "
                "(include the cap, spent, requested, projected credits) and STOP for a human decision.")
        else:
            budget_line = ("BUDGET — no credit cap set for this job (max_higgsfield_credits unset). "
                           "Still record each asset's get_cost credits in asset_manifest.")

        if runtime in ("ffmpeg", "remotion", "hyperframes"):
            runtime_line = (
                f"RENDER RUNTIME — the job requests render_runtime='{runtime}'. Set "
                f"edit_decisions.render_runtime='{runtime}' and route compose accordingly "
                "(ffmpeg->panda_render, remotion/hyperframes->video_compose). If that runtime "
                "is unavailable on this machine, STOP and escalate — do NOT silently fall back.")
        else:
            runtime_line = (
                "RENDER RUNTIME — 'auto': choose render_runtime per the decision matrix in "
                "skills/pipelines/panda-video/compose-director.md AND the actual on-box "
                "availability (check via video_compose). Default to 'ffmpeg' (panda_render) for "
                "character-mascot clips; pick 'remotion'/'hyperframes' only when the brief needs "
                "React/HTML motion graphics. Record the choice in edit_decisions.render_runtime "
                "and log a render_runtime_selection decision.")

        if voice_id:
            voice_line = (f"VOICE — use ElevenLabs voice_id='{voice_id}' (explicit override from "
                          "the job). ")
        else:
            voice_line = ("VOICE — use ElevenLabs with the voice_id from config/panda-elements.json "
                          f"`voices` matching narrator='{narrator}' and language='{lang}'. ")
        voice_line += ("Only if ElevenLabs is truly unavailable, fall back to Higgsfield audio and "
                       "record that decision.")

        if music is False or str(music).lower() in ("false", "none", "no", "off"):
            music_line = "MUSIC — do NOT add a background music bed for this job."
        else:
            mood = "" if music is True else f" Mood/brief: {music}."
            music_line = ("MUSIC — add a background music bed via the `music_gen` tool "
                          f"(ElevenLabs Music, same ELEVENLABS_API_KEY).{mood} Keep it under the VO.")

        return (
            f"Run the `{pipeline}` pipeline to produce a video.\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    narrator: {narrator}\n\n"
            "BRAND — MANDATORY, do NOT improvise: read config/panda-elements.json and USE its "
            "Higgsfield reference Element IDs for character consistency — the panda Element for "
            "every panda shot, the customer Element for the customer. Never invent a new panda.\n"
            f"{voice_line}\n{music_line}\n{runtime_line}\n{budget_line}\n\n"
            "Follow AGENT_GUIDE.md and skills/meta/checkpoint-protocol.md. Execute stages in "
            "order. At every stage whose manifest sets human_approval_default: true, write the "
            "checkpoint with status='awaiting_human' and STOP (end your turn) — do NOT "
            "self-approve.\n"
            "PIPELINE SHAPE: the `scene_plan` stage produces ONLY a structured TEXT plan — NO "
            "media, NO generation tools. The `assets` stage then runs in human-reviewed phases "
            "(cost gates):\n"
            + self._assets_phases_text(motion_sample) +
            "Finally compose the approved assets with the `panda_render` tool. Stop at the first gate."
        )

    def _carousel_start_prompt(self, job_id: str, brief: str, options: dict[str, Any]) -> str:
        lang = str(options.get("language", "en")).lower()
        ratio = _carousel_aspect(options)
        cap = _budget_cap({"options": options})
        auto_script = not _script_gate_enabled({"options": options})
        if cap is not None:
            budget_line = (
                f"BUDGET — HARD CAP of {cap} Higgsfield credits. Before ANY Higgsfield still "
                "generation, follow the BUDGET HARD RULE in skills/meta/higgsfield-mcp-bridge.md. "
                f"If spent + get_cost would exceed {cap}, write the assets checkpoint "
                "status='awaiting_human' with partial_progress={{\"phase\":\"budget_hold\"}} and STOP.")
        else:
            budget_line = ("BUDGET — no credit cap set. Still record each still's get_cost credits "
                           "in asset_manifest.")
        script_line = (
            "SCRIPT GATE — this job auto-approves script: write the script checkpoint "
            "status='completed' with human_approved=True, log decision_log category="
            "'approval_policy' (auto-approved by job option), and continue to scene_plan in "
            "the SAME turn."
            if auto_script else
            "SCRIPT GATE — write script status='awaiting_human' and STOP. Do NOT self-approve."
        )
        return (
            f"Run the `panda-carousel` pipeline to produce a STILLS-ONLY social carousel "
            f"(NOT a video).\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    aspect_ratio: {ratio}\n\n"
            "BRAND — MANDATORY: read config/panda-elements.json and USE its Higgsfield reference "
            "Element IDs — the panda Element for every panda slide, the customer Element for the "
            "customer. Never invent a new panda. Look: styles/panda.yaml.\n"
            f"{budget_line}\n{script_line}\n\n"
            "Follow AGENT_GUIDE.md, pipeline_defs/panda-carousel.yaml, and "
            "skills/pipelines/panda-carousel/*-director.md. Execute stages in order.\n"
            "PIPELINE SHAPE: idea (internal, no gate) → script (GATE 1) → scene_plan TEXT "
            "(GATE 2) → assets STILLS ONLY (GATE 3) → DONE.\n"
            "  - scene_plan: one scene per slide, bilingual captions.zh/en, required_assets are "
            "images only. Set metadata.aspect_ratio to the job option "
            f"'{ratio}' (caller-set; default 4:5). Pass that same ratio to generate_image.\n"
            "  - assets: generate ONLY stills via Higgsfield generate_image at that aspect ratio. "
            "Bake primary-language copy into each still. Write asset_manifest (images only) with "
            "per-still credits. Checkpoint status='awaiting_human' AND "
            "partial_progress={\"phase\":\"stills\"} and STOP.\n"
            "Do NOT generate motion clips, TTS, music, edit_decisions, or a compose/render. "
            "Do NOT brand the stills (no wordmark overlay) — branding is a later POST /brand. "
            "Stop at the first human_approval gate."
        )

    def _image_start_prompt(self, job_id: str, brief: str, options: dict[str, Any]) -> str:
        lang = str(options.get("language", "en")).lower()
        ratio = _stills_aspect(options, pipeline="panda-image")
        cap = _budget_cap({"options": options})
        if cap is not None:
            budget_line = (
                f"BUDGET — HARD CAP of {cap} Higgsfield credits. Before ANY Higgsfield still "
                "generation, follow the BUDGET HARD RULE in skills/meta/higgsfield-mcp-bridge.md. "
                f"If spent + get_cost would exceed {cap}, write the assets checkpoint "
                "status='awaiting_human' with partial_progress={{\"phase\":\"budget_hold\"}} and STOP.")
        else:
            budget_line = ("BUDGET — no credit cap set. Still record the still's get_cost credits "
                           "in asset_manifest.")
        return (
            f"Run the `panda-image` pipeline to produce ONE STILLS-ONLY social image "
            f"(NOT a video, NOT a carousel).\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    aspect_ratio: {ratio}\n\n"
            "BRAND — MANDATORY: read config/panda-elements.json and USE its Higgsfield reference "
            "Element IDs — the panda Element for every panda shot, the customer Element for the "
            "customer. Never invent a new panda. Look: styles/panda.yaml.\n"
            f"{budget_line}\n\n"
            "Follow AGENT_GUIDE.md, pipeline_defs/panda-image.yaml, and "
            "skills/pipelines/panda-image/*-director.md. Execute stages in order.\n"
            "PIPELINE SHAPE: idea (internal, no gate) → scene_plan TEXT (GATE 1) → "
            "assets ONE STILL (GATE 2) → DONE. There is NO script stage.\n"
            "  - scene_plan: exactly ONE scene, bilingual captions.zh/en, required_assets is "
            "one image only. Set metadata.aspect_ratio to the job option "
            f"'{ratio}' (caller-set; default 1:1). Pass that same ratio to generate_image.\n"
            "  - assets: generate ONE still via Higgsfield generate_image at that aspect ratio. "
            "Bake primary-language copy into the still. Write asset_manifest (images only) with "
            "credits. Checkpoint status='awaiting_human' AND "
            "partial_progress={\"phase\":\"stills\"} and STOP.\n"
            "Do NOT generate motion clips, TTS, music, edit_decisions, or a compose/render. "
            "Do NOT brand the still (no wordmark overlay) — branding is a later POST /brand. "
            "Stop at the first human_approval gate."
        )

    def _assets_phases_text(self, motion_sample: bool) -> str:
        stills = (
            "  PHASE 1 (stills): generate ONLY the stills — one per scene — via the Higgsfield "
            "MCP bridge (skills/meta/higgsfield-mcp-bridge.md), then write the assets checkpoint "
            "with status='awaiting_human' AND partial_progress={\"phase\":\"stills\"} and STOP. "
            "Do NOT generate any video yet.\n")
        if motion_sample:
            return stills + (
                "  PHASE 2 (motion sample): only after the stills are approved, animate ONE "
                "representative HERO still into a SINGLE sample clip (image_to_video) so the "
                "motion/animation can be approved before the full batch. Write the assets "
                "checkpoint status='awaiting_human' AND partial_progress={\"phase\":"
                "\"motion_sample\"} and STOP — no other clips, no audio yet.\n"
                "  PHASE 3 (media): only after the motion sample is approved, animate the "
                "REMAINING stills using the SAME motion approach + generate narration/music "
                "(ElevenLabs), record everything (incl. the sample) in asset_manifest with "
                "per-asset Higgsfield credits, then write the assets checkpoint "
                "status='awaiting_human' (no phase marker) and STOP.\n")
        return stills + (
            "  PHASE 2 (media): only after the stills are approved, animate the approved stills "
            "into motion clips (image_to_video) + generate narration/music (ElevenLabs), record "
            "everything in asset_manifest, then write the assets checkpoint status='awaiting_human' "
            "(no 'stills' phase marker) and STOP.\n")

    def _continue_prompt(self, job_id: str, pipeline: Optional[str] = None) -> str:
        p = pipeline or _DEFAULT_PIPELINE
        extra = ""
        if p == "panda-carousel":
            extra = (" This is a stills-only carousel — do NOT generate video, TTS, music, or "
                     "compose. After stills the pipeline is complete.")
        elif p == "panda-image":
            extra = (" This is a SINGLE still — do NOT generate video, TTS, music, or compose. "
                     "There is no script stage. After the one still the pipeline is complete.")
        return (
            f"Continue the `{p}` pipeline for project_id: {job_id}. Read the latest "
            "checkpoint, proceed from the next stage, and STOP at the next human_approval gate "
            f"(status='awaiting_human', end your turn). If the pipeline is complete, finish.{extra}"
        )

    def _stills_approved_prompt(self, job_id: str) -> str:
        return (
            f"For project_id: {job_id}, the STILLS phase of the `assets` stage is APPROVED. Do NOT "
            "mark the assets stage completed yet. Animate the approved stills into motion clips "
            "(image_to_video via the Higgsfield MCP bridge) and generate narration/music "
            "(ElevenLabs), record every file in asset_manifest, then rewrite the assets checkpoint "
            "with status='awaiting_human' (WITHOUT the 'stills' phase marker) and STOP for the "
            "full media approval."
        )

    def _budget_raised_prompt(self, job_id: str, new_cap: Any) -> str:
        cap_txt = f" The approved cap is now {new_cap} Higgsfield credits." if new_cap is not None else ""
        return (
            f"For project_id: {job_id}, the BUDGET HOLD is cleared — the human authorized continuing."
            f"{cap_txt} Resume the Higgsfield generation that was blocked, RE-CHECKING the budget "
            "hard-rule (spent + get_cost vs the cap) before generating. If it now fits, generate the "
            "batch, record each asset's credits in asset_manifest, and stop at the next assets gate. "
            "If it STILL exceeds the cap, do NOT generate — write the budget_hold checkpoint again."
        )

    def _motion_sample_prompt(self, job_id: str) -> str:
        return (
            f"For project_id: {job_id}, the STILLS phase of the `assets` stage is APPROVED. Do NOT "
            "mark the assets stage completed yet, and do NOT batch-generate all clips. Animate ONE "
            "representative HERO still (the most important scene, else scene 1) into a single MOTION "
            "SAMPLE clip (image_to_video via the Higgsfield MCP bridge) so the reviewer can approve "
            "the motion/animation feel before committing to the full batch. Record the sample's "
            "Higgsfield credits on that asset (credits + credits_source='actual'). Then rewrite the "
            "assets checkpoint with status='awaiting_human' AND partial_progress={\"phase\":"
            "\"motion_sample\"} and STOP. Generate NO other clips and NO audio yet."
        )

    def _motion_approved_prompt(self, job_id: str) -> str:
        return (
            f"For project_id: {job_id}, the MOTION SAMPLE is APPROVED. Do NOT mark the assets stage "
            "completed yet. Animate the REMAINING approved stills into motion clips using the SAME "
            "motion approach (model + motion params) as the approved sample, then generate "
            "narration/music (ElevenLabs). Record every file (incl. the already-approved sample) in "
            "asset_manifest with per-asset Higgsfield credits, then rewrite the assets checkpoint "
            "with status='awaiting_human' (WITHOUT any phase marker) and STOP for the full media "
            "approval."
        )

    def _revise_prompt(self, job_id: str, stage: Optional[str], response: dict[str, Any],
                       state: Optional[dict[str, Any]] = None) -> str:
        note = response.get("answer", "(no note)")
        shots = response.get("shots") or []
        shot_txt = f" Regenerate only shots {shots}." if shots else ""
        extra = ""
        is_stills = ((state or {}).get("gate") == "approve_stills"
                     or "STILLS" in str(stage or "").upper())
        if is_stills:
            mode = _stills_revise_mode(response)
            mode_label = "EDIT" if mode == "edit" else "FRESH"
            paths = _still_abs_paths(
                job_id, state, shots, getattr(self, "_projects_dir", None))
            path_txt = (f" Current still absolute paths: {paths}." if paths else
                        f" Current stills live under projects/{job_id}/assets/images/ "
                        "and the launcher artifacts dir.")
            shot_txt = (f" Flagged shots (1-based): {shots}." if shots else
                        " All current stills.")
            if mode == "edit":
                extra = (
                    f" MODE={mode_label}.{path_txt} EDIT: load each flagged still from disk, "
                    "register it with Higgsfield MCP via local upload / media_import "
                    "(media_import_url cannot fetch localhost artifact URLs), confirm the image "
                    "model's start/reference media role with models_explore, then generate_image "
                    "with that media_id plus a preservation prompt: keep composition, character, "
                    "layout, and typography; apply only the feedback. Keep Element IDs. Same "
                    "aspect ratio. Replace only those files and their asset_manifest rows "
                    "(new credits / job_id). Leave other slides untouched. If the image model "
                    "rejects a source still, surface a blocker — do NOT silently switch to FRESH."
                )
            else:
                extra = (
                    f" MODE={mode_label}.{path_txt} FRESH: generate_image from text + "
                    "panda/customer Element IDs only. Do NOT pass the old PNG. Replace only "
                    "the flagged files and their asset_manifest rows. Leave other slides untouched."
                )
            extra += (
                " Rewrite the assets checkpoint with status='awaiting_human' AND top-level "
                "partial_progress={\"phase\":\"stills\"} (not nested under metadata) and STOP. "
                "Do NOT generate video."
            )
        return (
            f"Revise stage '{stage}' for project_id: {job_id} per this feedback: {note}.{shot_txt}"
            f"{extra} "
            "Rewrite that stage's checkpoint with status='awaiting_human' and STOP for approval."
        )


def get_runner(name: str) -> Runner:
    return {"mock": MockRunner, "claude": ClaudeCodeRunner}.get(name, MockRunner)()
