"""Clean branded-render compose tool — montage-svc's render craft, folded in.

This replaces the old HTTP hinge (`montage_svc_compose.py`). Instead of POSTing to a
separate montage-svc service, we call montage-svc's PROVEN render pipeline in-process
(vendored at `vendor/montage_svc/`). The ffmpeg chains are reused verbatim — nothing
re-implemented, nothing re-tested.

CLEAN by default: uses the `ugc` profile (logo.enabled=false, cards.enabled=false), so the
output carries NO Panda branding. Branding is a SEPARATE, on-demand step applied to the
approved master by `panda_brand` — never baked in here. See memory
openmontage-render-fold-and-branding.

The tool takes plain local file paths for scenes + audio, stages them into a montage run
(runs/{run_id}/media/{media_id}.ext), builds a validated ComposeRequest, renders, copies the
result to output_path, and cleans up the run scratch.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# --- make the vendored montage_svc importable + point it at brand/data ------
_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ENGINE_ROOT / "vendor"
os.environ.setdefault("MONTAGE_BRAND_DIR", str(_VENDOR / "brand"))
os.environ.setdefault("MONTAGE_DATA_DIR", str(_VENDOR / "data"))  # gitignored scratch
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

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


def _safe_id(s: str) -> str:
    """Coerce an arbitrary string into montage's ^[A-Za-z0-9_-]{1,64}$ id space."""
    import re

    out = re.sub(r"[^A-Za-z0-9_-]", "-", s)[:64].strip("-")
    return out or "run"


def _duration_bounds(target_s: float, tolerance_fraction: float) -> tuple[float, float]:
    target = float(target_s)
    tolerance = float(tolerance_fraction)
    if target <= 0:
        raise ValueError("target_duration_s must be positive")
    if not 0 <= tolerance < 1:
        raise ValueError("duration_tolerance_fraction must be in [0, 1)")
    return target * (1.0 - tolerance), target * (1.0 + tolerance)


def expected_timeline_duration(
    scenes: list[dict[str, Any]], transition: dict[str, Any] | None = None
) -> float:
    """Return assembled visual duration after transition overlap."""
    if not scenes:
        raise ValueError("scenes cannot be empty")
    durations = [float(scene["duration_s"]) for scene in scenes]
    if any(duration <= 0 for duration in durations):
        raise ValueError("every scene duration_s must be positive")
    for scene, duration in zip(scenes, durations):
        audio_end = float(scene.get("audio_end_s") or 0.0)
        if audio_end > duration + 1e-6:
            raise ValueError(
                f"scene audio_end_s {audio_end:.3f}s exceeds effective duration "
                f"{duration:.3f}s"
            )
        source_duration = scene.get("source_duration_s")
        if (
            scene.get("audio_lipsync")
            and source_duration is not None
            and audio_end > float(source_duration) + 1e-6
        ):
            raise ValueError(
                f"lip-synced scene audio ends at {audio_end:.3f}s after source motion "
                f"{float(source_duration):.3f}s; a frozen hold would cover active speech"
            )
    tr = transition or {}
    overlap = (
        max(0.0, float(tr.get("duration_s", 0.5)))
        if tr.get("type", "xfade") == "xfade"
        else 0.0
    )
    if overlap and any(duration <= overlap for duration in durations):
        raise ValueError("transition overlap must be shorter than every scene")
    return sum(durations) - overlap * max(0, len(durations) - 1)


def _target_duration_error(
    actual_s: float, target_s: float | None, tolerance_fraction: float
) -> str | None:
    if target_s is None:
        return None
    lower, upper = _duration_bounds(target_s, tolerance_fraction)
    if lower - 1e-6 <= actual_s <= upper + 1e-6:
        return None
    return (
        f"timeline duration {actual_s:.3f}s is outside requested "
        f"{float(target_s):.3f}s ±{float(tolerance_fraction) * 100:.1f}% "
        f"({lower:.3f}-{upper:.3f}s)"
    )


def _premix_voice_tracks(
    tracks: list[dict[str, Any]],
    out_path: Path,
    *,
    voice_db: float = 0.0,
) -> Path:
    """Mix N VO clips (path + at_s) into one stereo bed via adelay+amix.

    Used so montage_svc's single voice_media_id can carry multi-speaker casting without
    rewriting the vendored AudioSpec. voice_db here is relative within the premix (usually 0);
    panda_render still applies audio.voice_db on the final mix.
    """
    import subprocess

    if not tracks:
        raise ValueError("voice_tracks is empty")

    from montage_svc.render.ffmpeg_ops import probe_duration

    prepared: list[tuple[Path, float]] = []
    for i, tr in enumerate(tracks):
        p = Path(tr["path"])
        if not p.is_file():
            raise FileNotFoundError(f"voice_tracks[{i}] not found: {p}")
        prepared.append((p, float(tr.get("at_s", 0) or 0)))

    if len(prepared) == 1 and prepared[0][1] <= 0:
        # Single track at t=0 — no delay/mix needed, but still transcode into
        # out_path's own container (a raw byte copy would mislabel e.g. mp3
        # source bytes under a .wav name).
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-i", str(prepared[0][0]), "-c:a", "pcm_s16le", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out_path.is_file():
            raise RuntimeError(
                f"voice_tracks premix failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '')[-800:]}"
            )
        return out_path

    # Bound the mix explicitly: a bare trailing `apad` has no stop condition,
    # so ffmpeg never signals EOF on [aout] and the process hangs forever.
    total = max(at_s + probe_duration(path) for path, at_s in prepared)

    inputs: list[str] = []
    parts: list[str] = []
    labels: list[str] = []
    for i, (path, at_s) in enumerate(prepared):
        inputs += ["-i", str(path)]
        delay_ms = max(0, int(round(at_s * 1000)))
        parts.append(
            f"[{i}:a]adelay={delay_ms}|{delay_ms},volume={voice_db}dB,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[av{i}]"
        )
        labels.append(f"[av{i}]")

    if len(labels) == 1:
        mix = f"{labels[0]}apad,atrim=0:{total:.3f}[aout]"
    else:
        mix = (
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
            f"normalize=0,apad,atrim=0:{total:.3f}[aout]"
        )
    filter_complex = ";".join(parts + [mix])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]", "-c:a", "pcm_s16le",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "voice_tracks premix timed out after 120s"
        ) from exc
    if proc.returncode != 0 or not out_path.is_file():
        raise RuntimeError(
            f"voice_tracks premix failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '')[-800:]}"
        )
    return out_path


class PandaRender(BaseTool):
    name = "panda_render"
    version = "0.1.0"
    tier = ToolTier.COMPOSE if hasattr(ToolTier, "COMPOSE") else ToolTier.GENERATE  # TODO confirm tier
    capability = "video_compose"
    provider = "montage_svc_folded"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL if hasattr(ToolRuntime, "LOCAL") else ToolRuntime.API

    dependencies = ["bin:ffmpeg", "bin:ffprobe"]
    install_instructions = (
        "Uses the vendored montage-svc render pipeline (vendor/montage_svc). "
        "Requires ffmpeg + ffprobe on PATH, and a CJK font (msyhbd.ttc) resolvable for "
        "Chinese captions (brand/fonts/ or a system font dir)."
    )

    capabilities = ["clean_compose"]
    best_for = [
        "assembling approved clips + audio + captions into the finished (UNBRANDED) master",
    ]
    not_good_for = ["adding branding — that is panda_brand, applied after approval"]
    fallback_tools = ["video_compose"]
    quality_score = 0.95

    input_schema = {
        "type": "object",
        "required": ["scenes", "output_path"],
        "properties": {
            "profile": {"type": "string", "default": "ugc",
                        "description": "Render profile. 'ugc' = clean/no-brand (default). 'bgc' would brand at compose time — avoid; branding is a separate step."},
            "resolution": {
                "type": "string",
                "default": "1080x1920",
                "description": (
                    "Master canvas WxH. Compose must pass the job's options.aspect_ratio "
                    "canvas (e.g. 9:16→1080x1920, 16:9→1920x1080). Default 1080x1920 is only "
                    "a last-resort fallback when omitted."
                ),
            },
            "fps": {"type": "integer", "default": 30,
                    "description": "Target fps. 60 triggers minterpolate (slow); use 30 for straight assembly."},
            "grade": {"type": "string", "default": "none"},
            "transition": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["xfade", "cut"], "default": "xfade"},
                    "duration_s": {"type": "number", "default": 0.5},
                },
            },
            "scenes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["media_path", "duration_s"],
                    "properties": {
                        "media_path": {"type": "string"},
                        "duration_s": {"type": "number"},
                        "source_duration_s": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Measured source clip duration before conformance.",
                        },
                        "audio_end_s": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Scene-local end of narration; must fit effective duration.",
                        },
                        "audio_lipsync": {
                            "type": "boolean",
                            "default": False,
                            "description": "When true, active audio must also fit source motion before any tail hold.",
                        },
                        "captions": {
                            "type": "object",
                            "properties": {"zh": {"type": "string"}, "en": {"type": "string"}},
                        },
                        "overlays": {"type": "array"},
                    },
                },
            },
            "audio": {
                "type": "object",
                "properties": {
                    "music_path": {"type": "string"},
                    "voice_path": {
                        "type": "string",
                        "description": "Single VO bed (legacy / single-speaker). Ignored when voice_tracks is set.",
                    },
                    "voice_tracks": {
                        "type": "array",
                        "description": (
                            "Multi-speaker VO: each entry is placed at at_s (seconds) and premixed "
                            "into one voice bed before montage mix. Prefer this when the script "
                            "has multiple section.speaker lines."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["path"],
                            "properties": {
                                "path": {"type": "string"},
                                "at_s": {"type": "number", "default": 0},
                                "speaker": {
                                    "type": "string",
                                    "enum": ["customer", "panda", "narrator"],
                                },
                            },
                        },
                    },
                    "sfx": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "at_s": {"type": "number"}, "db": {"type": "number"}},
                    }},
                    "music_db": {"type": "number", "default": -18.0},
                    "voice_db": {"type": "number", "default": -6.0},
                },
            },
            "run_id": {"type": "string", "description": "Optional; derived from output name if omitted."},
            "target_duration_s": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Requested final duration; when set, preflight and ffprobe must remain within tolerance.",
            },
            "duration_tolerance_fraction": {
                "type": "number",
                "minimum": 0,
                "exclusiveMaximum": 1,
                "default": 0.05,
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=2000, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    side_effects = ["renders video via ffmpeg", "writes final mp4 to output_path"]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        scenes_in: list[dict[str, Any]] = list(inputs["scenes"])
        transition_in: dict[str, Any] = inputs.get("transition") or {}
        target_duration = inputs.get("target_duration_s")
        duration_tolerance = float(inputs.get("duration_tolerance_fraction", 0.05))
        try:
            expected_duration = expected_timeline_duration(scenes_in, transition_in)
            preflight_error = _target_duration_error(
                expected_duration, target_duration, duration_tolerance
            )
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(success=False, error=f"panda_render duration preflight failed: {exc}")
        if preflight_error:
            return ToolResult(success=False, error=f"panda_render duration preflight failed: {preflight_error}")

        from montage_svc import storage as st
        from montage_svc.render.pipelines import run_compose
        from montage_svc.schemas import (
            AudioSpec,
            ComposeRequest,
            Scene,
            Sfx,
            Transition,
        )

        output_path = Path(inputs["output_path"])
        run_id = _safe_id(inputs.get("run_id") or output_path.stem or "panda-render")
        audio_in: dict[str, Any] = inputs.get("audio") or {}
        voice_tracks: list = list(audio_in.get("voice_tracks") or [])

        try:
            # --- 1) stage every media file into the run's media/ dir ----------
            st.ensure_run(run_id)

            scene_models: list[Scene] = []
            for i, sc in enumerate(scenes_in):
                src = Path(sc["media_path"])
                if not src.is_file():
                    return ToolResult(success=False, error=f"scene {i} media not found: {src}")
                mid = f"s{i:03d}"
                st.save_media(run_id, mid, src.suffix, src.read_bytes())
                scene_models.append(Scene(
                    media_id=mid,
                    duration_s=float(sc["duration_s"]),
                    captions=sc.get("captions"),
                    overlays=sc.get("overlays", []),
                ))

            def _stage(path: str | None, label: str) -> str | None:
                if not path:
                    return None
                p = Path(path)
                if not p.is_file():
                    raise FileNotFoundError(f"audio not found: {path}")
                st.save_media(run_id, label, p.suffix, p.read_bytes())
                return label

            music_id = _stage(audio_in.get("music_path"), "music")

            if voice_tracks:
                premix_path = st.run_dir(run_id) / "voice_premix.wav"
                _premix_voice_tracks(voice_tracks, premix_path)
                voice_id = _stage(str(premix_path), "voice")
            else:
                voice_id = _stage(audio_in.get("voice_path"), "voice")

            sfx_models: list[Sfx] = []
            for j, s in enumerate(audio_in.get("sfx", [])):
                sid = _stage(s["path"], f"sfx{j:02d}")
                sfx_models.append(Sfx(media_id=sid, at_s=float(s.get("at_s", 0)), db=float(s.get("db", 0))))

            # --- 2) build the validated request ------------------------------
            tr = transition_in
            req = ComposeRequest(
                run_id=run_id,
                version=1,
                profile=inputs.get("profile", "ugc"),        # CLEAN by default
                fps=int(inputs.get("fps", 30)),
                resolution=inputs.get("resolution", "1080x1920"),
                scenes=scene_models,
                transition=Transition(
                    type=tr.get("type", "xfade"),
                    duration_s=float(tr.get("duration_s", 0.5)),
                ),
                cards=None,                                   # NO cards here — branding is separate
                audio=AudioSpec(
                    music_media_id=music_id,
                    voice_media_id=voice_id,
                    sfx=sfx_models,
                    music_db=float(audio_in.get("music_db", -18.0)),
                    voice_db=float(audio_in.get("voice_db", -6.0)),
                ),
                grade=inputs.get("grade", "none"),
                output_label="final",
            )

            # --- 3) render (proven montage-svc pipeline, verbatim) -----------
            rendered = run_compose(req, job_id=f"{run_id}-job", progress=lambda _f: None)

            # --- 4) copy result out + clean the run scratch ------------------
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(rendered, output_path)
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"panda_render failed: {e}")
        finally:
            try:
                shutil.rmtree(st.run_dir(run_id), ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

        if not output_path.is_file() or output_path.stat().st_size == 0:
            return ToolResult(success=False, error="render produced no output")

        from tools.video._shared import probe_output

        probed = probe_output(output_path)
        if "duration_seconds" not in probed:
            if target_duration is not None:
                return ToolResult(
                    success=False,
                    error=(
                        "panda_render duration probe unavailable: ffprobe did not "
                        "return duration_seconds for the rendered output"
                    ),
                    artifacts=[str(output_path)],
                    duration_seconds=round(time.time() - start, 2),
                )
            actual_duration = None
        else:
            actual_duration = float(probed["duration_seconds"])
            postflight_error = _target_duration_error(
                actual_duration, target_duration, duration_tolerance
            )
            if postflight_error:
                return ToolResult(
                    success=False,
                    error=f"panda_render duration postflight failed: {postflight_error}",
                    artifacts=[str(output_path)],
                    duration_seconds=round(time.time() - start, 2),
                )
        return ToolResult(
            success=True,
            data={
                "provider": "montage_svc_folded",
                "profile": inputs.get("profile", "ugc"),
                "branded": False,
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                "voice_track_count": len(voice_tracks) if voice_tracks else (1 if audio_in.get("voice_path") else 0),
                "expected_timeline_duration_seconds": round(expected_duration, 3),
                "target_duration_seconds": (
                    round(float(target_duration), 3)
                    if target_duration is not None
                    else None
                ),
                "duration_tolerance_fraction": duration_tolerance,
                **probed,
            },
            artifacts=[str(output_path)],
            duration_seconds=round(time.time() - start, 2),
        )
