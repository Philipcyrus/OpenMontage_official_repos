"""Contracts for user screenshots laid over Panda shots (lib/screen_layout.py,
tools/video/screen_overlay.py, schemas, directors). No Node needed."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import screen_layout as sl  # noqa: E402


def _contains(outer: dict, inner: tuple[float, float, float, float], tol: float = 0.01) -> bool:
    x0, y0, x1, y1 = inner
    return (outer["x"] <= x0 + tol and outer["y"] <= y0 + tol
            and outer["x"] + outer["w"] >= x1 - tol and outer["y"] + outer["h"] >= y1 - tol)


@pytest.mark.parametrize("canvas", sorted(sl.KEEP_OUT))
def test_keep_out_areas_cover_what_panda_render_draws(canvas):
    vendor = ROOT / "vendor"
    os.environ.setdefault("MONTAGE_BRAND_DIR", str(vendor / "brand"))
    os.environ.setdefault("MONTAGE_DATA_DIR", str(vendor / "data"))
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    try:
        from PIL import Image
        from montage_svc import storage as st
        from montage_svc.render import overlays as ov

        st.ensure_profiles()
        ugc, bgc = st.load_profile("ugc"), st.load_profile("bgc")
        W, H = canvas
        cap = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ov.draw_caption(cap, ugc, "一行中文字幕", "One English line")
        logo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ov.draw_logo(logo, bgc)
    except Exception as e:  # noqa: BLE001 — needs the CJK font the render uses
        pytest.skip(f"vendored overlays unavailable here: {e}")
    frac = lambda b: (b[0] / W, b[1] / H, b[2] / W, b[3] / H)  # noqa: E731
    cb, lb = cap.getbbox(), logo.getbbox()
    assert cb is not None
    assert _contains(sl.KEEP_OUT[canvas]["captions"], frac(cb)), (canvas, frac(cb))
    if lb is not None:
        assert _contains(sl.KEEP_OUT[canvas]["logo"], frac(lb)), (canvas, frac(lb))


def test_chrome_geometry_matches_remotion_component():
    ts = (ROOT / "remotion-composer" / "src" / "panda" / "screenGeometry.ts").read_text(encoding="utf-8")
    for frame, vals in sl.CHROME.items():
        m = re.search(rf"{frame}:\s*\{{\s*side:\s*([\d.]+),\s*top:\s*([\d.]+),\s*bottom:\s*([\d.]+)", ts)
        assert m, frame
        assert tuple(float(v) for v in m.groups()) == (vals["side"], vals["top"], vals["bottom"])


@pytest.mark.parametrize("name", ["screen_layout", "screen_requests"])
def test_schemas_are_valid(name):
    schema = json.loads((ROOT / "schemas" / "artifacts" / f"{name}.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_plan_doc_example_is_a_clean_layout():
    doc = (ROOT / "docs" / "user-screenshots-plan.md").read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)\n```", doc, re.S)
    assert block, "docs/user-screenshots-plan.md §4 example missing"
    item = json.loads(block.group(1))
    assert not sl.schema_errors(item["layout"], "screen_layout")
    plan = {"version": "1.0", "metadata": {"aspect_ratio": "9:16"}, "scenes": [
        {"id": "s01", "type": "character_scene", "description": "x", "start_seconds": 0, "end_seconds": 6,
         "required_assets": [{"type": "image", "description": "still", "source": "generate"}, item]}]}
    inputs = [{"n": 1, "input_id": "in_01", "name": "checkout.png", "width": 1170, "height": 2532}]
    requests = [{"input_id": "in_01", "scenes": [1]}]
    assert sl.validate_requests(inputs, requests) == []
    assert sl.validate_layouts(plan, inputs, requests) == []


def test_geometry_fits_screen_inside_zone_and_keeps_aspect():
    layout = {"zone": {"x": 0.4, "y": 0.1, "w": 0.56, "h": 0.6}, "frame": "phone"}
    natural = {"width": 1170, "height": 2532}
    g = sl.device_geometry(layout, 1080, 1920, natural)
    d, s = g["device"], g["screen"]
    assert d["x"] >= 0.4 * 1080 - 0.5 and d["x"] + d["w"] <= 0.96 * 1080 + 0.5
    assert d["y"] >= 0.1 * 1920 - 0.5 and d["y"] + d["h"] <= 0.7 * 1920 + 0.5
    assert abs(s["w"] / s["h"] - 1170 / 2532) < 1e-6
    r = sl.fit_region({"x": 0.2, "y": 0.55, "w": 0.6, "h": 0.35}, g["aspect"], natural)
    assert abs(r["w"] / r["h"] - g["aspect"]) < 1e-6 and r["x"] >= 0 and r["y"] + r["h"] <= 2532 + 1e-6


def test_launcher_owned_paths(tmp_path):
    proj = tmp_path / "job_x"
    for sub in ("inputs", "overlay/boards", "assets/images"):
        (proj / sub).mkdir(parents=True)
    assert sl.is_launcher_owned(proj / "inputs" / "in_01.png", proj)
    assert sl.is_launcher_owned(proj / "overlay" / "boards" / "a.png", proj)
    assert not sl.is_launcher_owned(proj / "assets" / "images" / "s01.png", proj)
    assert not sl.is_launcher_owned(tmp_path / "elsewhere.png", proj)


def test_tool_discovered_without_node(monkeypatch):
    from tools.tool_registry import ToolRegistry

    reg = ToolRegistry()
    reg.discover()
    tool = reg.get("screen_overlay")
    assert tool is not None
    assert "screen_overlay" in tool.capabilities


def test_compose_swaps_only_screenshot_scenes(tmp_path, monkeypatch):
    import tools.video.screen_overlay as so

    proj = tmp_path / "job_c"
    (proj / "inputs").mkdir(parents=True)
    (proj / "assets" / "video").mkdir(parents=True)
    (proj / "inputs" / "inputs.json").write_text(json.dumps(
        [{"n": 1, "input_id": "in_01", "name": "a.png", "file": "in_01.png", "width": 100, "height": 200}]),
        encoding="utf-8")
    layout = {"zone": {"x": 0.4, "y": 0.1, "w": 0.5, "h": 0.5}, "frame": "phone"}
    plan = {"version": "1.0", "scenes": [
        {"id": "s01", "type": "character_scene", "description": "x", "start_seconds": 0, "end_seconds": 5,
         "required_assets": [{"type": "image", "source": "provided", "input_id": "in_01",
                              "description": "d", "layout": layout}]},
        {"id": "s02", "type": "character_scene", "description": "y", "start_seconds": 5, "end_seconds": 9,
         "required_assets": []}]}
    (proj / "checkpoint_scene_plan.json").write_text(json.dumps({"artifacts": {"scene_plan": plan}}),
                                                     encoding="utf-8")
    for sid in ("s01", "s02"):
        (proj / "assets" / "video" / f"{sid}.mp4").write_bytes(b"x")
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "asset_manifest.json").write_text(json.dumps({"version": "1.0", "assets": [
        {"id": "c1", "type": "video", "path": "assets/video/s01.mp4", "source_tool": "t", "scene_id": "s01"},
        {"id": "c2", "type": "video", "path": "assets/video/s02.mp4", "source_tool": "t", "scene_id": "s02"}]}),
        encoding="utf-8")
    monkeypatch.setattr(so, "remotion_ready", lambda: (True, "ok"))
    calls = []

    def fake_render(self, project, scene_id, group, recs, media, duration, W, H, language):
        calls.append((scene_id, duration, W, H))
        out = project / "overlay" / f"{scene_id}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"y")
        return out

    monkeypatch.setattr(so.ScreenOverlay, "_render_scene", fake_render)
    scenes = [  # s01 without scene_id → inferred from the manifest path
        {"media_path": str(proj / "assets" / "video" / "s01.mp4"), "duration_s": 4.2, "captions": {"en": "a"}},
        {"scene_id": "s02", "media_path": str(proj / "assets" / "video" / "s02.mp4"), "duration_s": 3.0},
    ]
    res = so.ScreenOverlay().execute({"mode": "compose", "project_dir": str(proj), "scenes": scenes})
    assert res.success, res.error
    out = res.data["scenes"]
    assert out[0]["media_path"].endswith("s01.mp4") and "overlay" in out[0]["media_path"]
    assert out[0]["duration_s"] == 4.2 and out[0]["captions"] == {"en": "a"}
    assert out[1] == scenes[1]
    assert calls == [("s01", 4.2, 1080, 1920)]


def test_board_requires_uploads(tmp_path, monkeypatch):
    import tools.video.screen_overlay as so

    monkeypatch.setattr(so, "remotion_ready", lambda: (True, "ok"))
    res = so.ScreenOverlay().execute({"mode": "board", "project_dir": str(tmp_path), "kind": "uploads"})
    assert not res.success and "no user screenshots" in res.error


def test_pipeline_and_directors_wire_screenshots():
    pv = yaml.safe_load((ROOT / "pipeline_defs" / "panda-video.yaml").read_text(encoding="utf-8"))
    compose = next(s for s in pv["stages"] if s["name"] == "compose")
    assert "screen_overlay" in compose["tools_available"]
    skills = ROOT / "skills" / "pipelines" / "panda-video"
    for name, must in (("idea-director.md", "requests.json"), ("scene-plan-director.md", "screen_layout.schema.json"),
                       ("asset-director.md", "USER SCREENSHOTS"), ("compose-director.md", "screen_overlay")):
        text = (skills / name).read_text(encoding="utf-8")
        assert "USER SCREENSHOTS" in text and must in text, name


@pytest.mark.parametrize("pipeline, directors", [
    ("panda-carousel", (("idea-director.md", "requests.json"), ("script-director.md", "requests.json"),
                        ("scene-plan-director.md", "screen_layout.schema.json"),
                        ("asset-director.md", "assets/images"))),
    ("panda-image", (("idea-director.md", "requests.json"),
                     ("scene-plan-director.md", "screen_layout.schema.json"),
                     ("asset-director.md", "assets/images"))),
])
def test_still_pipeline_directors_wire_screenshots(pipeline, directors):
    skills = ROOT / "skills" / "pipelines" / pipeline
    for name, must in directors:
        text = (skills / name).read_text(encoding="utf-8")
        assert "USER SCREENSHOTS" in text and must in text, (pipeline, name)
    # the launcher places the screenshots on stills; the agent never calls the render tool for them
    asset = (skills / "asset-director.md").read_text(encoding="utf-8")
    assert "never call `screen_overlay`" in asset
    assert pipeline in sl.STILL_PIPELINES and pipeline in sl.PIPELINES


def test_still_mode_places_screenshots_on_the_still(tmp_path, monkeypatch):
    """mode=still renders at the still's own size and writes the composite; the still is untouched."""
    import hashlib

    from PIL import Image

    import tools.video.screen_overlay as so

    proj = tmp_path / "job_s"
    (proj / "inputs").mkdir(parents=True)
    (proj / "assets" / "images").mkdir(parents=True)
    Image.new("RGB", (100, 200), "#1a73e8").save(proj / "inputs" / "in_01.png")
    (proj / "inputs" / "job.json").write_text(json.dumps({"pipeline": "panda-carousel"}), encoding="utf-8")
    (proj / "inputs" / "inputs.json").write_text(json.dumps(
        [{"n": 1, "input_id": "in_01", "name": "a.png", "file": "in_01.png", "width": 100, "height": 200}]),
        encoding="utf-8")
    layout = {"zone": {"x": 0.5, "y": 0.2, "w": 0.45, "h": 0.6}, "frame": "phone"}
    plan = {"version": "1.0", "metadata": {"aspect_ratio": "4:5"}, "scenes": [
        {"id": "slide-1", "type": "generated", "description": "x", "start_seconds": 0, "end_seconds": 1,
         "required_assets": [{"type": "image", "source": "provided", "input_id": "in_01",
                              "description": "d", "layout": layout}]}]}
    (proj / "checkpoint_scene_plan.json").write_text(json.dumps({"artifacts": {"scene_plan": plan}}),
                                                     encoding="utf-8")
    still = proj / "assets" / "images" / "slide-1.png"
    Image.new("RGB", (1080, 1350), "#ffffff").save(still)
    before = hashlib.sha256(still.read_bytes()).hexdigest()
    monkeypatch.setattr(so, "remotion_ready", lambda: (True, "ok"))
    seen = {}

    def fake_run(self, cmd, timeout, cwd=None):
        props = json.loads(Path(next(a for a in cmd if a.startswith("--props="))[8:]).read_text(encoding="utf-8"))
        seen.update(props)
        out = Path(cmd[5])
        layer = Image.new("RGBA", (props["width"], props["height"]), (0, 0, 0, 0))
        layer.paste((17, 17, 17, 255), (600, 300, 900, 1000))
        layer.save(out)

    monkeypatch.setattr(so.ScreenOverlay, "_run", fake_run)
    out = proj / "overlay" / "stills" / "slide-1.png"
    res = so.ScreenOverlay().execute({"mode": "still", "project_dir": str(proj), "scene_id": "slide-1",
                                      "still_path": str(still), "output_path": str(out)})
    assert res.success, res.error
    assert seen["still"] is True and (seen["width"], seen["height"]) == (1080, 1350)
    assert seen["background"] == {"type": "none"} and seen["durationInFrames"] == 1
    img = Image.open(out).convert("RGB")
    assert img.size == (1080, 1350) and img.getpixel((700, 500)) == (17, 17, 17) and img.getpixel((50, 50)) == (255, 255, 255)
    assert hashlib.sha256(still.read_bytes()).hexdigest() == before
    bad = so.ScreenOverlay().execute({"mode": "still", "project_dir": str(proj), "scene_id": "slide-9",
                                      "still_path": str(still), "output_path": str(out)})
    assert not bad.success and "places no screenshots" in bad.error
