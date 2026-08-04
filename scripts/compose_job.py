#!/usr/bin/env python
"""Deterministic compose: assemble a job's already-generated clips (+ VO) into the final
master via panda_render — NO agent. This is the reliable, fast, visible replacement for an
agent-driven compose (which stalls: compose is pure assembly, not a task for the LLM). It
mirrors how the Mochi Dify workflow delegates rendering to code rather than the model.

Uses the engine's canonical project layout:
  projects/<job>/assets/video/*.mp4   -> scenes (in filename order)
  projects/<job>/assets/audio/*.wav   -> concatenated into one VO track (optional)
  -> writes projects/<job>/renders/final.mp4

Usage: python scripts/compose_job.py <job_id> [--transition xfade|cut]
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MONTAGE_BRAND_DIR", str(ROOT / "vendor" / "brand"))
os.environ.setdefault("MONTAGE_DATA_DIR", str(ROOT / "vendor" / "data"))

FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")


def _dur(p: Path) -> float:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/compose_job.py <job_id> [--transition xfade|cut]")
        sys.exit(1)
    job = sys.argv[1]
    transition = "cut"
    if "--transition" in sys.argv:
        transition = sys.argv[sys.argv.index("--transition") + 1]

    proj = ROOT / "projects" / job
    vdir, adir = proj / "assets" / "video", proj / "assets" / "audio"
    clips = sorted(p for p in vdir.glob("*.mp4")) if vdir.is_dir() else []
    if not clips:
        print(f"no clips found in {vdir}")
        sys.exit(1)
    print("clips:", [c.name for c in clips])

    # Concatenate per-section VO wavs into a single voice track (re-encode for safety).
    voice_path = None
    vos = sorted(adir.glob("*.wav")) if adir.is_dir() else []
    if vos:
        print("vo:", [v.name for v in vos])
        listf = proj / "_vo_concat.txt"
        listf.write_text("".join(f"file '{v.resolve()}'\n" for v in vos), encoding="utf-8")
        voice_path = str(proj / "_vo.wav")
        r = subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                            "-ar", "48000", "-ac", "2", voice_path], capture_output=True, text=True)
        if not Path(voice_path).is_file():
            print("VO concat failed:", (r.stderr or "")[-400:])
            voice_path = None

    scenes = [{"media_path": str(c), "duration_s": round(_dur(c), 2) or 5.0} for c in clips]
    total = sum(s["duration_s"] for s in scenes)
    print(f"scene durations: {[s['duration_s'] for s in scenes]}  (total ~{total:.1f}s)")

    out = proj / "renders" / "final.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    inputs = {"scenes": scenes, "fps": 30, "grade": "none", "profile": "ugc",
              "output_path": str(out), "transition": {"type": transition, "duration_s": 0.4}}
    if voice_path:
        inputs["audio"] = {"voice_path": voice_path, "voice_db": -2.0}

    from tools.video.panda_render import PandaRender
    print(f">>> rendering deterministically via panda_render (transition={transition})...")
    res = PandaRender().execute(inputs)
    if not res.success:
        print("FAILED:", res.error)
        sys.exit(1)
    print("OK ->", out)
    print("data:", res.data)


if __name__ == "__main__":
    main()
