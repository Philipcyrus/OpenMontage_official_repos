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


def test_still_mode_keeps_every_generated_pixel_outside_the_screenshot(tmp_path):
    """Carousel / image: the screenshot lands where the geometry says, in its settled state (a
    highlight timed for 'later' still shows), and every pixel away from it is the generated still's."""
    import random

    from PIL import Image, ImageDraw

    proj = tmp_path / "job_still"
    for sub in ("inputs", "assets/images"):
        (proj / sub).mkdir(parents=True)
    shot = Image.new("RGB", (400, 800), "#ffffff")
    ImageDraw.Draw(shot).rectangle([100, 300, 300, 500], fill="#1a73e8")
    shot.save(proj / "inputs" / "in_01.png")
    (proj / "inputs" / "job.json").write_text(json.dumps({"pipeline": "panda-carousel"}), encoding="utf-8")
    (proj / "inputs" / "inputs.json").write_text(json.dumps([
        {"n": 1, "input_id": "in_01", "name": "s.png", "file": "in_01.png", "width": 400, "height": 800}]),
        encoding="utf-8")
    layout = {"zone": {"x": 0.5, "y": 0.2, "w": 0.45, "h": 0.6}, "frame": "none",
              "steps": [{"kind": "highlight_box", "region": {"x": 0.05, "y": 0.05, "w": 0.3, "h": 0.1},
                         "at_s": 50, "duration_s": 1}]}
    plan = {"version": "1.0", "metadata": {"aspect_ratio": "4:5"}, "scenes": [
        {"id": "slide-1", "type": "generated", "description": "x", "start_seconds": 0, "end_seconds": 1,
         "required_assets": [{"type": "image", "source": "provided", "input_id": "in_01",
                              "description": "d", "layout": layout}]}]}
    (proj / "checkpoint_scene_plan.json").write_text(json.dumps({"artifacts": {"scene_plan": plan}}),
                                                     encoding="utf-8")
    # a noisy still, so any resampling or colour shift of the background would show
    W, H = 810, 1012
    rnd = random.Random(7)
    still_img = Image.new("RGB", (W, H))
    still_img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(W * H)])
    still = proj / "assets" / "images" / "slide-1.png"
    still_img.save(still)

    out = proj / "overlay" / "stills" / "slide-1.png"
    res = ScreenOverlay().execute({"mode": "still", "project_dir": str(proj), "scene_id": "slide-1",
                                   "still_path": str(still), "output_path": str(out)})
    assert res.success, res.error
    img = Image.open(out).convert("RGB")
    assert img.size == (W, H)

    g = sl.device_geometry(layout, W, H, {"width": 400, "height": 800})
    s, d = g["screen"], g["device"]
    r, gg, b = img.getpixel((int(s["x"] + s["w"] * 0.5), int(s["y"] + s["h"] * 0.5)))   # the blue block
    assert b > 180 and r < 90, (r, gg, b)
    # settled state: the 'later' highlight is drawn (yellow edge around its region)
    hx = int(s["x"] + s["w"] * 0.05) - 1
    hy = int(s["y"] + s["h"] * 0.10)
    assert any(img.getpixel((x, hy))[0] > 200 and img.getpixel((x, hy))[2] < 80 for x in range(hx - 6, hx + 6))
    # everything 12 px or more away from the screenshot is byte-identical to the generated still
    margin = 12
    x0, y0 = int(d["x"]) - margin, int(d["y"]) - margin
    x1, y1 = int(d["x"] + d["w"]) + margin, int(d["y"] + d["h"]) + margin
    src = still_img.load()
    dst = img.load()
    diffs = sum(1 for y in range(0, H, 3) for x in range(0, W, 3)
                if not (x0 <= x <= x1 and y0 <= y <= y1) and src[x, y] != dst[x, y])
    assert diffs == 0, f"{diffs} generated pixels changed outside the screenshot"


def test_held_still_composites_onto_blank_phone_screen(tmp_path):
    """frame:held fills the blank screen rect with the upload; outside pixels stay generated."""
    from PIL import Image, ImageDraw

    proj = tmp_path / "job_held"
    for sub in ("inputs", "assets/images"):
        (proj / sub).mkdir(parents=True)
    shot = Image.new("RGB", (400, 800), "#ffffff")
    ImageDraw.Draw(shot).rectangle([40, 40, 360, 760], fill="#e11d48")
    shot.save(proj / "inputs" / "in_01.png")
    (proj / "inputs" / "job.json").write_text(json.dumps({"pipeline": "panda-image"}), encoding="utf-8")
    (proj / "inputs" / "inputs.json").write_text(json.dumps([
        {"n": 1, "input_id": "in_01", "name": "s.png", "file": "in_01.png", "width": 400, "height": 800}]),
        encoding="utf-8")
    layout = {
        "zone": {"x": 0.35, "y": 0.25, "w": 0.30, "h": 0.45},
        "subject_zone": {"x": 0.15, "y": 0.10, "w": 0.70, "h": 0.80},
        "frame": "held",
    }
    plan = {"version": "1.0", "metadata": {"aspect_ratio": "1:1"}, "scenes": [
        {"id": "slide-1", "type": "generated", "description": "x", "start_seconds": 0, "end_seconds": 1,
         "required_assets": [{"type": "image", "source": "provided", "input_id": "in_01",
                              "description": "d", "layout": layout}]}]}
    (proj / "checkpoint_scene_plan.json").write_text(json.dumps({"artifacts": {"scene_plan": plan}}),
                                                     encoding="utf-8")
    W = H = 1024
    still_img = Image.new("RGB", (W, H), "#fdc50d")
    ImageDraw.Draw(still_img).rectangle(
        [int(0.35 * W), int(0.25 * H), int(0.65 * W), int(0.70 * H)], fill="#ffffff")
    still = proj / "assets" / "images" / "slide-1.png"
    still_img.save(still)

    out = proj / "overlay" / "stills" / "slide-1.png"
    res = ScreenOverlay().execute({"mode": "still", "project_dir": str(proj), "scene_id": "slide-1",
                                   "still_path": str(still), "output_path": str(out)})
    assert res.success, res.error
    img = Image.open(out).convert("RGB")
    g = sl.device_geometry(layout, W, H, {"width": 400, "height": 800})
    s = g["screen"]
    r, gg, b = img.getpixel((int(s["x"] + s["w"] * 0.5), int(s["y"] + s["h"] * 0.5)))
    assert r > 180 and gg < 90 and b < 120, (r, gg, b)
    assert img.getpixel((20, 20)) == (0xfd, 0xc5, 0x0d)


def _still_job(proj: Path, layout: dict, aspect: str = "4:5") -> None:
    """A one-slide carousel project with one screenshot placement."""
    from PIL import Image, ImageDraw

    for sub in ("inputs", "assets/images"):
        (proj / sub).mkdir(parents=True, exist_ok=True)
    shot = Image.new("RGB", (400, 800), "#ffffff")
    ImageDraw.Draw(shot).rectangle([100, 300, 300, 500], fill="#1a73e8")
    shot.save(proj / "inputs" / "in_01.png")
    (proj / "inputs" / "job.json").write_text(json.dumps({"pipeline": "panda-carousel"}),
                                              encoding="utf-8")
    (proj / "inputs" / "inputs.json").write_text(json.dumps([
        {"n": 1, "input_id": "in_01", "name": "s.png", "file": "in_01.png",
         "width": 400, "height": 800}]), encoding="utf-8")
    plan = {"version": "1.0", "metadata": {"aspect_ratio": aspect}, "scenes": [
        {"id": "slide-1", "type": "generated", "description": "x", "start_seconds": 0,
         "end_seconds": 1, "required_assets": [{"type": "image", "source": "provided",
                                                "input_id": "in_01", "description": "d",
                                                "layout": layout}]}]}
    (proj / "checkpoint_scene_plan.json").write_text(json.dumps({"artifacts": {"scene_plan": plan}}),
                                                     encoding="utf-8")


def test_still_mode_handles_jpeg_rotated_and_high_bit_depth_stills(tmp_path):
    """A JPEG still keeps its colour profile, an EXIF-rotated still is composited as it is shown,
    and a 16-bit still is not flattened to black and white."""
    from PIL import Image, ImageDraw, ImageOps

    proj = tmp_path / "job_formats"
    layout = {"zone": {"x": 0.5, "y": 0.25, "w": 0.45, "h": 0.55}, "frame": "none"}
    _still_job(proj, layout)
    overlay = ScreenOverlay()

    # 1) JPEG with an ICC profile: kept, and the grey background survives the one re-encode
    W, H = 1024, 1280
    grey = Image.new("RGB", (W, H), (120, 130, 140))
    ImageDraw.Draw(grey).rectangle([40, 40, 300, 300], fill=(30, 40, 50))
    icc = (Path(sys.prefix) / "nonexistent").read_bytes() if False else None
    try:
        from PIL import ImageCms

        icc = ImageCms.createProfile("sRGB")
        icc = ImageCms.ImageCmsProfile(icc).tobytes()
    except Exception:  # noqa: BLE001 — ImageCms is optional
        icc = None
    jpg = proj / "assets" / "images" / "slide-1.jpg"
    grey.save(jpg, "JPEG", quality=95, **({"icc_profile": icc} if icc else {}))
    out = proj / "overlay" / "stills" / "slide-1.jpg"
    res = overlay.execute({"mode": "still", "project_dir": str(proj), "scene_id": "slide-1",
                           "still_path": str(jpg), "output_path": str(out)})
    assert res.success, res.error
    with Image.open(out) as got:
        assert got.size == (W, H) and got.format == "JPEG"
        if icc:
            assert got.info.get("icc_profile"), "the JPEG still lost its colour profile"
        far = got.convert("RGB").getpixel((20, H - 20))
    assert all(abs(a - b) <= 6 for a, b in zip(far, (120, 130, 140))), far

    # 2) the same still with an EXIF orientation tag: composited on the image as DISPLAYED
    rot = Image.new("RGB", (H, W), (120, 130, 140))          # stored sideways
    exif = rot.getexif()
    exif[274] = 6                                            # rotate 90° CW when displayed
    rotated = proj / "assets" / "images" / "slide-1.jpg"
    rot.save(rotated, "JPEG", quality=95, exif=exif)
    res = overlay.execute({"mode": "still", "project_dir": str(proj), "scene_id": "slide-1",
                           "still_path": str(rotated), "output_path": str(out)})
    assert res.success, res.error
    with Image.open(out) as got:
        shown = ImageOps.exif_transpose(got)
        assert shown.size == (W, H), f"the composite is not upright: {got.size} / {shown.size}"
        g = sl.device_geometry(layout, W, H, {"width": 400, "height": 800})["screen"]
        r, gg, b = shown.convert("RGB").getpixel((int(g["x"] + g["w"] * 0.5),
                                                  int(g["y"] + g["h"] * 0.5)))
    assert b > 150 and r < 110, (r, gg, b)

    # 3) a 16-bit greyscale PNG still: scaled to 8-bit, not clipped to pure black and white
    png16 = proj / "assets" / "images" / "slide-1.png"
    deep = Image.new("I;16", (512, 640), 30000)
    deep.save(png16)
    out16 = proj / "overlay" / "stills" / "slide-1.png"
    res = overlay.execute({"mode": "still", "project_dir": str(proj), "scene_id": "slide-1",
                           "still_path": str(png16), "output_path": str(out16)})
    assert res.success, res.error
    with Image.open(out16) as got:
        val = got.convert("L").getpixel((10, 10))
    assert 100 < val < 140, f"16-bit still flattened to {val} (30000/257 = 117)"


def test_board_shows_sequential_screenshots_in_separate_previews(tmp_path):
    """Two screenshots in the same place, one after the other: BOTH must be reviewable.

    Drawn on one preview the later one simply covers the earlier one, and the gate shows a layout
    nobody can check. Each display window gets its own cell, rendered at a moment inside it.
    """
    from PIL import Image, ImageDraw

    proj = tmp_path / "job_board_seq"
    for sub in ("inputs", "assets/video", "artifacts"):
        (proj / sub).mkdir(parents=True)
    for iid, colour in (("in_01", "#1a73e8"), ("in_02", "#0f9d58")):
        shot = Image.new("RGB", (400, 800), "#ffffff")
        ImageDraw.Draw(shot).rectangle([20, 20, 380, 780], fill=colour)
        shot.save(proj / "inputs" / f"{iid}.png")
    (proj / "inputs" / "inputs.json").write_text(json.dumps([
        {"n": 1, "input_id": "in_01", "name": "a.png", "file": "in_01.png", "width": 400, "height": 800},
        {"n": 2, "input_id": "in_02", "name": "b.png", "file": "in_02.png", "width": 400, "height": 800}]),
        encoding="utf-8")
    zone = {"x": 0.30, "y": 0.20, "w": 0.40, "h": 0.50}
    a = {"zone": zone, "frame": "none", "enter": {"type": "none"}, "show": {"from_s": 0.0, "to_s": 2.0}}
    b = {"zone": zone, "frame": "none", "enter": {"type": "none"}, "show": {"from_s": 2.0, "to_s": 5.0}}
    plan = {"version": "1.0", "metadata": {"aspect_ratio": "9:16"}, "scenes": [
        {"id": "s01", "type": "character_scene", "description": "x", "start_seconds": 0,
         "end_seconds": 5, "required_assets": [
             {"type": "image", "source": "provided", "input_id": "in_01", "description": "d",
              "layout": a},
             {"type": "image", "source": "provided", "input_id": "in_02", "description": "d",
              "layout": b}]}]}
    (proj / "checkpoint_scene_plan.json").write_text(
        json.dumps({"artifacts": {"scene_plan": plan}}), encoding="utf-8")

    out = tmp_path / "board.png"
    res = ScreenOverlay().execute({"mode": "board", "project_dir": str(proj), "kind": "layouts",
                                   "output_path": str(out)})
    assert res.success, res.error
    assert res.data["cells"] == 2, res.data

    # Both cells are drawn side by side at the same scale: the left one must be blue (screenshot 1)
    # where the right one is green (screenshot 2).
    img = Image.open(out).convert("RGB")
    W, H = img.size
    colours = []
    for cell in (0, 1):
        # cell centre: the board lays cells out left to right with equal widths
        cx = int(W * (0.25 + 0.5 * cell))
        strip = [img.getpixel((cx, y)) for y in range(int(H * 0.15), int(H * 0.55))]
        blue = sum(1 for r, g, bb in strip if bb > 120 and bb > r + 40 and bb > g + 20)
        green = sum(1 for r, g, bb in strip if g > 100 and g > r + 30 and g > bb + 20)
        colours.append((blue, green))
    assert colours[0][0] > 20 and colours[0][1] < 5, colours   # first window: screenshot 1 only
    assert colours[1][1] > 20 and colours[1][0] < 5, colours   # second window: screenshot 2 only
