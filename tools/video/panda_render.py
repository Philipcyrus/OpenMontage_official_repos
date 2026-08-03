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
            "resolution": {"type": "string", "default": "1080x1920"},
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
                    "voice_path": {"type": "string"},
                    "sfx": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "at_s": {"type": "number"}, "db": {"type": "number"}},
                    }},
                    "music_db": {"type": "number", "default": -18.0},
                    "voice_db": {"type": "number", "default": -6.0},
                },
            },
            "run_id": {"type": "string", "description": "Optional; derived from output name if omitted."},
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

        try:
            # --- 1) stage every media file into the run's media/ dir ----------
            st.ensure_run(run_id)

            scenes_in = inputs["scenes"]
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

            audio_in = inputs.get("audio") or {}
            def _stage(path: str | None, label: str) -> str | None:
                if not path:
                    return None
                p = Path(path)
                if not p.is_file():
                    raise FileNotFoundError(f"audio not found: {path}")
                st.save_media(run_id, label, p.suffix, p.read_bytes())
                return label

            music_id = _stage(audio_in.get("music_path"), "music")
            voice_id = _stage(audio_in.get("voice_path"), "voice")
            sfx_models: list[Sfx] = []
            for j, s in enumerate(audio_in.get("sfx", [])):
                sid = _stage(s["path"], f"sfx{j:02d}")
                sfx_models.append(Sfx(media_id=sid, at_s=float(s.get("at_s", 0)), db=float(s.get("db", 0))))

            # --- 2) build the validated request ------------------------------
            tr = inputs.get("transition") or {}
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
        return ToolResult(
            success=True,
            data={
                "provider": "montage_svc_folded",
                "profile": inputs.get("profile", "ugc"),
                "branded": False,
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                **probed,
            },
            artifacts=[str(output_path)],
            duration_seconds=round(time.time() - start, 2),
        )
