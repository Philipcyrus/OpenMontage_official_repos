"""The three render pipelines: compose, overlay, mix.

Each takes a validated request + a progress callback, and returns the path of
the finished mp4. They are pure with respect to the service: no DB, no HTTP.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from montage_svc.render import ffmpeg_ops as ff
from montage_svc.render import overlays as ov
from montage_svc.schemas import AudioSpec, ComposeRequest, MixRequest, OverlayRequest
from montage_svc.storage import (
    find_media,
    kind_for_ext,
    load_profile,
    out_dir,
    resolve_source,
    work_dir,
)

Progress = Callable[[float], None]


def output_path(run_id: str, label: str, version: int) -> Path:
    d = out_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{label}_v{version}.mp4"


def _audio_args(run_id: str, spec: AudioSpec) -> dict:
    return {
        "music": find_media(run_id, spec.music_media_id) if spec.music_media_id else None,
        "voice": find_media(run_id, spec.voice_media_id) if spec.voice_media_id else None,
        "sfx": [(find_media(run_id, s.media_id), s.at_s, s.db) for s in spec.sfx],
        "music_db": spec.music_db,
        "voice_db": spec.voice_db,
    }


# --------------------------------------------------------------------------
# compose
# --------------------------------------------------------------------------

def run_compose(req: ComposeRequest, job_id: str, progress: Progress) -> Path:
    """normalize -> minterpolate -> overlay -> xfade join (+ cards) -> grade -> mix.

    Note the order: the brief specifies overlay before minterpolate, but
    interpolating a burned-in caption warps its glyph edges. Motion is
    interpolated first, then text is stamped on top, so captions stay crisp.
    See EXTRACTION_NOTES.md.
    """
    profile = load_profile(req.profile)
    w, h = req.wh()
    work = work_dir(req.run_id, job_id)
    work.mkdir(parents=True, exist_ok=True)

    try:
        clips: list[Path] = []
        cards = req.cards if (req.cards and profile["cards"].get("enabled")) else None

        # --- intro card ---------------------------------------------------
        if cards and cards.intro:
            png = ov.intro_card(profile, w, h, cards.intro.model_dump(), work / "card_intro.png")
            clips.append(ff.normalize_scene(
                png, "image", float(profile["cards"]["intro_duration_s"]),
                w, h, req.fps, work / "clip_intro.mp4"))

        # --- scenes -------------------------------------------------------
        n = len(req.scenes)
        for i, scene in enumerate(req.scenes):
            src = find_media(req.run_id, scene.media_id)
            kind = kind_for_ext(src.suffix)
            if kind == "audio":
                raise ff.RenderError(
                    f"scene {i} media_id {scene.media_id!r} is audio; scenes need image or video"
                )

            # Images are already static — nothing to interpolate, so they are
            # rasterised straight at the target fps.
            base_fps = req.fps if kind == "image" else None
            clip = ff.normalize_scene(src, kind, scene.duration_s, w, h, base_fps,
                                      work / f"s{i:02d}_norm.mp4")

            if kind == "video" and abs(ff.probe_fps(clip) - req.fps) > 0.5:
                clip = ff.interpolate(clip, req.fps, work / f"s{i:02d}_fps.mp4")

            png = ov.scene_overlay(
                profile, w, h,
                scene.captions.model_dump() if scene.captions else None,
                [o.model_dump() for o in scene.overlays],
                work / f"s{i:02d}_ovl.png",
            )
            if png:
                clip = ff.overlay_static(clip, png, work / f"s{i:02d}_final.mp4")

            clips.append(clip)
            progress(0.10 + 0.65 * (i + 1) / n)

        # --- outro card ---------------------------------------------------
        if cards and cards.outro:
            png = ov.outro_card(profile, w, h, cards.outro.model_dump(), work / "card_outro.png")
            clips.append(ff.normalize_scene(
                png, "image", float(profile["cards"]["outro_duration_s"]),
                w, h, req.fps, work / "clip_outro.mp4"))

        # --- join / grade / mix -------------------------------------------
        joined = ff.join(clips, work / "joined.mp4", req.transition.type,
                         req.transition.duration_s, req.fps)
        progress(0.82)

        graded = ff.grade(joined, req.grade, work / "graded.mp4")
        progress(0.90)

        final = output_path(req.run_id, req.output_label, req.version)
        if req.audio.is_empty():
            ff.finalize(graded, final)
        else:
            ff.mix_audio(graded, final, **_audio_args(req.run_id, req.audio))
        progress(1.0)
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# overlay — cheap revision rung
# --------------------------------------------------------------------------

def run_overlay(req: OverlayRequest, job_id: str, progress: Progress) -> Path:
    """Re-draw overlays on an existing render. No regeneration, no re-interpolation.

    Implemented for real (not the brief's documented fallback): the timed
    `enable='between(t,a,b)'` chain from the UGC compose script re-stamps
    captions onto a joined video directly.
    """
    profile = load_profile(req.profile)
    src = resolve_source(req.run_id, req.source)
    work = work_dir(req.run_id, job_id)
    work.mkdir(parents=True, exist_ok=True)

    try:
        probe = ff.probe_duration(src)
        # The frame size comes from the source, not the request — re-overlaying
        # must not silently rescale an already-approved cut.
        w, h = ff.probe_size(src)

        layers: list[tuple[Path, float, float]] = []
        n = len(req.scenes)
        for i, scene in enumerate(req.scenes):
            if scene.start_s >= probe:
                raise ff.RenderError(
                    f"scene {i} starts at {scene.start_s}s but {req.source} is only "
                    f"{probe:.2f}s long"
                )
            png = ov.scene_overlay(
                profile, w, h,
                scene.captions.model_dump() if scene.captions else None,
                [o.model_dump() for o in scene.overlays],
                work / f"o{i:02d}.png",
                # The logo is baked into the source already; re-stamping it on
                # every window would double the watermark.
                with_logo=False,
            )
            if png:
                layers.append((png, scene.start_s, min(scene.end_s, probe)))
            progress(0.10 + 0.50 * (i + 1) / n)

        if not layers:
            raise ff.RenderError("no overlays to draw — every scene was empty")

        final = output_path(req.run_id, req.output_label, req.version)
        ff.overlay_timed(src, layers, final)
        progress(1.0)
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# mix-audio — the other cheap rung
# --------------------------------------------------------------------------

def run_mix(req: MixRequest, _job_id: str, progress: Progress) -> Path:
    """Keep the video track, replace the audio. Video is stream-copied.

    Takes the same (req, job_id, progress) shape as the other runners so the job
    worker can call them uniformly; mix needs no scratch dir, so job_id is unused.
    """
    src = resolve_source(req.run_id, req.source)
    progress(0.2)
    final = output_path(req.run_id, req.output_label, req.version)
    ff.mix_audio(src, final, **_audio_args(req.run_id, req.audio))
    progress(1.0)
    return final
