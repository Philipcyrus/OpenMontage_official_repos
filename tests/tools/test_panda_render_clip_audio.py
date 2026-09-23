"""A clip's own audio never reaches the final mix; the ElevenLabs voice is laid exactly once.

Kling's lip-synced clips come back with the voice baked in. Compose must drop every clip's audio
and lay the original voice file once, at its script time — otherwise the line would play twice
or drift from the mouth.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video.panda_render import PandaRender

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True)


def _clip(path: Path, with_audio: bool) -> Path:
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=12", "-t", "3"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-af", "volume=6dB", "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-an"]
    _run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    return path


def _render(clip: Path, voice: Path, out: Path, copies: int = 1) -> Path:
    result = PandaRender().execute({
        "profile": "ugc", "resolution": "64x64", "fps": 12,
        "transition": {"type": "cut", "duration_s": 0},
        "scenes": [{"media_path": str(clip), "duration_s": 3}],
        "audio": {"voice_tracks": [{"path": str(voice), "at_s": 1.0}] * copies},
        "output_path": str(out), "run_id": out.stem,
    })
    assert result.success, result.error
    return out


def _pcm(path: Path) -> bytes:
    return _run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-f", "s16le", "-ac", "1",
                 "-ar", "16000", "-"]).stdout


def _max_db(path: Path, start: float, dur: float) -> float:
    err = subprocess.run(["ffmpeg", "-ss", str(start), "-t", str(dur), "-i", str(path), "-vn",
                          "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.search(r"max_volume: (-?[\d.]+|-inf) dB", err).group(1).replace("-inf", "-200"))


def test_clip_audio_is_dropped_and_the_voice_is_mixed_once(tmp_path: Path) -> None:
    voice = tmp_path / "vo.wav"
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
          "-t", "1", str(voice)])
    loud = _render(_clip(tmp_path / "kling_like.mp4", True), voice, tmp_path / "final_a.mp4")
    silent = _render(_clip(tmp_path / "silent.mp4", False), voice, tmp_path / "final_b.mp4")

    twice = _render(_clip(tmp_path / "silent2.mp4", False), voice, tmp_path / "final_c.mp4",
                    copies=2)

    assert _pcm(loud) == _pcm(silent)                # the clip's own audio changed nothing
    assert _max_db(loud, 0.0, 0.9) < -60             # nothing before the line starts
    once = _max_db(loud, 1.0, 1.0)
    assert once > -40                                # the voice is there, at its script time
    assert _max_db(twice, 1.0, 1.0) - once > 4.5     # laid once: a second copy adds ~6 dB
