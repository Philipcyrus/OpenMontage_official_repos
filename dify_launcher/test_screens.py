"""User screenshots — launcher side. No LLM, no Node needed (boards are stubbed here; the real
Remotion render is covered by tests/tools/test_screen_overlay_render.py).

Proves:
  * intake: allow-listed hosts only, no redirects, size / type / pixel limits, EXIF rotation applied,
    metadata stripped, nothing created on a bad upload, media only for panda-video /
    panda-carousel / panda-image
  * the user's scene assignments are enforced (missing / extra / unplaced / moment / beyond plan)
  * layout checks: captions, logo corner, character area, legibility, zoom, same-frame overlap;
    carousel / image in slide / image words with no caption strip and no timing
  * clear-area checks on stills
  * jobs WITHOUT uploads: identical prompts, no inputs key, nothing mirrored differently
  * jobs WITH uploads: facts appended to every leg, inputs/overlay never become stills or clips,
    gate questions carry the notes, checks never raise
  * carousel / image: the stills Dify shows and the brand pass stamps carry the screenshots; the
    clean stills stay for revise; failures and busy areas are flagged at the stills gate

Run:  python dify_launcher/test_screens.py
"""

from __future__ import annotations

import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="panda_screens_"))
os.environ["OPENMONTAGE_PROJECTS_DIR"] = str(_TMP / "projects")
os.environ["DIFY_DATA_DIR"] = str(_TMP / "launcher")
os.environ["DIFY_RUNNER"] = "mock"
os.environ["DIFY_ASYNC"] = "0"
os.environ.pop("DIFY_TOKEN", None)
os.environ["DIFY_FILES_HOSTS"] = "127.0.0.1"
os.environ.pop("DIFY_FILES_BASE", None)
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from PIL import Image  # noqa: E402

from dify_launcher import runner as R  # noqa: E402
from dify_launcher import screens  # noqa: E402
from dify_launcher import store  # noqa: E402
from lib import screen_layout as sl  # noqa: E402

PROJECTS = Path(os.environ["OPENMONTAGE_PROJECTS_DIR"])
PROJECTS.mkdir(parents=True, exist_ok=True)


def expect_error(fn, *args, contains: str = "") -> str:
    try:
        fn(*args)
    except screens.IntakeError as e:
        assert contains.lower() in str(e).lower(), f"{e!r} does not mention {contains!r}"
        return str(e)
    raise AssertionError(f"{fn.__name__}{args!r} did not raise IntakeError")


def png_bytes(w: int, h: int, color: str = "#ffffff") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1) media_items / resolve_url
# ---------------------------------------------------------------------------
assert screens.media_items({}) == []
assert screens.media_items({"media": []}) == []
expect_error(screens.media_items, {"media": "x"}, contains="list")
expect_error(screens.media_items, {"media": [{"name": "a"}]}, contains="url")
expect_error(screens.media_items, {"media": [{"url": "http://127.0.0.1/a"}] * (screens.MAX_FILES + 1)},
             contains="too many")
items = screens.media_items({"media": [{"url": "http://127.0.0.1/x/check<out>.png", "name": 'a/b:"c".png'},
                                       "http://127.0.0.1/y/shot.png"]})
assert items[0]["name"] == "abc.png" and items[1]["name"] == "shot.png", items

old_hosts = os.environ.pop("DIFY_FILES_HOSTS")
expect_error(screens.resolve_url, "http://127.0.0.1/a.png", contains="not enabled")
os.environ["DIFY_FILES_HOSTS"] = old_hosts
expect_error(screens.resolve_url, "/files/abc/file-preview?sign=x", contains="DIFY_FILES_BASE")
os.environ["DIFY_FILES_BASE"] = "https://dify.example.com"
assert screens.resolve_url("/files/abc/file-preview?sign=x") == "https://dify.example.com/files/abc/file-preview?sign=x"
expect_error(screens.resolve_url, "https://evil.example.net/a.png", contains="not an allowed")
expect_error(screens.resolve_url, "ftp://127.0.0.1/a.png", contains="http")
os.environ.pop("DIFY_FILES_BASE")
print("[ok] media list + link allow-list (relative links need DIFY_FILES_BASE; other hosts refused)")

# ---------------------------------------------------------------------------
# 2) fetch + normalise against a local server
# ---------------------------------------------------------------------------
exif_jpeg = io.BytesIO()
_im = Image.new("RGB", (40, 20), "#ff0000")
_exif = Image.Exif()
_exif[0x0112] = 6            # rotate 90° CW on display
_exif[0x010F] = "SecretCam"  # Make — must not survive
_im.save(exif_jpeg, format="JPEG", exif=_exif)
gif = io.BytesIO()
Image.new("RGB", (10, 10), "#00ff00").save(gif, format="GIF")

FILES = {
    "/ok.png": png_bytes(120, 260, "#eeeeee"),
    "/wide.png": png_bytes(300, 100, "#dddddd"),
    "/exif.jpg": exif_jpeg.getvalue(),
    "/anim.gif": gif.getvalue(),
    "/notimage.png": b"<html>not an image</html>",
    "/big.png": b"\x89PNG" + b"0" * (int(screens.MAX_MB * 1024 * 1024) + 10),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok.png")
            self.end_headers()
            return
        body = FILES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_server.server_address[1]}"

import time  # noqa: E402

deadline = time.monotonic() + 30
assert screens.fetch(BASE + "/ok.png", deadline) == FILES["/ok.png"]
expect_error(screens.fetch, BASE + "/redirect", deadline, contains="redirect")
expect_error(screens.fetch, BASE + "/missing.png", deadline, contains="404")
expect_error(screens.fetch, BASE + "/big.png", deadline, contains="larger than")

png, w, h = screens.normalise(FILES["/exif.jpg"])
assert (w, h) == (20, 40), f"EXIF rotation not applied: {(w, h)}"
with Image.open(io.BytesIO(png)) as out:
    assert out.format == "PNG"
    assert not out.info.get("exif") and 0x010F not in out.getexif(), "metadata must be stripped"
expect_error(screens.normalise, FILES["/anim.gif"], contains="unsupported")
expect_error(screens.normalise, FILES["/notimage.png"], contains="not a readable image")
_saved_pixels = screens.MAX_PIXELS
screens.MAX_PIXELS = 1000
expect_error(screens.normalise, FILES["/ok.png"], contains="too many pixels")
screens.MAX_PIXELS = _saved_pixels
print("[ok] fetch: no redirects, 404 and oversize refused; normalise: EXIF rotation, metadata stripped, GIF/non-image/pixel cap refused")

# ---------------------------------------------------------------------------
# 3) POST /jobs — media accepted for panda-video / panda-carousel / panda-image; bad media creates nothing
# ---------------------------------------------------------------------------
import tools.video.screen_overlay as so_mod  # noqa: E402

so_mod.remotion_ready = lambda: (True, "ok")          # the render itself is tested elsewhere
from fastapi.testclient import TestClient  # noqa: E402

from dify_launcher.app import app  # noqa: E402

client = TestClient(app)


def job_dirs() -> set[str]:
    return {p.name for p in store.JOBS_DIR.iterdir()} if store.JOBS_DIR.exists() else set()


brief = "A 30s video showing how to activate a Panda eSIM on the checkout page."
r = client.post("/jobs", json={"brief": brief, "pipeline": "panda-video",
                               "options": {"media": [{"url": BASE + "/ok.png", "name": "checkout.png"},
                                                     {"url": BASE + "/wide.png", "name": "site.png"}]}})
assert r.status_code == 200, r.text
body = r.json()
assert body["inputs"] == [{"n": 1, "name": "checkout.png"}, {"n": 2, "name": "site.png"}], body.get("inputs")
jid = body["job_id"]
recs = sl.load_inputs(PROJECTS / jid)
assert [x["input_id"] for x in recs] == ["in_01", "in_02"] and recs[1]["width"] == 300, recs
assert (PROJECTS / jid / "inputs" / "in_01.png").is_file()
state = store.load_state(jid)
assert "media" not in (state.get("options") or {}), "signed links must not stay in job state"
assert client.get(f"/jobs/{jid}").json()["inputs"] == body["inputs"]
assert sl.pipeline_of(PROJECTS / jid) == "panda-video"
for still_pipeline in ("panda-carousel", "panda-image"):
    rs = client.post("/jobs", json={"brief": brief, "pipeline": still_pipeline,
                                    "options": {"language": "en", "media": [{"url": BASE + "/ok.png"}]}})
    assert rs.status_code == 200 and rs.json()["inputs"] == [{"n": 1, "name": "ok.png"}], rs.text
    assert sl.pipeline_of(PROJECTS / rs.json()["job_id"]) == still_pipeline
    assert json.loads((PROJECTS / rs.json()["job_id"] / "inputs" / "job.json").read_text())["language"] == "en"

before = job_dirs()
for bad, want in (
    ({"pipeline": "hybrid", "options": {"media": [{"url": BASE + "/ok.png"}]}}, "panda-carousel"),
    ({"pipeline": "panda-video", "options": {"media": [{"url": "https://evil.example.net/a.png"}]}}, "allowed"),
    ({"pipeline": "panda-video", "options": {"media": [{"url": BASE + "/notimage.png"}]}}, "image"),
    ({"pipeline": "panda-video", "options": {"media": [{"url": BASE + "/redirect"}]}}, "redirect"),
):
    rr = client.post("/jobs", json={"brief": brief, **bad})
    assert rr.status_code == 400 and want in rr.json()["detail"], (bad, rr.status_code, rr.text)
assert job_dirs() == before, "a rejected upload must not create a job"

so_mod.remotion_ready = lambda: (False, "Node 18 is too old")
rr = client.post("/jobs", json={"brief": brief, "options": {"media": [{"url": BASE + "/ok.png"}]}})
assert rr.status_code == 400 and "Remotion" in rr.json()["detail"], rr.text
so_mod.remotion_ready = lambda: (True, "ok")

plain = client.post("/jobs", json={"brief": brief, "pipeline": "panda-video", "options": {"language": "en"}})
assert plain.status_code == 200 and "inputs" not in plain.json()
assert not (PROJECTS / plain.json()["job_id"] / "inputs").exists()
_plain_opts = store.load_state(plain.json()["job_id"])["options"]
assert _plain_opts["language"] == "en" and "media" not in _plain_opts
print("[ok] POST /jobs: media staged before the job exists; 400 + nothing created for bad media / "
      "wrong pipeline / no Remotion; jobs without media unchanged")

# ---------------------------------------------------------------------------
# 4) assignments + layout rules
# ---------------------------------------------------------------------------
INPUTS = [
    {"n": 1, "input_id": "in_01", "name": "checkout.png", "width": 1170, "height": 2532},
    {"n": 2, "input_id": "in_02", "name": "site.png", "width": 1440, "height": 900},
    {"n": 3, "input_id": "in_03", "name": "office.jpg", "width": 800, "height": 600},
]
GOOD_PHONE = {"zone": {"x": 0.40, "y": 0.14, "w": 0.56, "h": 0.56},
              "subject_zone": {"x": 0.03, "y": 0.28, "w": 0.34, "h": 0.45}, "frame": "phone",
              "camera": "locked",
              "steps": [{"kind": "blur_region", "region": {"x": 0.06, "y": 0.30, "w": 0.88, "h": 0.034}},
                        {"kind": "zoom_to", "region": {"x": 0.2, "y": 0.55, "w": 0.6, "h": 0.35},
                         "at_s": 2.0, "duration_s": 0.8},
                        {"kind": "card", "text": {"zh": "点这里付款", "en": "Tap Pay"},
                         "zone": {"x": 0.42, "y": 0.64, "w": 0.52, "h": 0.05}, "at_s": 3.0, "duration_s": 1.5}]}
GOOD_WEB = {"zone": {"x": 0.04, "y": 0.14, "w": 0.60, "h": 0.30},
            "subject_zone": {"x": 0.30, "y": 0.46, "w": 0.40, "h": 0.28}, "frame": "browser", "camera": "locked"}


def plan_with(*scene_items, n_scenes: int = 3) -> dict:
    scenes = []
    for i in range(n_scenes):
        ras = [{"type": "image", "description": "still", "source": "generate"}]
        for (scene_no, iid, layout) in scene_items:
            if scene_no == i + 1:
                ras.append({"type": "image", "source": "provided", "input_id": iid,
                            "description": "shot", "layout": layout})
        scenes.append({"id": f"s{i + 1:02d}", "type": "character_scene", "description": "x",
                       "start_seconds": i * 5, "end_seconds": (i + 1) * 5, "required_assets": ras})
    return {"version": "1.0", "metadata": {"aspect_ratio": "9:16"}, "scenes": scenes}


REQS = [{"input_id": "in_01", "scenes": [1], "instruction": "zoom on Pay"},
        {"input_id": "in_02", "scenes": [2], "instruction": "show the site"},
        {"input_id": "in_03", "scenes": [], "moment": ""}]

assert sl.validate_requests(INPUTS, REQS) == []
assert sl.validate_layouts(plan_with((1, "in_01", GOOD_PHONE), (2, "in_02", GOOD_WEB)), INPUTS, REQS) == [], \
    sl.validate_layouts(plan_with((1, "in_01", GOOD_PHONE), (2, "in_02", GOOD_WEB)), INPUTS, REQS)


def notes_for(plan, reqs=REQS) -> str:
    return " | ".join(sl.validate_layouts(plan, INPUTS, reqs))


assert "scene 2 is missing screenshot 2" in notes_for(plan_with((1, "in_01", GOOD_PHONE)))
assert "scene 3 shows screenshot 1, but the user put it in scene 1" in notes_for(
    plan_with((1, "in_01", GOOD_PHONE), (2, "in_02", GOOD_WEB), (3, "in_01", GOOD_PHONE)))
assert "screenshot 3 has no scene from the user but appears" in notes_for(
    plan_with((1, "in_01", GOOD_PHONE), (2, "in_02", GOOD_WEB), (3, "in_03", GOOD_WEB)))
assert "only 3 scenes" in notes_for(plan_with((1, "in_01", GOOD_PHONE)),
                                    [{"input_id": "in_01", "scenes": [5]}])
assert "is not used in any scene" in notes_for(plan_with((1, "in_01", GOOD_PHONE)),
                                                [{"input_id": "in_02", "scenes": [], "moment": "when we explain activation"}])
# one screenshot in two scenes, two screenshots in one scene (side by side, and one after another)
two_scenes = [{"input_id": "in_01", "scenes": [1, 3]}, {"input_id": "in_02", "scenes": [1]}]
side = dict(GOOD_WEB, zone={"x": 0.04, "y": 0.02, "w": 0.34, "h": 0.22},
            subject_zone={"x": 0.03, "y": 0.28, "w": 0.34, "h": 0.45})
same_zone_later = dict(GOOD_PHONE, show={"from_s": 2.5, "to_s": 5.0})
first_only = dict(GOOD_PHONE, show={"from_s": 0.0, "to_s": 2.5})
ok_plan = plan_with((1, "in_01", first_only), (1, "in_02", dict(same_zone_later, zone=GOOD_PHONE["zone"])),
                    (3, "in_01", GOOD_PHONE))
assert "overlap on screen" not in notes_for(ok_plan, two_scenes), notes_for(ok_plan, two_scenes)
clash_plan = plan_with((1, "in_01", GOOD_PHONE), (1, "in_02", dict(GOOD_WEB, zone=GOOD_PHONE["zone"])),
                       (3, "in_01", GOOD_PHONE))
assert "overlap on screen at the same time" in notes_for(clash_plan, two_scenes)
assert "missing screenshot" not in notes_for(ok_plan, two_scenes)

low = dict(GOOD_PHONE, zone={"x": 0.40, "y": 0.40, "w": 0.56, "h": 0.50})
assert "caption strip" in notes_for(plan_with((1, "in_01", low), (2, "in_02", GOOD_WEB)))
high = dict(GOOD_PHONE, zone={"x": 0.40, "y": 0.02, "w": 0.56, "h": 0.56})
assert "Panda logo" in notes_for(plan_with((1, "in_01", high), (2, "in_02", GOOD_WEB)))
covering = dict(GOOD_PHONE, subject_zone={"x": 0.45, "y": 0.2, "w": 0.3, "h": 0.3})
assert "covers the character area" in notes_for(plan_with((1, "in_01", covering), (2, "in_02", GOOD_WEB)))
tiny = dict(GOOD_WEB, zone={"x": 0.04, "y": 0.14, "w": 0.30, "h": 0.12})
assert "too small" in notes_for(plan_with((1, "in_01", GOOD_PHONE), (2, "in_02", tiny)))
flat_zoom = dict(GOOD_PHONE, steps=[{"kind": "zoom_to", "region": {"x": 0, "y": 0.4, "w": 1.0, "h": 0.3},
                                     "at_s": 1, "duration_s": 1}])
assert "barely zooms" in notes_for(plan_with((1, "in_01", flat_zoom), (2, "in_02", GOOD_WEB)))
late = dict(GOOD_PHONE, steps=[{"kind": "highlight_box", "region": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1},
                                "at_s": 4.5, "duration_s": 2}])
assert "runs outside the scene" in notes_for(plan_with((1, "in_01", late), (2, "in_02", GOOD_WEB)))
bad_frame = dict(GOOD_PHONE, frame="tablet")
assert "frame must be one of" in notes_for(plan_with((1, "in_01", bad_frame), (2, "in_02", GOOD_WEB)))

assert "missing" in " ".join(sl.validate_requests(INPUTS, None))
dup = REQS + [{"input_id": "in_01", "scenes": [2]}]
assert "listed 2 times" in " ".join(sl.validate_requests(INPUTS, dup))
assert "unknown screenshot id" in " ".join(sl.validate_requests(INPUTS, REQS + [{"input_id": "in_09", "scenes": []}]))
assert "missing from requests.json" in " ".join(sl.validate_requests(INPUTS, REQS[:2]))
print("[ok] assignments binding (missing / extra / unplaced / moment / beyond plan / two scenes / two in one scene)")
print("[ok] layout checks: captions, logo corner, character area, legibility, zoom, timing, schema")

# carousel / image: slide wording, no caption strip, no timing, logo corner out to the edge
SLIDE_PHONE = {"zone": {"x": 0.42, "y": 0.25, "w": 0.54, "h": 0.69},
               "subject_zone": {"x": 0.03, "y": 0.40, "w": 0.36, "h": 0.55}, "frame": "phone",
               "crop": {"x": 0, "y": 0.1, "w": 1, "h": 0.5},
               "steps": [{"kind": "blur_region", "region": {"x": 0.06, "y": 0.30, "w": 0.88, "h": 0.034}},
                         {"kind": "highlight_box", "region": {"x": 0.1, "y": 0.5, "w": 0.8, "h": 0.08},
                          "at_s": 99, "duration_s": 5},
                         {"kind": "card", "text": {"zh": "点这里", "en": "Tap"},
                          "zone": {"x": 0.45, "y": 0.94, "w": 0.5, "h": 0.05}}]}
SLIDE_WEB = {"zone": {"x": 0.04, "y": 0.25, "w": 0.92, "h": 0.42},
             "subject_zone": {"x": 0.30, "y": 0.70, "w": 0.40, "h": 0.28}, "frame": "browser"}


def slide_plan(*items, n_scenes: int = 3, aspect: str = "4:5") -> dict:
    p = plan_with(*items, n_scenes=n_scenes)
    p["metadata"] = {"aspect_ratio": aspect}
    return p


def car_notes(plan, reqs=REQS, pipeline="panda-carousel") -> str:
    return " | ".join(sl.validate_layouts(plan, INPUTS, reqs, pipeline=pipeline))


clean_slides = slide_plan((1, "in_01", SLIDE_PHONE), (2, "in_02", SLIDE_WEB))
assert car_notes(clean_slides) == "", car_notes(clean_slides)
video_view = " | ".join(sl.validate_layouts(clean_slides, INPUTS, REQS))
assert "caption strip" in video_view and "runs outside the scene" in video_view, video_view
assert "slide 2 is missing screenshot 2" in car_notes(slide_plan((1, "in_01", SLIDE_PHONE)))
assert "but the plan has only 3 slides" in car_notes(slide_plan((1, "in_01", SLIDE_PHONE)),
                                                      [{"input_id": "in_01", "scenes": [5]}])
assert "screenshot 3 has no slide from the user" in car_notes(
    slide_plan((1, "in_01", SLIDE_PHONE), (2, "in_02", SLIDE_WEB), (3, "in_03", SLIDE_WEB)))
corner = dict(SLIDE_WEB, zone={"x": 0.30, "y": 0.02, "w": 0.69, "h": 0.30})
assert "if the carousel is branded" in car_notes(slide_plan((1, "in_01", SLIDE_PHONE), (2, "in_02", corner)))
# no timing on a slide: two screenshots in one zone always overlap
stacked = slide_plan((1, "in_01", dict(SLIDE_PHONE, show={"from_s": 0, "to_s": 0.5})),
                     (1, "in_02", dict(SLIDE_WEB, zone=SLIDE_PHONE["zone"], show={"from_s": 0.5, "to_s": 1})))
two_on_one = [{"input_id": "in_01", "scenes": [1]}, {"input_id": "in_02", "scenes": [1]}]
stacked_notes = car_notes(stacked, two_on_one)
assert "overlap on screen" in stacked_notes and "at the same time" not in stacked_notes, stacked_notes
# panda-image: one image
one = slide_plan((1, "in_01", SLIDE_PHONE), n_scenes=1, aspect="1:1")
img_reqs = [{"input_id": "in_01", "scenes": [1]}, {"input_id": "in_02", "scenes": [2]}]
img_notes = car_notes(one, img_reqs, "panda-image")
assert "screenshot 2 is assigned to image 2, but this job makes one image" in img_notes, img_notes
assert "the image is missing screenshot 2" in car_notes(one, [{"input_id": "in_02", "scenes": [1]}], "panda-image")
assert sl.canvas_for({}, pipeline="panda-carousel") == (1080, 1350)
assert sl.canvas_for({}, pipeline="panda-image") == (1080, 1080)
assert sl.canvas_for({"metadata": {"aspect_ratio": "3:4"}}, pipeline="panda-carousel") == (1080, 1440)
keep = sl.keep_clear_lines(clean_slides, pipeline="panda-carousel")
assert keep[0].startswith("slide 1 (s01): keep") and "slide text" in keep[0] and "camera" not in keep[0], keep
assert sl.keep_clear_lines(one, pipeline="panda-image")[0].startswith("the image (s01): keep")
assert screens._scene_note_map(["slide 2: covers it", "scene 3: no"], "panda-carousel") == {"2": "covers it"}
assert screens._scene_note_map(["the image: tight"], "panda-image") == {"1": "tight"}
print("[ok] carousel / image layout rules: slide / image wording, no caption strip or timing, "
      "logo corner, one image")

# geometry parity with the Remotion component
import re  # noqa: E402

ts = (_ENGINE_ROOT / "remotion-composer" / "src" / "panda" / "screenGeometry.ts").read_text(encoding="utf-8")
for frame, vals in sl.CHROME.items():
    m = re.search(rf"{frame}:\s*\{{\s*side:\s*([\d.]+),\s*top:\s*([\d.]+),\s*bottom:\s*([\d.]+)", ts)
    assert m, f"CHROME.{frame} not found in screenGeometry.ts"
    assert tuple(float(v) for v in m.groups()) == (vals["side"], vals["top"], vals["bottom"]), frame
print("[ok] CHROME geometry matches remotion-composer/src/panda/screenGeometry.ts")

# ---------------------------------------------------------------------------
# 5) clear-area check on stills
# ---------------------------------------------------------------------------
proj = PROJECTS / "job_clear"
(proj / "inputs").mkdir(parents=True, exist_ok=True)
(proj / "assets" / "images").mkdir(parents=True, exist_ok=True)
(proj / "artifacts").mkdir(parents=True, exist_ok=True)
clean = Image.new("RGB", (1080, 1920), "#ffffff")
clean.save(proj / "assets" / "images" / "s01.png")
busy = Image.new("RGB", (720, 1280), "#ffffff")           # different size: cover-crop mapping
from PIL import ImageDraw  # noqa: E402

ImageDraw.Draw(busy).rectangle([380, 250, 650, 700], fill="#fdc50d")
busy.save(proj / "assets" / "images" / "s02.png")
(proj / "artifacts" / "asset_manifest.json").write_text(json.dumps({"version": "1.0", "assets": [
    {"id": "a", "type": "image", "path": "assets/images/s01.png", "source_tool": "t", "scene_id": "s01"},
    {"id": "b", "type": "image", "path": "assets/images/s02.png", "source_tool": "t", "scene_id": "s02"},
]}), encoding="utf-8")
plan2 = plan_with((1, "in_01", GOOD_PHONE), (2, "in_01", GOOD_PHONE), n_scenes=2)
clear_notes = screens.still_clear_notes(proj, plan2, INPUTS)
assert len(clear_notes) == 1 and clear_notes[0].startswith("scene 2:"), clear_notes
print("[ok] still clear-area check flags only the still with something in the screenshot area")

# ---------------------------------------------------------------------------
# 6) runner: prompts unchanged without uploads; facts appended with them; mirroring guard
# ---------------------------------------------------------------------------
import subprocess  # noqa: E402

run = R.ClaudeCodeRunner()
run._projects_dir = PROJECTS
captured: list[str] = []
_real_run = subprocess.run


def _fake_run(cmd, **kw):
    if isinstance(cmd, list) and cmd and cmd[0] == run._bin:
        captured.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    return _real_run(cmd, **kw)


subprocess.run = _fake_run
try:
    (PROJECTS / "job_plain").mkdir(parents=True, exist_ok=True)
    samples = [
        run._start_prompt("job_plain", brief, {}, "panda-video"),
        run._continue_prompt("job_plain", "panda-video"),
        run._stills_approved_prompt("job_plain", {}),
        run._revise_prompt("job_plain", "scene_plan", {"answer": "shorter"}),
    ]
    for p in samples:
        run._run_agent(p, "job_plain", "leg")
    assert captured == samples, "prompts for jobs without uploads must be byte-identical"

    shots = PROJECTS / "job_shots"
    (shots / "inputs").mkdir(parents=True, exist_ok=True)
    (shots / "inputs" / "inputs.json").write_text(json.dumps(INPUTS), encoding="utf-8")
    (shots / "inputs" / "requests.json").write_text(json.dumps(REQS), encoding="utf-8")
    (shots / "checkpoint_scene_plan.json").write_text(json.dumps(
        {"stage": "scene_plan", "status": "completed",
         "artifacts": {"scene_plan": plan_with((1, "in_01", GOOD_PHONE), (2, "in_02", GOOD_WEB))}}),
        encoding="utf-8")
    captured.clear()
    run._run_agent(samples[1], "job_shots", "leg")
    sent = captured[0]
    assert sent.startswith(samples[1]) and "USER SCREENSHOTS" in sent
    for want in ("in_01", "checkout.png", "1 → scene 1", "3 → not placed", "scene 1 (s01): keep",
                 "screen_overlay mode=compose", "Captions are drawn over"):
        assert want in sent, f"facts missing {want!r}:\n{sent}"
finally:
    subprocess.run = _real_run
assert screens.facts(PROJECTS / "job_plain") == ""
print("[ok] prompts byte-identical without uploads; USER SCREENSHOTS facts appended with them")

mjob = "job_mirror_shots"
mp = PROJECTS / mjob
for sub in ("inputs", "overlay", "assets/images", "assets/video", "renders", "artifacts"):
    (mp / sub).mkdir(parents=True, exist_ok=True)
(mp / "inputs" / "in_01.png").write_bytes(png_bytes(10, 10))
(mp / "overlay" / "s01.mp4").write_bytes(b"00")
(mp / "overlay" / "boards").mkdir(parents=True, exist_ok=True)
(mp / "overlay" / "boards" / "screens_layouts.png").write_bytes(png_bytes(10, 10))
(mp / "assets" / "images" / "s01.png").write_bytes(png_bytes(10, 10))
arts = run._mirror_artifacts(mjob, {"inputs": [str(mp / "inputs" / "in_01.png"), "inputs/in_01.png"],
                                    "overlay": str(mp / "overlay" / "s01.mp4"),
                                    "board": "overlay/boards/screens_layouts.png"})
assert arts.get("stills") == ["s01.png"], arts.get("stills")
assert not arts.get("clips"), arts.get("clips")
print("[ok] inputs/ and overlay/ files never become stills or clips (folder scan + checkpoint paths)")

# ---------------------------------------------------------------------------
# 7) gate hook: notes in the question, board key set, never raises
# ---------------------------------------------------------------------------
from lib import checkpoint as cp  # noqa: E402

gjob = "job_shots"
store.ensure_job(gjob)
bad_plan = plan_with((1, "in_01", GOOD_PHONE), n_scenes=3)     # scene 2 is missing screenshot 2
(shots / "checkpoint_scene_plan.json").write_text(json.dumps(
    {"stage": "scene_plan", "status": "awaiting_human", "artifacts": {"scene_plan": bad_plan}}),
    encoding="utf-8")
rendered: list[str] = []


def _fake_board(project_dir, job_id, kind, plan, recs, notes_map, language):
    rendered.append(kind)
    name = screens.BOARD_FILES[kind]
    store.artifact_path(job_id, name).write_bytes(png_bytes(8, 8))
    return name


screens.render_board = _fake_board
_real_latest, _real_next = cp.get_latest_checkpoint, cp.get_next_stage
cp.get_latest_checkpoint = lambda _pd, _jid: {"stage": "scene_plan", "status": "awaiting_human",
                                               "artifacts": {"scene_plan": bad_plan}}
try:
    st = run._sync({"job_id": gjob, "pipeline": "panda-video", "options": {"language": "zh"}})
    assert st["gate"] == "approve_scene_plan" and st["status"] == "awaiting_human"
    assert "Your screenshots — please check" in st["question"], st["question"]
    assert "scene 2 is missing screenshot 2" in st["question"]
    assert st["artifacts"].get("screens_board") == "screens_layouts.png" and rendered == ["layouts"]
    md = store.artifact_path(gjob, "scene_plan.md").read_text(encoding="utf-8")
    assert "## Your screenshots" in md and "## Screenshots in this plan" in md and "Scene 1" in md

    # a job without uploads gets exactly the old question and no board
    cp.get_latest_checkpoint = lambda _pd, _jid: {"stage": "scene_plan", "status": "awaiting_human",
                                                   "artifacts": {"scene_plan": bad_plan}}
    st2 = run._sync({"job_id": "job_plain", "pipeline": "panda-video", "options": {}})
    assert st2["question"] == R._question_for_gate("approve_scene_plan", stage="scene_plan")
    assert "screens_board" not in st2["artifacts"]

    # checks never raise
    _real_plan = sl.load_scene_plan
    sl.load_scene_plan = lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))
    notes = screens.apply_gate(PROJECTS, gjob, "approve_scene_plan", {})
    assert any("could not run" in n for n in notes), notes
    sl.load_scene_plan = _real_plan
    screens.render_board = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no chromium"))
    notes = screens.apply_gate(PROJECTS, gjob, "approve_scene_plan", {})
    assert any("could not be rendered" in n for n in notes), notes
finally:
    cp.get_latest_checkpoint, cp.get_next_stage = _real_latest, _real_next
print("[ok] gate: notes appended to the question, board attached, markdown sections added; "
      "no change for jobs without uploads; checks never raise")

# tampered upload is reported
(shots / "inputs" / "inputs.json").write_text(json.dumps([dict(INPUTS[0], sha256="0" * 64)]), encoding="utf-8")
(shots / "inputs" / "in_01.png").write_bytes(png_bytes(5, 5))
assert any("changed after upload" in n for n in screens.integrity_notes(shots, sl.load_inputs(shots)))
print("[ok] a changed upload is reported at the gate")

# ---------------------------------------------------------------------------
# 8) final-video check: finds the screenshot when it is there, flags it when it is not (ffmpeg only)
# ---------------------------------------------------------------------------
if shutil.which("ffmpeg"):
    fp = PROJECTS / "job_final"
    for sub in ("inputs", "overlay", "assets/video", "artifacts"):
        (fp / sub).mkdir(parents=True, exist_ok=True)
    frecs = [dict(INPUTS[0], sha256="x")]
    fplan = plan_with((1, "in_01", dict(GOOD_PHONE, steps=[])), n_scenes=1)
    g = sl.device_box_fraction(GOOD_PHONE, 540, 960, {"width": 1170, "height": 2532})
    box = f"x={int(g['x'] * 540)}:y={int(g['y'] * 960)}:w={int(g['w'] * 540)}:h={int(g['h'] * 960)}"
    raw_clip = fp / "assets" / "video" / "s01.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=white:s=540x960:r=24:d=5",
                    "-pix_fmt", "yuv420p", str(raw_clip)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_clip), "-vf",
                    f"drawbox={box}:color=0x1a73e8@1:t=fill,drawbox=x=300:y=300:w=120:h=40:color=black@1:t=fill",
                    "-pix_fmt", "yuv420p", str(fp / "overlay" / "s01.mp4")], check=True)
    (fp / "artifacts" / "asset_manifest.json").write_text(json.dumps({"version": "1.0", "assets": [
        {"id": "c", "type": "video", "path": "assets/video/s01.mp4", "source_tool": "t", "scene_id": "s01"}]}),
        encoding="utf-8")
    (fp / "overlay" / "s01.json").write_text(json.dumps(
        {"layout_hash": sl.layout_hash(sl.screenshot_items(fplan), frecs), "duration_s": 5.0}), encoding="utf-8")
    fplan["metadata"] = {"aspect_ratio": "9:16"}
    canvas_plan = dict(fplan)
    # the final is the overlay clip itself (what panda_render would carry) -> found
    assert screens.final_notes(fp, canvas_plan, frecs, fp / "overlay" / "s01.mp4") == []
    # a final rendered from the raw clip -> flagged
    assert screens.final_notes(fp, canvas_plan, frecs, raw_clip) == [
        "scene 1: its screenshot could not be found in the final video"]
    # layout edited after the overlay was rendered -> flagged
    edited = plan_with((1, "in_01", dict(GOOD_PHONE, steps=[], frame="card")), n_scenes=1)
    assert any("layout changed" in n for n in screens.final_notes(fp, edited, frecs, fp / "overlay" / "s01.mp4"))
    # compose never ran -> flagged
    (fp / "overlay" / "s01.json").unlink()
    assert any("were not rendered" in n for n in screens.final_notes(fp, canvas_plan, frecs, fp / "overlay" / "s01.mp4"))
    print("[ok] final check: screenshot found when present; flagged when missing, stale or never rendered")
else:
    print("[skip] final check (ffmpeg not on PATH)")

# ---------------------------------------------------------------------------
# 9) carousel / image: screenshots placed onto the stills (render stubbed; real render in
#    tests/tools/test_screen_overlay_render.py)
# ---------------------------------------------------------------------------
import hashlib  # noqa: E402
import math  # noqa: E402


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def still_project(job: str, pipeline: str, plan: dict, reqs: list) -> Path:
    pj = PROJECTS / job
    for sub in ("inputs", "assets/images", "artifacts"):
        (pj / sub).mkdir(parents=True, exist_ok=True)
    recs = []
    for rec in INPUTS[:2]:
        data = png_bytes(rec["width"] // 10, rec["height"] // 10)
        (pj / "inputs" / f"{rec['input_id']}.png").write_bytes(data)
        recs.append(dict(rec, file=f"{rec['input_id']}.png", sha256=hashlib.sha256(data).hexdigest()))
    (pj / "inputs" / "job.json").write_text(json.dumps({"pipeline": pipeline, "language": "zh"}))
    (pj / "inputs" / "inputs.json").write_text(json.dumps(recs))
    (pj / "inputs" / "requests.json").write_text(json.dumps(reqs))
    (pj / "checkpoint_scene_plan.json").write_text(json.dumps(
        {"stage": "scene_plan", "status": "completed", "artifacts": {"scene_plan": plan}}), encoding="utf-8")
    return pj


def draw_slide(path: Path, busy_zone: bool = False, tint: str = "#ffffff") -> None:
    im = Image.new("RGB", (1080, 1350), tint)
    d = ImageDraw.Draw(im)
    d.rectangle([60, 600, 380, 1260], fill="#111111")        # the panda, left, in its subject_zone
    if busy_zone:
        d.rectangle([600, 400, 900, 700], fill="#fdc50d")    # slide copy painted where the screenshot goes
    im.save(path)


cjob = "job_carousel_shots"
cpj = still_project(cjob, "panda-carousel", slide_plan((1, "in_01", SLIDE_PHONE), n_scenes=2),
                    [{"input_id": "in_01", "scenes": [1]}, {"input_id": "in_02", "scenes": []}])
raw1, raw2 = cpj / "assets" / "images" / "slide-1.png", cpj / "assets" / "images" / "slide-2.png"
draw_slide(raw1)
draw_slide(raw2)
(cpj / "artifacts" / "asset_manifest.json").write_text(json.dumps({"version": "1.0", "assets": [
    {"id": "a", "type": "image", "path": "assets/images/slide-1.png", "source_tool": "t", "scene_id": "s01"},
    {"id": "b", "type": "image", "path": "assets/images/slide-2.png", "source_tool": "t", "scene_id": "s02"},
]}), encoding="utf-8")

BLUE = (26, 115, 232)
PHONE_NATURAL = {"width": 1170, "height": 2532}
still_renders: list[str] = []


def fake_still_render(project, scene_id, still, out, language):
    still_renders.append(scene_id)
    im = Image.open(still).convert("RGB")
    g = sl.device_box_fraction(SLIDE_PHONE, im.width, im.height, PHONE_NATURAL)
    ImageDraw.Draw(im).rectangle([int(g["x"] * im.width), int(g["y"] * im.height),
                                  int((g["x"] + g["w"]) * im.width) - 1,
                                  int((g["y"] + g["h"]) * im.height) - 1], fill=BLUE)
    im.save(out)
    return True, ""


gbox = sl.device_box_fraction(SLIDE_PHONE, 1080, 1350, PHONE_NATURAL)
centre = (int((gbox["x"] + gbox["w"] / 2) * 1080), int((gbox["y"] + gbox["h"] / 2) * 1350))
clean_sha = _sha(raw1)
_real_render_still = screens._render_still
screens._render_still = fake_still_render
try:
    carts = run._mirror_artifacts(cjob, {})
    assert sorted(carts["stills"]) == ["slide-1.png", "slide-2.png"], carts.get("stills")
    assert Image.open(store.artifact_path(cjob, "slide-1.png")).convert("RGB").getpixel(centre) == BLUE, \
        "the still Dify shows must carry the screenshot"
    assert _sha(store.artifact_path(cjob, "slide-2.png")) == _sha(raw2), "a slide without screenshots stays as generated"
    assert _sha(raw1) == clean_sha, "the clean still under assets/images is never touched"
    assert still_renders == ["s01"]
    run._mirror_artifacts(cjob, {})              # every sync re-copies the clean stills...
    assert still_renders == ["s01"], "cached — not rendered again"
    assert Image.open(store.artifact_path(cjob, "slide-1.png")).convert("RGB").getpixel(centre) == BLUE
    # edit-mode revise imports the clean still, never the copy with the screenshot
    assert R._still_abs_paths(cjob, {"artifacts": carts}, [1], PROJECTS) == [str(raw1.resolve())]
    # the brand pass stamps the still that carries the screenshot
    try:
        R._apply_brand({"job_id": cjob, "artifacts": dict(carts)})
        stamped = Image.open(store.artifact_path(cjob, "slide-1.bgc.png")).convert("RGB")
        assert stamped.getpixel(centre) == BLUE, "branded slide lost the screenshot"
        print("[ok] brand pass stamps the slide with its screenshot")
    except ImportError as e:
        print(f"[skip] brand pass ({e})")
    # a regenerated still gets its screenshot again
    draw_slide(raw1, tint="#fefefe")
    carts = run._mirror_artifacts(cjob, {})
    assert still_renders == ["s01", "s01"], still_renders

    # gate: no extra board and no notes when all is well
    cp.get_latest_checkpoint = lambda _pd, _jid: {"stage": "assets", "status": "awaiting_human",
                                                   "partial_progress": {"phase": "stills"}, "artifacts": {}}
    st = run._sync({"job_id": cjob, "pipeline": "panda-carousel", "options": {"language": "zh"}})
    assert st["gate"] == "approve_stills", st.get("gate")
    assert "Your screenshots" not in (st.get("question") or ""), st.get("question")
    assert "screens_board" not in st["artifacts"]

    # slide copy painted into the screenshot area is flagged, by slide
    draw_slide(raw1, busy_zone=True)
    notes = screens.apply_gate(PROJECTS, cjob, "approve_stills", run._mirror_artifacts(cjob, {}))
    assert any(n.startswith("slide 1: the still has the character, props or slide text") for n in notes), notes

    # --- regressions found by the adversarial test pass -----------------------
    def two_slide_project(job, ids=("s01", "s02")):
        scenes = []
        for i, sid in enumerate(ids, 1):
            scenes.append({"id": sid, "type": "character_scene", "description": "x",
                           "start_seconds": i - 1, "end_seconds": i, "required_assets": [
                               {"type": "image", "description": "still", "source": "generate"},
                               {"type": "image", "source": "provided", "input_id": f"in_{i:02d}",
                                "description": "shot", "layout": SLIDE_PHONE}]})
        pj = still_project(job, "panda-carousel",
                           {"version": "1.0", "metadata": {"aspect_ratio": "4:5"}, "scenes": scenes},
                           [{"input_id": "in_01", "scenes": [1]}, {"input_id": "in_02", "scenes": [2]}])
        rows = []
        for i, sid in enumerate(ids, 1):
            draw_slide(pj / "assets" / "images" / f"slide-{i}.png", tint=f"#fffff{i}")
            rows.append({"id": f"a{i}", "type": "image", "path": f"assets/images/slide-{i}.png",
                         "source_tool": "t", "scene_id": sid})
        (pj / "artifacts" / "asset_manifest.json").write_text(
            json.dumps({"version": "1.0", "assets": rows}), encoding="utf-8")
        return pj

    # a cached composite is published BEFORE any render starts, so an unchanged slide is never
    # served as the clean still while another slide renders
    tjob = "job_two_slides"
    tpj = two_slide_project(tjob)
    run._mirror_artifacts(tjob, {})
    placed_1 = _sha(store.artifact_path(tjob, "slide-1.png"))
    assert placed_1 != _sha(tpj / "assets" / "images" / "slide-1.png")
    draw_slide(tpj / "assets" / "images" / "slide-2.png", tint="#fffef0")   # only slide 2 changed
    order_seen = []

    def order_render(project, scene_id, still, out, language):
        order_seen.append(scene_id)
        assert _sha(store.artifact_path(tjob, "slide-1.png")) == placed_1, \
            "slide 1 was served clean while slide 2 rendered"
        return fake_still_render(project, scene_id, still, out, language)

    screens._render_still = order_render
    run._mirror_artifacts(tjob, {})
    assert order_seen == ["s02"], order_seen

    # one slide's failure does not skip the others, and it counts against the retry cap
    def one_bad_render(project, scene_id, still, out, language):
        if scene_id == "s01":
            raise RuntimeError("boom")
        return fake_still_render(project, scene_id, still, out, language)

    bjob = "job_slide_isolation"
    bpj = two_slide_project(bjob)
    screens._render_still = one_bad_render
    barts = run._mirror_artifacts(bjob, {})
    bstatus = json.loads((bpj / "overlay" / "stills" / "status.json").read_text(encoding="utf-8"))
    assert bstatus["s01"]["failures"] == 1 and "boom" in bstatus["s01"]["error"], bstatus
    assert bstatus["s02"]["composite_sha"], "the second slide must still be placed"
    assert _sha(store.artifact_path(bjob, "slide-1.png")) == _sha(bpj / "assets" / "images" / "slide-1.png")
    bnotes = screens.apply_gate(PROJECTS, bjob, "approve_stills", barts)
    assert any(n.startswith("slide 1: the screenshot could not be placed") for n in bnotes), bnotes
    # an unreadable still is reported per slide — the other slides keep their notes
    (bpj / "assets" / "images" / "slide-1.png").write_bytes(b"not an image")
    unotes = screens.apply_gate(PROJECTS, bjob, "approve_stills", barts)
    assert any("slide 1: its generated still could not be read" in n for n in unotes), unotes
    assert not any("checks could not run" in n for n in unotes), unotes

    # scene ids that share a sanitised prefix keep their own caches (no re-render every sync)
    hjob = "job_prefix_ids"
    two_slide_project(hjob, ids=("hook", "hook_2"))
    screens._render_still = fake_still_render
    hbefore = len(still_renders)
    for _ in range(3):
        run._mirror_artifacts(hjob, {})
    assert sorted(still_renders[hbefore:]) == ["hook", "hook_2"], still_renders[hbefore:]

    # an archived rejected take in the manifest must not hide the still that ships
    (cpj / "assets" / "images" / "rejected_s01_take2.png").write_bytes(raw1.read_bytes())
    man = json.loads((cpj / "artifacts" / "asset_manifest.json").read_text(encoding="utf-8"))
    man["assets"].append({"id": "rej", "type": "image", "scene_id": "s01", "source_tool": "t",
                          "path": "assets/images/rejected_s01_take2.png"})
    (cpj / "artifacts" / "asset_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    rarts = run._mirror_artifacts(cjob, {})
    assert Image.open(store.artifact_path(cjob, "slide-1.png")).convert("RGB").getpixel(centre) == BLUE
    assert screens.placement_notes(cpj, cjob, sl.load_scene_plan(cpj), rarts,
                                   "panda-carousel", "approve_stills") == []

    # a still the manifest does not point at is flagged at approve_stills, not shipped silently
    nojob = "job_manifest_gap"
    nopj = two_slide_project(nojob)
    (nopj / "assets" / "images" / "slide-2.png").rename(nopj / "assets" / "images" / "slide-02.png")
    narts = run._mirror_artifacts(nojob, {})
    nnotes = screens.apply_gate(PROJECTS, nojob, "approve_stills", narts)
    assert any("slide 2: no generated still for it is listed in the asset manifest" in n
               for n in nnotes), nnotes

    # out of render time: the slide is reported as pending, and the pre-brand pass places it
    pjob = "job_budget"
    ppj = two_slide_project(pjob)
    _real_budget = screens.STILLS_BUDGET_S
    screens.STILLS_BUDGET_S = -1.0
    try:
        parts = run._mirror_artifacts(pjob, {})
    finally:
        screens.STILLS_BUDGET_S = _real_budget
    pstatus = json.loads((ppj / "overlay" / "stills" / "status.json").read_text(encoding="utf-8"))
    assert pstatus["s01"].get("pending") and not pstatus["s01"].get("composite_sha"), pstatus
    pnotes = screens.apply_gate(PROJECTS, pjob, "approve_stills", parts)
    assert any("its screenshot is not on the still yet" in n for n in pnotes), pnotes
    run._place_screenshots(pjob, parts, math.inf)      # the pass that opens the brand gate
    assert screens.placement_notes(ppj, pjob, sl.load_scene_plan(ppj), parts,
                                   "panda-carousel", "approve_stills") == []

    # stills kept outside assets/images: a revise leg still gets the clean file, never the composite
    sjob = "job_stills_subdir"
    spj = two_slide_project(sjob)
    (spj / "assets" / "stills").mkdir(parents=True, exist_ok=True)
    moved = spj / "assets" / "stills" / "slide-1.png"
    (spj / "assets" / "images" / "slide-1.png").rename(moved)
    man = json.loads((spj / "artifacts" / "asset_manifest.json").read_text(encoding="utf-8"))
    man["assets"][0]["path"] = "assets/stills/slide-1.png"
    (spj / "artifacts" / "asset_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    sarts = run._mirror_artifacts(sjob, {"asset_manifest": man})
    assert "slide-1.png" in (sarts.get("stills") or []), sarts.get("stills")
    assert Image.open(store.artifact_path(sjob, "slide-1.png")).convert("RGB").getpixel(centre) == BLUE
    assert R._still_abs_paths(sjob, {"artifacts": sarts}, [1], PROJECTS) == [str(moved.resolve())], \
        "an edit revise must never be handed the copy with the screenshot"

    screens._render_still = fake_still_render
    # a failing render: shown without the screenshot, flagged, tried at most twice per version
    screens._render_still = lambda *a, **k: (still_renders.append("fail") or (False, "chromium missing"))
    draw_slide(raw1, tint="#fdfdfd")
    for _ in range(3):
        farts = run._mirror_artifacts(cjob, {})
    assert still_renders.count("fail") == 2, still_renders
    assert _sha(store.artifact_path(cjob, "slide-1.png")) == _sha(raw1)
    notes = screens.apply_gate(PROJECTS, cjob, "approve_stills", farts)
    assert any("slide 1: the screenshot could not be placed on the still (the screenshot layout "
               "could not be drawn" in n for n in notes), notes
    assert not any("chromium missing" in n for n in notes), "raw tool output must stay out of the gate"
    _fail_status = json.loads((cpj / "overlay" / "stills" / "status.json").read_text(encoding="utf-8"))
    assert "chromium missing" in _fail_status["s01"]["error"], "the cause belongs in status.json"
    # the brand gate is the last stop before the stamp: it repeats the placement notes
    assert any("could not be placed" in n
               for n in screens.apply_gate(PROJECTS, cjob, "approve_brand", farts)), "brand gate note"
finally:
    screens._render_still = _real_render_still
    cp.get_latest_checkpoint, cp.get_next_stage = _real_latest, _real_next

# nothing happens for video jobs or jobs without uploads
assert screens.place_on_stills(PROJECTS, "job_plain", {"stills": ["s01.png"]}) == []
assert screens.place_on_stills(PROJECTS, "job_shots", {"stills": ["s01.png"]}) == []

# prompt facts say who places the screenshots, in slide / image words
cfacts = screens.facts(cpj)
for want in ("slide numbers are binding", "1 → slide 1", "slide 1 (s01): keep",
             "places the screenshots onto the stills", "Canvas 1080x1350", "if the carousel is branded"):
    assert want in cfacts, f"carousel facts missing {want!r}:\n{cfacts}"
for unwanted in ("mode=compose", "Captions are drawn over", "camera locked"):
    assert unwanted not in cfacts, unwanted
ipj = still_project("job_image_shots", "panda-image",
                    slide_plan((1, "in_01", SLIDE_PHONE), n_scenes=1, aspect="1:1"),
                    [{"input_id": "in_01", "scenes": [1]}, {"input_id": "in_02", "scenes": []}])
ifacts = screens.facts(ipj)
for want in ("every placed screenshot goes on the one image", "1 → the image", "the image (s01): keep",
             "Canvas 1080x1080", "if the image is branded"):
    assert want in ifacts, f"image facts missing {want!r}:\n{ifacts}"
print("[ok] carousel / image: screenshots placed on the stills Dify shows (cached, clean originals "
      "kept for revise), flagged when the area is busy or the render fails; facts in slide / image words")

# ---------------------------------------------------------------------------
# 10) canvas parsing, the logo keep-out, wording, plan freshness, the job's language
# ---------------------------------------------------------------------------
# a caller-set size or an unlisted ratio is measured the way the job will really be produced
assert sl.canvas_for({"metadata": {"aspect_ratio": "1080x1080"}}, pipeline="panda-carousel") == (1080, 1080)
assert sl.canvas_for({"metadata": {"aspect_ratio": "1920x1080"}}, pipeline="panda-carousel") == (1920, 1080)
assert sl.canvas_for({"metadata": {"aspect_ratio": "2:3"}}, pipeline="panda-carousel") == (1080, 1620)
assert sl.canvas_for({"metadata": {"aspect_ratio": "3:2"}}, pipeline="panda-image") == (1620, 1080)
assert sl.canvas_for({"metadata": {"aspect_ratio": "junk"}}, pipeline="panda-image") == (1080, 1080)
assert sl.canvas_for({"metadata": {"aspect_ratio": "1080x1080"}}) == (1080, 1920), "video unchanged"
assert sl.canvas_for({"metadata": {"aspect_ratio": "4:3"}}) == (1440, 1080), "3:4 / 4:3 now measured"
for _r in ("1:1", "4:5", "3:4", "9:16", "16:9", "4:3", "2:3", "1080x1080", "832x1248"):
    assert sl.ratio_canvas(_r) == R._carousel_pixel_size(_r), _r   # one parser, one canvas

# the stills logo keep-out covers the FIXED-PIXEL brand stamp at every real still size
for _W, _H in ((1024, 1024), (1024, 1280), (896, 1152), (1080, 1350), (2048, 2048), (768, 1344)):
    _ko = sl.keep_out_for(_W, _H, "panda-carousel")
    assert "captions" not in _ko, "a still has no caption strip"
    assert _ko["logo"]["x"] <= 1.0 - sl.LOGO_STAMP_PX[0] / _W + 1e-9, (_W, _H, _ko)
    assert _ko["logo"]["h"] >= sl.LOGO_STAMP_PX[1] / _H - 1e-9, (_W, _H, _ko)
assert sl.keep_out_for(1080, 1920)["logo"] == {"x": 0.64, "y": 0.03, "w": 0.33, "h": 0.10}, "video unchanged"

# requests.json wording follows the pipeline; the video strings stay as they were
assert sl.validate_requests(INPUTS[:1], None) == [
    "requests.json is missing — the screenshots have not been matched to scenes yet"]
assert "matched to slides yet" in sl.validate_requests(INPUTS[:1], None, "panda-carousel")[0]
assert "matched to the image yet" in sl.validate_requests(INPUTS[:1], None, "panda-image")[0]
_bad_req = [{"input_id": "in_01", "scenes": [0]}, {"input_id": "in_02", "scenes": [1]}]
_v = " | ".join(sl.validate_requests(INPUTS[:2], _bad_req))
assert "screenshot 1: scene numbers must be 1 or higher" in _v, _v
_c = " | ".join(sl.validate_requests(INPUTS[:2], _bad_req, "panda-carousel"))
assert "screenshot 1: slide numbers must be 1 or higher" in _c, _c
_i = " | ".join(sl.validate_requests(INPUTS[:2], _bad_req, "panda-image"))
assert "screenshot 1: use 1 (the image) or leave it unplaced" in _i, _i

# a card in the logo corner, or one too long to fit on its single line, is flagged
_corner_card = dict(SLIDE_PHONE, steps=[{"kind": "card", "text": {"zh": "点这里", "en": "Tap"},
                                         "zone": {"x": 0.70, "y": 0.02, "w": 0.28, "h": 0.10}}])
_cn = car_notes(slide_plan((1, "in_01", _corner_card), (2, "in_02", SLIDE_WEB)))
assert "card sits in the corner where the Panda logo goes" in _cn, _cn
_long_card = dict(SLIDE_PHONE, steps=[
    {"kind": "card", "zone": {"x": 0.45, "y": 0.90, "w": 0.50, "h": 0.06},
     "text": {"zh": "在设置里点开移动服务然后添加 eSIM 并扫描二维码",
              "en": "Open Settings, tap Mobile Service, then Add eSIM and scan the QR code"}}])
_ln = car_notes(slide_plan((1, "in_01", _long_card), (2, "in_02", SLIDE_WEB)))
assert "text is too long" in _ln, _ln

# a stills revise that rewrites artifacts/scene_plan.json wins over the approved checkpoint copy
_fp = PROJECTS / "job_plan_freshness"
(_fp / "artifacts").mkdir(parents=True, exist_ok=True)
_old_plan = slide_plan((1, "in_01", SLIDE_PHONE), n_scenes=2)
_new_plan = slide_plan((2, "in_01", SLIDE_PHONE), n_scenes=2)
(_fp / "checkpoint_scene_plan.json").write_text(json.dumps(
    {"stage": "scene_plan", "status": "completed", "artifacts": {"scene_plan": _old_plan}}),
    encoding="utf-8")
assert sl.screenshot_items(sl.load_scene_plan(_fp))[0]["scene_number"] == 1
(_fp / "artifacts" / "scene_plan.json").write_text(json.dumps(_new_plan), encoding="utf-8")
_cp_mtime = (_fp / "checkpoint_scene_plan.json").stat().st_mtime
os.utime(_fp / "artifacts" / "scene_plan.json", (_cp_mtime + 10, _cp_mtime + 10))
assert sl.screenshot_items(sl.load_scene_plan(_fp))[0]["scene_number"] == 2, "the newer plan wins"
os.utime(_fp / "artifacts" / "scene_plan.json", (_cp_mtime - 10, _cp_mtime - 10))
assert sl.screenshot_items(sl.load_scene_plan(_fp))[0]["scene_number"] == 1, "older file ignored"

# inputs/job.json records the language the JOB runs in — the placed cards read from it
_zh_brief = "请做一个四页轮播，介绍熊猫移动 eSIM 的激活流程，包含订单页和激活页的截图说明。"
_rz = client.post("/jobs", json={"brief": _zh_brief, "pipeline": "panda-carousel",
                                 "options": {"language": "en",
                                             "media": [{"url": BASE + "/ok.png"}]}})
assert _rz.status_code == 200, _rz.text
_zjob = PROJECTS / _rz.json()["job_id"] / "inputs" / "job.json"
assert json.loads(_zjob.read_text(encoding="utf-8"))["language"] == "zh", \
    "a Mandarin brief sent with language:en is switched to zh — the cards must follow"
_ren = client.post("/jobs", json={"brief": brief, "pipeline": "panda-image",
                                  "options": {"media": [{"url": BASE + "/ok.png"}]}})
assert _ren.status_code == 200, _ren.text
_ejob = PROJECTS / _ren.json()["job_id"] / "inputs" / "job.json"
assert json.loads(_ejob.read_text(encoding="utf-8"))["language"] == "en", "no language option -> en"
assert screens._job_language(PROJECTS / "does-not-exist") == "en"
print("[ok] canvas parsing matches the real stills, the logo keep-out covers the stamp on small "
      "stills, slide / image wording, the newest scene plan wins, job.json carries the job language")

_server.shutdown()
shutil.rmtree(_TMP, ignore_errors=True)
print("\n[PASS] user screenshots: intake, assignments, layout checks, prompt facts, mirroring guard, gate hook, "
      "carousel / image stills")
