"""FFmpeg chains, as pure functions.

Extracted from the repo's existing craft code:
  - normalize + concat + timed overlay `enable='between(t,a,b)'`
        <- projects/panda-airport-arrival-ugc/artifacts/compose.py
  - xfade offset math for N clips
        <- tools/video/video_stitch.py::_chain_xfade
  - amix + AAC 192k
        <- tools/audio/audio_mixer.py
  - grade filter strings
        <- tools/enhancement/color_grade.py (copied into config.GRADE_PRESETS)
  - minterpolate -> 60fps
        was never committed to code; it lived in ad-hoc shell commands and is
        implemented fresh here (see EXTRACTION_NOTES.md).

Nothing here accepts a raw filter string or raw ffmpeg arg from a request.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from montage_svc.config import GRADE_PRESETS, ffmpeg_bin, ffprobe_bin

X264 = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "192k"]


class RenderError(RuntimeError):
    """An ffmpeg invocation failed. Message carries the tail of stderr."""


def _run(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-6:]
        raise RenderError(f"{what} failed: " + " | ".join(tail))


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RenderError(f"could not probe duration of {path.name}") from None


def has_audio(path: Path) -> bool:
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def probe_fps(path: Path) -> float:
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=avg_frame_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    raw = r.stdout.strip()
    try:
        num, _, den = raw.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_size(path: Path) -> tuple[int, int]:
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True,
    )
    try:
        w, h = (int(x) for x in r.stdout.strip().split("x")[:2])
        return w, h
    except ValueError:
        raise RenderError(f"could not probe frame size of {path.name}") from None


def _scale_vf(w: int, h: int, fps: int | None) -> str:
    # Cover-crop to the target frame, exactly as the UGC compose chain does.
    rate = f"fps={fps}," if fps else ""
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},{rate}format=yuv420p,setsar=1")


def normalize_scene(src: Path, kind: str, duration_s: float, w: int, h: int,
                    fps: int | None, out: Path) -> Path:
    """Bring one scene to exactly `duration_s` at w x h, silent.

    `fps=None` keeps the source frame rate — that's what a video scene wants,
    because forcing it to 60 here would duplicate frames and leave minterpolate
    nothing real to interpolate between. Images pass the target fps directly.

    Images are looped. Videos are trimmed to length, and short ones are padded
    by cloning the last frame (tpad) rather than being left to end early — a
    scene that under-runs its slot would desync every downstream xfade offset.
    """
    if kind == "image":
        cmd = [ffmpeg_bin(), "-y", "-loop", "1", "-t", f"{duration_s}", "-i", str(src),
               "-vf", _scale_vf(w, h, fps), "-an", *X264, "-crf", "18", str(out)]
    else:
        vf = (f"{_scale_vf(w, h, fps)},"
              f"tpad=stop_mode=clone:stop_duration={duration_s},"
              f"trim=duration={duration_s},setpts=PTS-STARTPTS")
        cmd = [ffmpeg_bin(), "-y", "-i", str(src), "-vf", vf, "-an",
               *X264, "-crf", "18", str(out)]
    _run(cmd, f"normalize {src.name}")
    return out


def interpolate(src: Path, fps: int, out: Path) -> Path:
    """Motion-interpolate to `fps`. The heaviest step in the pipeline by far."""
    _run([ffmpeg_bin(), "-y", "-i", str(src),
          "-vf", f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
          "-an", *X264, "-crf", "18", str(out)],
         f"minterpolate {src.name}")
    return out


def overlay_static(src: Path, png: Path, out: Path) -> Path:
    """Stamp one full-frame RGBA PNG over the whole clip."""
    _run([ffmpeg_bin(), "-y", "-i", str(src), "-i", str(png),
          "-filter_complex", "[0:v][1:v]overlay=0:0[v]", "-map", "[v]",
          "-an", *X264, "-crf", "18", str(out)],
         f"overlay {png.name}")
    return out


def overlay_timed(src: Path, layers: list[tuple[Path, float, float]], out: Path) -> Path:
    """Stamp N full-frame PNGs over time windows.

    This is the chain from the UGC compose.py, generalized: each layer becomes
    an `overlay=0:0:enable='between(t,t0,t1)'` link in a chained filtergraph.
    """
    if not layers:
        raise RenderError("overlay_timed called with no layers")
    inputs: list[str] = ["-i", str(src)]
    for png, _, _ in layers:
        inputs += ["-i", str(png)]

    links = []
    cur = "[0:v]"
    for idx, (_png, t0, t1) in enumerate(layers, start=1):
        nxt = f"[v{idx}]"
        links.append(f"{cur}[{idx}:v]overlay=0:0:enable='between(t,{t0},{t1})'{nxt}")
        cur = nxt

    has_a = has_audio(src)
    cmd = [ffmpeg_bin(), "-y", *inputs,
           "-filter_complex", ";".join(links), "-map", cur]
    if has_a:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    else:
        cmd += ["-an"]
    cmd += [*X264, "-crf", "19", "-movflags", "+faststart", str(out)]
    _run(cmd, "timed overlay")
    return out


def join(clips: list[Path], out: Path, transition: str, duration_s: float,
         fps: int) -> Path:
    """Join clips with cross-dissolves (or hard cuts when duration_s == 0).

    xfade offsets are cumulative and shrink by the transition length on every
    join — the same math as video_stitch._chain_xfade. Getting this wrong is
    the classic way a montage drifts out of sync with its captions.
    """
    if len(clips) == 1:
        _run([ffmpeg_bin(), "-y", "-i", str(clips[0]), "-c", "copy", str(out)], "passthrough")
        return out

    if transition == "cut" or duration_s <= 0:
        listfile = out.parent / f"{out.stem}_concat.txt"
        listfile.write_text(
            "".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8"
        )
        _run([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
              *X264, "-crf", "18", str(out)], "concat")
        return out

    durations = [probe_duration(c) for c in clips]
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]

    links = []
    cur = "[0:v]"
    offset = 0.0
    for i in range(1, len(clips)):
        offset += durations[i - 1] - duration_s
        nxt = "[vout]" if i == len(clips) - 1 else f"[x{i}]"
        links.append(
            f"{cur}[{i}:v]xfade=transition=fade:duration={duration_s}:"
            f"offset={offset:.3f}{nxt}"
        )
        cur = nxt

    _run([ffmpeg_bin(), "-y", *inputs, "-filter_complex", ";".join(links),
          "-map", "[vout]", "-r", str(fps), "-an", *X264, "-crf", "18", str(out)],
         "xfade join")
    return out


def grade(src: Path, preset: str, out: Path) -> Path:
    """Apply a named grade preset. Unknown names are rejected upstream by the
    schema; `none` is a copy."""
    vf = GRADE_PRESETS.get(preset, "")
    if not vf:
        _run([ffmpeg_bin(), "-y", "-i", str(src), "-c", "copy", str(out)], "grade passthrough")
        return out
    _run([ffmpeg_bin(), "-y", "-i", str(src), "-vf", vf, "-an",
          *X264, "-crf", "18", str(out)], f"grade {preset}")
    return out


def mix_audio(video: Path, out: Path, music: Path | None = None,
              voice: Path | None = None,
              sfx: list[tuple[Path, float, float]] | None = None,
              music_db: float = -18.0, voice_db: float = -6.0) -> Path:
    """Lay a music bed / VO / SFX under an existing video track.

    Video is stream-copied — this never re-encodes picture, which is what makes
    /mix-audio a cheap revision rung. amix uses normalize=0 so the declared dB
    levels survive: with the default normalize=1, ffmpeg divides every input by
    the input count and a carefully set -6 dB VO silently drops further.
    """
    sfx = sfx or []
    if music is None and voice is None and not sfx:
        _run([ffmpeg_bin(), "-y", "-i", str(video), "-c", "copy",
              "-movflags", "+faststart", str(out)], "mix passthrough")
        return out

    total = probe_duration(video)
    inputs: list[str] = ["-i", str(video)]
    parts: list[str] = []
    labels: list[str] = []
    idx = 1

    if music is not None:
        # -stream_loop -1 so a short bed tiles under a longer cut instead of
        # dropping to silence partway through.
        inputs = ["-i", str(video), "-stream_loop", "-1", "-i", str(music)]
        parts.append(
            f"[{idx}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
            f"volume={music_db}dB,afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5[amus]"
        )
        labels.append("[amus]")
        idx += 1

    if voice is not None:
        inputs += ["-i", str(voice)]
        parts.append(f"[{idx}:a]volume={voice_db}dB,apad[avoc]")
        labels.append("[avoc]")
        idx += 1

    for n, (path, at_s, db) in enumerate(sfx):
        inputs += ["-i", str(path)]
        delay_ms = int(at_s * 1000)
        parts.append(
            f"[{idx}:a]adelay={delay_ms}|{delay_ms},volume={db}dB[asfx{n}]"
        )
        labels.append(f"[asfx{n}]")
        idx += 1

    if len(labels) == 1:
        parts.append(f"{labels[0]}apad,atrim=0:{total:.3f}[aout]")
    else:
        parts.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
            f"normalize=0,apad,atrim=0:{total:.3f}[aout]"
        )

    _run([ffmpeg_bin(), "-y", *inputs, "-filter_complex", ";".join(parts),
          "-map", "0:v", "-map", "[aout]", "-c:v", "copy", *AAC,
          "-movflags", "+faststart", "-t", f"{total:.3f}", str(out)],
         "audio mix")
    return out


def finalize(src: Path, out: Path) -> Path:
    """Faststart remux for web playback."""
    _run([ffmpeg_bin(), "-y", "-i", str(src), "-c", "copy",
          "-movflags", "+faststart", str(out)], "finalize")
    return out
