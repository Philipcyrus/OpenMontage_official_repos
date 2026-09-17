"""Real Remotion render of a screenshot scene (skipped where Node 22 + remotion-composer are missing).

Proves the screenshot lands exactly where lib/screen_layout.py's geometry says, over the clip,
for the clip's full length, and that the board renders.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import screen_layout as sl  # noqa: E402
from tools.video.screen_overlay import ScreenOverlay, remotion_ready  # noqa: E402

pytestmark = pytest.mark.skipif(
    not remotion_ready()[0] or not shutil.which("ffmpeg"),
    reason=f"Remotion not ready here: {remotion_ready()[1]}",
)


def _frame(video: Path, t: float, out: Path):
    from PIL import Image

    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(video),
                    "-frames:v", "1", str(out)], check=True)
    return Image.open(out).convert("RGB")


def test_compose_places_screenshot_where_geometry_says(tmp_path):
    from PIL import Image, ImageDraw

    proj = tmp_path / "job_render"
    for sub in ("inputs", "assets/video", "artifacts"):
        (proj / sub).mkdir(parents=True)
    shot = Image.new("RGB", (400, 800), "#ffffff")
    ImageDraw.Draw(shot).rectangle([100, 300, 300, 500], fill="#1a73e8")   # blue block in the middle
    shot.save(proj / "inputs" / "in_01.png")
    (proj / "inputs" / "inputs.json").write_text(json.dumps([
        {"n": 1, "input_id": "in_01", "name": "s.png", "file": "in_01.png", "width": 400, "height": 800}]),
        encoding="utf-8")
    layout = {"zone": {"x": 0.5, "y": 0.1, "w": 0.45, "h": 0.6}, "frame": "none",
              "enter": {"type": "none"}}
    plan = {"version": "1.0", "metadata": {"aspect_ratio": "9:16"}, "scenes": [
        {"id": "s01", "type": "character_scene", "description": "x", "start_seconds": 0, "end_seconds": 1.5,
         "required_assets": [{"type": "image", "source": "provided", "input_id": "in_01",
                              "description": "d", "layout": layout}]}]}
    (proj / "checkpoint_scene_plan.json").write_text(json.dumps({"artifacts": {"scene_plan": plan}}),
                                                     encoding="utf-8")
    clip = proj / "assets" / "video" / "s01.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=white:s=540x960:r=24:d=1.0", "-pix_fmt", "yuv420p", str(clip)], check=True)
    (proj / "artifacts" / "asset_manifest.json").write_text(json.dumps({"version": "1.0", "assets": [
        {"id": "c", "type": "video", "path": "assets/video/s01.mp4", "source_tool": "t", "scene_id": "s01"}]}),
        encoding="utf-8")

    tool = ScreenOverlay()
    res = tool.execute({"mode": "compose", "project_dir": str(proj), "resolution": "540x960",
                        "scenes": [{"scene_id": "s01", "media_path": str(clip), "duration_s": 1.5}]})
    assert res.success, res.error
    out = Path(res.data["scenes"][0]["media_path"])
    assert out.is_file() and out.parent.name == "overlay"
    meta = json.loads((proj / "overlay" / "s01.json").read_text(encoding="utf-8"))
    assert meta["frames"] == 36 and meta["layout_hash"] == sl.layout_hash(sl.screenshot_items(plan), sl.load_inputs(proj))

    # the 1.0 s clip was held to 1.5 s, and the screenshot is there at the end
    img = _frame(out, 1.4, tmp_path / "f.png")
    g = sl.device_geometry(layout, 540, 960, {"width": 400, "height": 800})
    s = g["screen"]
    cx, cy = int(s["x"] + s["w"] * 0.5), int(s["y"] + s["h"] * 0.5)      # centre of the blue block
    r, gg, b = img.getpixel((cx, cy))
    assert b > 180 and r < 90, (r, gg, b)
    assert min(img.getpixel((60, 480))) > 235                            # left half stays the clip (white)

    board = tool.execute({"mode": "board", "project_dir": str(proj), "kind": "layouts",
                          "output_path": str(tmp_path / "board.png")})
    assert board.success, board.error
    assert (tmp_path / "board.png").stat().st_size > 1000
