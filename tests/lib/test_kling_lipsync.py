"""Kling customer lip-sync pass: eligibility, exact audio, restart safety, fallbacks, budget.

No real Kling call is made: a fake client stands in for the API and a fake CDN serves each clip's
bytes. Media are tiny real files made with ffmpeg so probing, padding and the byte-level checks
are real.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import kling_lipsync as kl
from tools._kling.errors import KlingAPIError

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def _clip(path: Path, seconds: float, color: str = "black", audio_s: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=12:d={seconds}"]
    if audio_s:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=200:sample_rate=16000:d={audio_s}",
                "-c:a", "aac"]
    else:
        cmd += ["-an"]
    _run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    return path


def _tone(path: Path, seconds: float, freq: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=16000",
          "-t", str(seconds), str(path)])
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dur(path: Path) -> float:
    return kl._probe(path)["duration_s"]


SECTIONS = [
    {"id": "sec-02", "speaker": "panda", "text": "Hi", "start_seconds": 5.0, "end_seconds": 8.0},
    {"id": "sec-04", "speaker": "customer", "text": "Wait", "start_seconds": 10.0, "end_seconds": 12.4},
    {"id": "sec-07", "speaker": "customer", "text": "So", "start_seconds": 20.5, "end_seconds": 23.0},
    {"id": "sec-07b", "speaker": "narrator", "text": "Tag", "start_seconds": 23.5, "end_seconds": 24.5},
    {"id": "sec-09", "speaker": "customer", "text": "Me", "start_seconds": 30.0, "end_seconds": 31.5},
    {"id": "sec-09b", "speaker": "panda", "text": "You", "start_seconds": 32.0, "end_seconds": 33.5},
    {"id": "sec-11", "speaker": "customer", "text": "Ok", "start_seconds": 40.0, "end_seconds": 41.2},
]
SCENES = [
    {"id": "scene-02", "type": "character_scene", "description": "d", "start_seconds": 5.0, "end_seconds": 10.0},
    {"id": "scene-04", "type": "character_scene", "description": "d", "start_seconds": 10.0, "end_seconds": 15.0},
    {"id": "scene-07", "type": "character_scene", "description": "d", "start_seconds": 20.0, "end_seconds": 25.0},
    {"id": "scene-09", "type": "character_scene", "description": "d", "start_seconds": 30.0, "end_seconds": 35.0},
    {"id": "scene-11", "type": "character_scene", "description": "d", "start_seconds": 40.0, "end_seconds": 45.0},
]
SCENE_IDS = ("scene-02", "scene-04", "scene-07", "scene-09", "scene-11")


def _write_plan(proj: Path, scenes: list[dict]) -> None:
    (proj / "checkpoint_scene_plan.json").write_text(json.dumps(
        {"artifacts": {"scene_plan": {"version": "1.0", "scenes": scenes}}}))


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "job_kling"
    (proj / "artifacts").mkdir(parents=True)
    (proj / "checkpoint_script.json").write_text(json.dumps(
        {"artifacts": {"script": {"version": "1.0", "sections": SECTIONS}}}))
    _write_plan(proj, SCENES)
    for sid in SCENE_IDS:
        _clip(proj / "assets" / "video" / f"{sid}.mp4", 5)
    audio = proj / "assets" / "audio"
    _tone(audio / "vo-sec-02-panda.wav", 2.6, 300)
    _tone(audio / "vo-sec-04-customer.wav", 2.4, 400)
    _tone(audio / "vo-sec-07-customer.wav", 2.5, 500)
    _tone(audio / "vo-sec-07b-narrator.wav", 1.0, 600)
    _tone(audio / "vo-sec-09-customer.wav", 1.5, 700)
    _tone(audio / "vo-sec-09b-panda.wav", 1.5, 800)
    _tone(audio / "vo-sec-11-customer.wav", 1.2, 900)
    return proj


def _url(sid: str, take: str = "") -> str:
    return f"https://cdn.example.net/{sid}{take}.mp4"


def _request(*scene_ids: str, **extra: dict) -> dict:
    return {"scenes": [{"scene_id": s, "clip_path": f"assets/video/{s}.mp4",
                        "video_url": _url(s), **extra.get(s, {})}
                       for s in scene_ids]}


class World:
    """What the fake Kling API and the fake Higgsfield CDN do, and everything they were asked."""

    def __init__(self, project: Path, tmp: Path) -> None:
        self.project = project
        self.calls: list[tuple[str, object]] = []
        self.lock = threading.Lock()
        self.faces: dict[str, list[dict]] = {}
        self.poll_status = "succeed"
        self.crash_polls = 0
        self.submit_error: Exception | None = None
        self.submit_errors: list[Exception] = []           # raised once each, in order
        self.identify_errors: list[Exception] = []
        self.ledger_had_task_at_first_poll: dict[str, bool] = {}
        self.inflight = 0
        self.max_inflight = 0
        self.poll_delay = 0.0
        self.output = _clip(tmp / "kling_out.mp4", 5, "red")
        self.tasks: dict[str, str] = {}
        self.cdn: dict[str, bytes] = {}
        self.url_scene: dict[str, str] = {}
        for sid in SCENE_IDS:
            self.publish(sid, project / "assets" / "video" / f"{sid}.mp4")

    def publish(self, sid: str, clip: Path, take: str = "") -> str:
        """Put a take on the fake CDN (Higgsfield serves the exact bytes that were ingested)."""
        url = _url(sid, take)
        self.cdn[url] = clip.read_bytes()
        self.url_scene[url] = sid
        return url

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)


class FakeClient:
    def __init__(self, world: World) -> None:
        self.w = world

    def post(self, path: str, payload: dict) -> dict:
        assert path == "/v1/videos/identify-face"
        url = payload["video_url"]
        with self.w.lock:
            self.w.calls.append(("identify", url))
            if self.w.identify_errors:
                raise self.w.identify_errors.pop(0)
        scene = self.w.url_scene[url]
        faces = self.w.faces.get(scene, [{"face_id": "f1", "start_time": 0, "end_time": 5000}])
        return {"code": 0, "data": {"session_id": f"sess-{scene}", "face_data": faces}}

    def create_classic_task(self, path: str, payload: dict) -> str:
        assert path == kl.LIPSYNC_PATH
        with self.w.lock:
            self.w.calls.append(("submit", payload))
            if self.w.submit_errors:
                raise self.w.submit_errors.pop(0)
            if self.w.submit_error is not None:
                raise self.w.submit_error
            task = f"task-{self.w.count('submit')}"
            self.w.tasks[task] = payload["session_id"].removeprefix("sess-")
        return task

    def get(self, path: str) -> dict:
        task = path.rsplit("/", 1)[-1]
        scene = self.w.tasks[task]
        ledger = json.loads((self.w.project / kl.KLING_DIR / kl.LEDGER_FILE).read_text())
        with self.w.lock:
            self.w.calls.append(("poll", task))
            self.w.ledger_had_task_at_first_poll.setdefault(
                scene, ledger["scenes"][scene].get("task_id") == task)
            self.w.inflight += 1
            self.w.max_inflight = max(self.w.max_inflight, self.w.inflight)
            crash = self.w.crash_polls > 0
            if crash:
                self.w.crash_polls -= 1
        try:
            time.sleep(self.w.poll_delay)
            if crash:
                raise ConnectionError("process killed mid-poll")
            status = self.w.poll_status
            data = {"task_status": status}
            if status == "succeed":
                data["task_result"] = {"videos": [{"url": f"https://kling.example/{task}.mp4"}]}
            if status == "failed":
                data["task_status_msg"] = "face lost"
            return {"code": 0, "data": data}
        finally:
            with self.w.lock:
                self.w.inflight -= 1

    def download(self, url: str, output_path: Path) -> Path:
        with self.w.lock:
            self.w.calls.append(("download", url))
        if url in self.w.cdn:
            output_path.write_bytes(self.w.cdn[url])
        elif url.startswith("https://kling.example/"):
            shutil.copyfile(self.w.output, output_path)
        else:
            raise KlingAPIError("not found", http_status=404)
        return output_path


@pytest.fixture()
def world(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("KLING_API_KEY", "test-key")
    monkeypatch.setattr(kl, "BUSY_BACKOFF_S", (0.01, 0.01))
    return World(project, tmp_path)


def _go(project: Path, world: World, request: dict, **kw) -> dict:
    return kl.run(project, request=request, client_factory=lambda submit=False: FakeClient(world),
                  poll_interval=0.0, timeout_s=kw.pop("timeout_s", 5.0), **kw)


def _scene(out: dict, sid: str) -> dict:
    return out["scenes"][sid]


def _ledger(project: Path) -> dict:
    return json.loads((project / kl.KLING_DIR / kl.LEDGER_FILE).read_text())


def _submits(world: World) -> list[dict]:
    return [p for k, p in world.calls if k == "submit"]


# ---------------------------------------------------------------------------
# which scenes, which lines
# ---------------------------------------------------------------------------


def test_only_customer_only_scenes_are_candidates(project: Path) -> None:
    assert kl.customer_scenes(project) == ["scene-04", "scene-07", "scene-11"]
    script = kl._latest_artifact(project, "script")
    plan = kl._latest_artifact(project, "scene_plan")
    lines = kl.scene_lines(script, plan, "scene-07")
    assert [(ln["section_id"], ln["speaker"], ln["offset_s"]) for ln in lines] == [
        ("sec-07", "customer", 0.5), ("sec-07b", "narrator", 3.5)]
    assert kl.eligibility(kl.scene_lines(script, plan, "scene-09"))[0] is False


def test_a_line_without_speaker_uses_the_job_default_voice(project: Path) -> None:
    script = {"sections": [{"id": "x", "text": "t", "start_seconds": 10.0, "end_seconds": 12.0}]}
    plan = kl._latest_artifact(project, "scene_plan")
    assert kl.eligibility(kl.scene_lines(script, plan, "scene-04", "customer"))[0] is True
    assert kl.eligibility(kl.scene_lines(script, plan, "scene-04", "panda"))[0] is False


def test_the_scene_plan_decides_which_scene_owns_a_line(project: Path) -> None:
    # sec-04 starts at 10.0 (scene-04's window) but the plan ties it to scene-02 (a J-cut):
    # it belongs to scene-02 and is no longer in scene-04.
    scenes = json.loads(json.dumps(SCENES))
    scenes[0]["required_assets"] = [{"type": "narration", "description": "d", "source": "generate",
                                     "speaker": "customer", "script_section_id": "sec-04"}]
    _write_plan(project, scenes)
    script = kl._latest_artifact(project, "script")
    plan = kl._latest_artifact(project, "scene_plan")
    assert [ln["section_id"] for ln in kl.scene_lines(script, plan, "scene-04")] == []
    assert [(ln["section_id"], ln["offset_s"]) for ln in kl.scene_lines(script, plan, "scene-02")] \
        == [("sec-02", 0.0), ("sec-04", 5.0)]


def test_a_customer_line_that_starts_before_the_clip_is_not_sent(
        project: Path, world: World) -> None:
    # The plan ties sec-04 (moved to 9.5 s) to scene-04 (10-15 s): compose lays it 0.5 s before
    # the clip starts, which Kling cannot reproduce — refused, never clamped to 0.
    script = json.loads(json.dumps(SECTIONS))
    next(s for s in script if s["id"] == "sec-04")["start_seconds"] = 9.5
    (project / "checkpoint_script.json").write_text(json.dumps(
        {"artifacts": {"script": {"version": "1.0", "sections": script}}}))
    scenes = json.loads(json.dumps(SCENES))
    scenes[1]["required_assets"] = [{"type": "narration", "description": "d", "source": "generate",
                                     "speaker": "customer", "script_section_id": "sec-04"}]
    _write_plan(project, scenes)
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.INELIGIBLE
    assert "starts before this clip" in _scene(out, "scene-04")["reason"] and world.calls == []
    # ...and the gate still tells the human why this customer scene stayed on Seedance.
    assert any(n.startswith("scene-04: original clip kept — the customer's line starts before")
               for n in kl.gate_notes(project))


def test_a_section_the_plan_lists_on_two_scenes_counts_in_both(project: Path) -> None:
    # The panda's sec-09b is listed on scene-07 as well as scene-09: scene-07 has a panda line
    # now, so it stays on Seedance (never "first listing wins").
    scenes = json.loads(json.dumps(SCENES))
    narr = {"type": "narration", "description": "d", "source": "generate",
            "script_section_id": "sec-09b"}
    scenes[2]["required_assets"] = [narr]
    scenes[3]["required_assets"] = [narr]
    _write_plan(project, scenes)
    script = kl._latest_artifact(project, "script")
    plan = kl._latest_artifact(project, "scene_plan")
    assert kl.eligibility(kl.scene_lines(script, plan, "scene-07"))[0] is False
    assert kl.eligibility(kl.scene_lines(script, plan, "scene-09"))[0] is False
    assert "scene-07" not in kl.customer_scenes(project)


def test_panda_scenes_never_reach_kling(project: Path, world: World) -> None:
    out = _go(project, world, _request("scene-02", "scene-09"))
    assert world.calls == []
    assert _scene(out, "scene-02")["status"] == kl.INELIGIBLE
    assert "animal" in _scene(out, "scene-09")["reason"]


# ---------------------------------------------------------------------------
# what is sent, and what the clip becomes
# ---------------------------------------------------------------------------


def test_success_sends_the_exact_line_at_its_offset_and_keeps_the_original(
        project: Path, world: World) -> None:
    clip = project / "assets/video/scene-07.mp4"
    before = _sha(clip)
    out = _go(project, world, _request("scene-07"))
    rec = _scene(out, "scene-07")
    assert rec["status"] == kl.DONE and rec["selected"] == "kling"

    payload = _submits(world)[0]
    face = payload["face_choose"][0]
    assert payload["session_id"] == "sess-scene-07" and face["face_id"] == "f1"
    assert face["sound_insert_time"] == 500               # the line starts 0.5 s into the clip
    assert face["sound_start_time"] == 0
    assert face["original_audio_volume"] == 0.0
    customer = (project / "assets/audio/vo-sec-07-customer.wav").read_bytes()
    assert base64.b64decode(face["sound_file"]) == customer   # narrator excluded, file untouched
    assert face["sound_end_time"] == 2500

    backup = project / rec["original_backup"]
    assert _sha(backup) == before                          # byte-identical original kept
    assert _sha(clip) == _sha(world.output)                # the clip now holds Kling's version
    assert world.ledger_had_task_at_first_poll == {"scene-07": True}
    # QA gets the customer's line placed at its clip time, so it needs no offset (lipsync_qa
    # clamps offsets to 2 s; a clip-local bed never needs one).
    assert rec["qa_offset_s"] == 0.0 and rec["qa_audio_path"].endswith(".qa-audio.wav")
    assert _dur(project / rec["qa_audio_path"]) == pytest.approx(3.0, abs=0.05)

    assert kl.select(project, "scene-07", "original")["status"] == "ok"
    assert _sha(clip) == before
    assert kl.select(project, "scene-07", "kling")["status"] == "ok"
    assert _sha(clip) == _sha(world.output)


def test_a_short_line_is_padded_to_the_two_second_minimum(project: Path, world: World) -> None:
    out = _go(project, world, _request("scene-11"))
    assert _scene(out, "scene-11")["status"] == kl.DONE
    face = _submits(world)[0]["face_choose"][0]
    assert face["sound_end_time"] >= 2000 and face["sound_insert_time"] == 0


def test_a_short_line_near_the_end_is_padded_before_it(project: Path, world: World) -> None:
    # 1.2 s line at 3.6 s in a 5 s clip: the silence goes BEFORE the line, and the insert time
    # moves back by the same amount, so the words still land at 3.6 s.
    script = json.loads(json.dumps(SECTIONS))
    next(s for s in script if s["id"] == "sec-11")["start_seconds"] = 43.6
    (project / "checkpoint_script.json").write_text(json.dumps(
        {"artifacts": {"script": {"version": "1.0", "sections": script}}}))
    out = _go(project, world, _request("scene-11"))
    assert _scene(out, "scene-11")["status"] == kl.DONE
    face = _submits(world)[0]["face_choose"][0]
    assert face["sound_end_time"] == 2000
    # 800 ms of padding: the 200 ms left after the line, the other 600 ms before it.
    assert face["sound_insert_time"] == 3000
    assert face["sound_insert_time"] + face["sound_end_time"] <= 5000
    import io
    import wave
    with wave.open(io.BytesIO(base64.b64decode(face["sound_file"]))) as w:
        rate = w.getframerate()
        pcm = memoryview(w.readframes(w.getnframes())).cast("h")
    assert max(abs(v) for v in pcm[: int(0.58 * rate)]) < 50          # silence first...
    assert max(abs(v) for v in pcm[int(0.62 * rate): int(0.7 * rate)]) > 1000   # ...words at 600 ms


def test_the_voice_file_is_never_guessed(project: Path, world: World) -> None:
    # A second encoding of the same line: ambiguous, so nothing is sent...
    _tone(project / "assets/audio/vo-sec-04-customer.mp3", 2.0, 410)
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.INELIGIBLE
    assert "several voice files" in _scene(out, "scene-04")["reason"] and world.calls == []
    # ...until the request names the file compose uses.
    take2 = _tone(project / "assets/audio/vo-sec-04-customer.speed108.wav", 2.2, 420)
    out = _go(project, world, _request("scene-04", **{"scene-04": {
        "audio": {"sec-04": "assets/audio/vo-sec-04-customer.speed108.wav"}}}))
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert base64.b64decode(_submits(world)[0]["face_choose"][0]["sound_file"]) == take2.read_bytes()


def test_a_retake_under_a_new_name_is_never_passed_over(project: Path, world: World) -> None:
    # The faster retake compose uses sits next to the first take: two takes, so nothing is sent
    # until the request names one (the old take is never picked just because its name is plain).
    _tone(project / "assets/audio/vo-sec-04-customer.speed108.wav", 2.2, 420)
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.INELIGIBLE and world.calls == []
    assert "several voice files" in _scene(out, "scene-04")["reason"]


def test_a_done_scene_that_cannot_be_rechecked_says_so_at_the_gate(
        project: Path, world: World) -> None:
    _go(project, world, _request("scene-04"))
    _tone(project / "assets/audio/vo-sec-04-customer.mp3", 2.4, 440)   # a second take appears
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.DONE and world.count("submit") == 1
    assert "could not be confirmed" in _scene(out, "scene-04")["check_error"]
    note = next(n for n in kl.gate_notes(project) if n.startswith("scene-04"))
    assert note.startswith("scene-04: Kling's version is in use, but it could not be confirmed")
    (project / "assets/audio/vo-sec-04-customer.mp3").unlink()          # resolved again
    out = _go(project, world, _request("scene-04"))
    assert "check_error" not in _scene(out, "scene-04")
    assert any(n.startswith("scene-04: the customer's lip-sync was redone by Kling")
               for n in kl.gate_notes(project))


def test_the_asset_manifest_names_the_voice_file(project: Path, world: World) -> None:
    take2 = _tone(project / "assets/audio/final-take-customer.wav", 2.2, 430)
    (project / "checkpoint_assets.json").write_text(json.dumps({"artifacts": {"asset_manifest": {
        "version": "1.0", "assets": [
            {"id": "vo4", "type": "narration", "path": "assets/audio/final-take-customer.wav",
             "source_tool": "elevenlabs_tts", "scene_id": "scene-04",
             "voice_performance": {"source_section_id": "sec-04"}}]}}}))
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert base64.b64decode(_submits(world)[0]["face_choose"][0]["sound_file"]) == take2.read_bytes()


# ---------------------------------------------------------------------------
# restarts and retries never pay twice
# ---------------------------------------------------------------------------


def test_a_restart_polls_the_saved_task_and_never_pays_twice(project: Path, world: World) -> None:
    world.crash_polls = 1
    first = _go(project, world, _request("scene-04"))
    assert _scene(first, "scene-04")["status"] == kl.SUBMITTED
    assert first["run_again"] is True and first["pending"] == ["scene-04"]
    assert _sha(project / "assets/video/scene-04.mp4") != _sha(world.output)
    second = _go(project, world, _request("scene-04"))
    assert _scene(second, "scene-04")["status"] == kl.DONE and second["run_again"] is False
    assert world.count("submit") == 1 and world.count("identify") == 1


def test_still_processing_is_resumed_later_not_resubmitted(project: Path, world: World) -> None:
    world.poll_status = "processing"
    first = _go(project, world, _request("scene-04"), timeout_s=0.5)
    assert _scene(first, "scene-04")["status"] == kl.SUBMITTED
    assert "still processing" in _scene(first, "scene-04")["note"]
    world.poll_status = "succeed"
    assert _scene(_go(project, world, _request("scene-04")), "scene-04")["status"] == kl.DONE
    assert world.count("submit") == 1


def test_a_task_in_flight_is_collected_even_when_the_request_drops_it(
        project: Path, world: World) -> None:
    world.poll_status = "processing"
    _go(project, world, _request("scene-04"), timeout_s=0.5)
    world.poll_status = "succeed"
    out = _go(project, world, _request("scene-07"))
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert world.count("submit") == 2                       # scene-04 once, scene-07 once


def test_a_bad_request_never_reopens_a_paid_scene(project: Path, world: World) -> None:
    # In flight: a wrong clip_path still collects the paid task and never resubmits it.
    world.poll_status = "processing"
    _go(project, world, _request("scene-04"), timeout_s=0.5)
    world.poll_status = "succeed"
    bad = {"scenes": [{"scene_id": "scene-04", "clip_path": "assets/video/missing.mp4",
                       "video_url": _url("scene-04")}]}
    out = _go(project, world, bad)
    assert _scene(out, "scene-04")["status"] == kl.DONE and world.count("submit") == 1
    # Done: a wrong path, or a skip, only adds a note — the scene stays done and is never resent.
    out = _go(project, world, bad)
    assert _scene(out, "scene-04")["status"] == kl.DONE
    out = _go(project, world, _request("scene-04", **{"scene-04": {"skip": "turned away"}}))
    assert _scene(out, "scene-04")["status"] == kl.DONE
    _go(project, world, _request("scene-04"))
    assert world.count("submit") == 1 and world.count("identify") == 1


def test_a_kling_failure_keeps_the_original_and_retries_only_on_request(
        project: Path, world: World) -> None:
    clip = project / "assets/video/scene-04.mp4"
    before = _sha(clip)
    world.poll_status = "failed"
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.FAILED and _sha(clip) == before
    _go(project, world, _request("scene-04"))
    _go(project, world, _request("scene-04", **{"scene-04": {"skip": "later"}}))
    assert _scene(_go(project, world, _request("scene-04")), "scene-04")["status"] == kl.FAILED
    assert world.count("submit") == 1                      # no silent paid retry, skip or not
    world.poll_status = "succeed"
    out = _go(project, world, _request("scene-04"), retry_failed=True)
    assert _scene(out, "scene-04")["status"] == kl.DONE and world.count("submit") == 2


def test_attempts_are_capped(project: Path, world: World) -> None:
    world.poll_status = "failed"
    _go(project, world, _request("scene-04"))
    _go(project, world, _request("scene-04"), retry_failed=True)
    _go(project, world, _request("scene-04"), retry_failed=True)
    assert world.count("submit") == kl.MAX_ATTEMPTS


def test_a_lost_connection_on_submit_is_resent_only_when_asked_under_the_same_id(
        project: Path, world: World) -> None:
    world.submit_error = KlingAPIError("connection reset")
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.UNKNOWN_SUBMISSION
    world.submit_error = None
    _go(project, world, _request("scene-04"))
    _go(project, world, _request("scene-04"), retry_failed=True)       # a job-wide retry: no
    _go(project, world, _request("scene-04", **{"scene-04": {"skip": "x"}}))
    _go(project, world, _request("scene-04"))
    assert world.count("submit") == 1
    out = _go(project, world, _request("scene-04"), resend_unknown=["scene-04"])
    assert _scene(out, "scene-04")["status"] == kl.DONE
    first, resent = _submits(world)
    assert resent["external_task_id"] == first["external_task_id"]   # Kling refuses a duplicate id
    assert _ledger(project)["scenes"]["scene-04"]["attempts"] == 1


def test_a_resend_that_stops_at_the_face_check_stays_a_resend(
        project: Path, world: World) -> None:
    world.submit_error = KlingAPIError("bad gateway", http_status=502)
    _go(project, world, _request("scene-04"))
    first_id = _submits(world)[0]["external_task_id"]
    world.submit_error = None
    # Rate-limited face check during the resend: still "may have been sent", pending.
    world.identify_errors = [KlingAPIError("api request too fast", code=1302, http_status=429)]
    out = _go(project, world, _request("scene-04"), resend_unknown=["scene-04"])
    assert _scene(out, "scene-04")["status"] == kl.UNKNOWN_SUBMISSION and out["run_again"] is True
    # No face this time: still a resend, never a plain failure a job-wide retry would resend.
    world.faces["scene-04"] = []
    out = _go(project, world, _request("scene-04"), resend_unknown=["scene-04"])
    assert _scene(out, "scene-04")["status"] == kl.UNKNOWN_SUBMISSION
    _go(project, world, _request("scene-04"), retry_failed=True)
    assert world.count("submit") == 1
    del world.faces["scene-04"]
    out = _go(project, world, _request("scene-04"), resend_unknown=["scene-04"])
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert [p["external_task_id"] for p in _submits(world)] == [first_id, first_id]


def test_renaming_a_clip_never_reopens_a_paid_take(project: Path, world: World) -> None:
    world.submit_error = KlingAPIError("connection reset")
    _go(project, world, _request("scene-04"))                            # maybe sent
    world.submit_error = None
    world.poll_status = "failed"
    _go(project, world, _request("scene-07"))                            # failed
    world.poll_status = "succeed"
    for sid in ("scene-04", "scene-07"):
        src = project / f"assets/video/{sid}.mp4"
        shutil.copyfile(src, project / f"assets/video/{sid}-final.mp4")    # same bytes, new name
    req = {"scenes": [{"scene_id": sid, "clip_path": f"assets/video/{sid}-final.mp4",
                       "video_url": _url(sid)} for sid in ("scene-04", "scene-07")]}
    out = _go(project, world, req)
    assert _scene(out, "scene-04")["status"] == kl.UNKNOWN_SUBMISSION
    assert _scene(out, "scene-07")["status"] == kl.FAILED
    assert _scene(out, "scene-07")["clip_path"] == "assets/video/scene-07-final.mp4"
    assert world.count("submit") == 2                                    # nothing resent


def test_a_clip_already_with_kling_cannot_be_claimed_by_another_scene_later(
        project: Path, world: World) -> None:
    world.poll_status = "processing"
    _go(project, world, _request("scene-04"), timeout_s=0.5)             # scene-04 in flight
    world.poll_status = "succeed"
    req = {"scenes": [{"scene_id": "scene-07", "clip_path": "assets/video/scene-04.mp4",
                       "video_url": _url("scene-04")}]}
    out = _go(project, world, req)
    assert _scene(out, "scene-07")["status"] == kl.INELIGIBLE
    assert "already requested for scene-04" in _scene(out, "scene-07")["reason"]
    assert _scene(out, "scene-04")["status"] == kl.DONE and world.count("submit") == 1


def test_the_same_line_under_another_file_name_is_not_paid_again(
        project: Path, world: World) -> None:
    _go(project, world, _request("scene-04"))
    shutil.copyfile(project / "assets/audio/vo-sec-04-customer.wav",
                    project / "assets/audio/final-sec-04.wav")
    out = _go(project, world, _request("scene-04", **{"scene-04": {
        "audio": {"sec-04": "assets/audio/final-sec-04.wav"}}}))
    assert _scene(out, "scene-04")["status"] == kl.DONE and world.count("submit") == 1


def test_a_stop_during_the_link_check_pays_nothing(project: Path, world: World,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    real = FakeClient.download

    def stop_while_checking(self: FakeClient, url: str, output_path: Path) -> Path:
        if url in self.w.cdn:
            kl._STOP.set()                      # SIGTERM lands during the free url check
        return real(self, url, output_path)

    monkeypatch.setattr(FakeClient, "download", stop_while_checking)
    try:
        out = _go(project, world, _request("scene-04"))
    finally:
        kl._STOP.clear()
    assert world.count("identify") == 0 and world.count("submit") == 0
    assert out["run_again"] is True and _scene(out, "scene-04")["status"] == kl.QUEUED


def test_a_clip_renamed_while_kling_works_still_gets_its_result(
        project: Path, world: World) -> None:
    world.poll_status = "processing"
    _go(project, world, _request("scene-04"), timeout_s=0.5)
    world.poll_status = "succeed"
    os.replace(project / "assets/video/scene-04.mp4", project / "assets/video/scene-04-final.mp4")
    req = {"scenes": [{"scene_id": "scene-04", "clip_path": "assets/video/scene-04-final.mp4",
                       "video_url": _url("scene-04")}]}
    out = _go(project, world, req)
    rec = _scene(out, "scene-04")
    assert rec["status"] == kl.DONE and rec["clip_path"] == "assets/video/scene-04-final.mp4"
    assert _sha(project / "assets/video/scene-04-final.mp4") == _sha(world.output)
    assert world.count("submit") == 1


def test_kling_busy_during_a_retry_never_turns_into_an_unrequested_retry(
        project: Path, world: World) -> None:
    busy = KlingAPIError("busy", code=1303, http_status=429)
    world.poll_status = "failed"
    _go(project, world, _request("scene-04", "scene-07"))                # both FAILED
    world.poll_status = "succeed"
    world.identify_errors = [busy]                                       # face check busy
    world.submit_errors = [busy] * (len(kl.BUSY_BACKOFF_S) + 1)          # submit busy throughout
    out = _go(project, world, _request("scene-04", "scene-07"), retry_failed=True, concurrency=1)
    for sid in ("scene-04", "scene-07"):
        assert _scene(out, sid)["status"] == kl.FAILED and _scene(out, sid)["waiting"]
    assert out["run_again"] is True
    submits = world.count("submit")
    _go(project, world, _request("scene-04", "scene-07"))                # a plain run: no retry
    assert world.count("submit") == submits
    out = _go(project, world, _request("scene-04", "scene-07"), retry_failed=True)
    assert all(_scene(out, s)["status"] == kl.DONE for s in ("scene-04", "scene-07"))


def test_the_gate_never_says_nothing_was_sent_for_a_maybe_billed_scene(
        project: Path, world: World) -> None:
    world.submit_error = KlingAPIError("bad gateway", http_status=502)
    _go(project, world, _request("scene-04"))
    world.submit_error = None
    world.identify_errors = [KlingAPIError("too fast", code=1302, http_status=429)]
    _go(project, world, _request("scene-04"), resend_unknown=["scene-04"])
    note = next(n for n in kl.gate_notes(project) if n.startswith("scene-04"))
    assert "may have reached Kling" in note and "requested resend was not sent yet" in note
    assert "not sent to Kling yet" not in note


def test_a_refused_resend_stays_unknown(project: Path, world: World) -> None:
    world.submit_error = KlingAPIError("connection reset")
    _go(project, world, _request("scene-04"))
    world.submit_error = KlingAPIError("duplicate external_task_id", code=1201, http_status=400)
    out = _go(project, world, _request("scene-04"), resend_unknown=["scene-04"])
    assert _scene(out, "scene-04")["status"] == kl.UNKNOWN_SUBMISSION
    _go(project, world, _request("scene-04"), retry_failed=True)
    assert world.count("submit") == 2


def test_a_refused_request_is_a_plain_failure_and_is_not_billed(
        project: Path, world: World) -> None:
    world.submit_error = KlingAPIError("bad audio", code=1201, http_status=400)
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.FAILED
    assert out["estimated_usd_spent"] == pytest.approx(kl.ESTIMATE_IDENTIFY_USD)
    assert kl._definitely_not_created(KlingAPIError("x", http_status=502)) is False
    assert kl._definitely_not_created(KlingAPIError("x", code=5000)) is False
    assert kl._definitely_not_created(KlingAPIError("x", code=1201)) is True


def test_kling_busy_is_retried_with_the_same_request_and_never_counted(
        project: Path, world: World) -> None:
    busy = KlingAPIError("parallel task over resource pack limit", code=1303, http_status=429)
    world.submit_errors = [busy]
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.DONE
    first, second = _submits(world)
    assert first["external_task_id"] == second["external_task_id"]
    assert _ledger(project)["scenes"]["scene-04"]["attempts"] == 1
    # Still busy after every backoff: queued for the next run, nothing counted or billed.
    world.submit_errors = [busy] * (len(kl.BUSY_BACKOFF_S) + 1)
    out = _go(project, world, _request("scene-07"))
    assert _scene(out, "scene-07")["status"] == kl.QUEUED and out["run_again"] is True
    assert _ledger(project)["scenes"]["scene-07"].get("attempts") == 0
    assert _ledger(project)["scenes"]["scene-07"]["estimated_usd"] == pytest.approx(
        kl.ESTIMATE_IDENTIFY_USD)
    assert _scene(_go(project, world, _request("scene-07")), "scene-07")["status"] == kl.DONE


def test_a_busy_face_check_is_queued_not_failed(project: Path, world: World) -> None:
    world.identify_errors = [KlingAPIError("api request too fast", code=1302, http_status=429)]
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.QUEUED and out["run_again"] is True
    assert out["estimated_usd_spent"] == 0.0 and world.count("submit") == 0
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.DONE


def test_a_busy_backoff_never_shows_as_submitting(
        project: Path, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    ledger = project / kl.KLING_DIR / kl.LEDGER_FILE

    class Spy(threading.Event):
        def wait(self, timeout=None):
            if timeout:
                seen.append(json.loads(ledger.read_text())["scenes"]["scene-04"]["status"])
            return super().wait(0)

    monkeypatch.setattr(kl, "_STOP", Spy())
    world.submit_errors = [KlingAPIError("busy", code=1303, http_status=429)]
    out = _go(project, world, _request("scene-04"), timeout_s=30.0)
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert seen and kl.SUBMITTING not in seen and kl.QUEUED in seen


def test_a_retry_that_runs_out_of_time_is_still_pending(project: Path, world: World) -> None:
    world.poll_status = "failed"
    _go(project, world, _request("scene-04"))
    world.poll_status = "succeed"
    world.poll_delay = 1.5                      # scene-07 uses up the whole pass
    out = _go(project, world, _request("scene-07", "scene-04"), retry_failed=True,
              concurrency=1, timeout_s=1.2)
    rec = _scene(out, "scene-04")
    assert rec["status"] == kl.FAILED and "ran out of time" in rec["waiting"]
    assert out["run_again"] is True and "scene-04" in out["pending"]
    note = next(n for n in kl.gate_notes(project) if n.startswith("scene-04"))
    assert note.startswith("scene-04: original clip kept — Kling: face lost.")
    assert "The requested retry was not sent yet (the pass ran out of time" in note
    world.poll_delay = 0.0
    out = _go(project, world, _request("scene-07", "scene-04"), retry_failed=True)
    assert _scene(out, "scene-04")["status"] == kl.DONE and out["run_again"] is False
    assert "waiting" not in _scene(out, "scene-04")


def test_a_repeated_stop_signal_does_not_cut_the_shutdown_short() -> None:
    import signal
    before = signal.getsignal(signal.SIGTERM)
    try:
        kl._STOP.clear()
        kl._stop_on_signal()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
        assert kl._STOP.is_set()
        assert handler(signal.SIGTERM, None) is None      # the second one is ignored
    finally:
        signal.signal(signal.SIGTERM, before)
        kl._STOP.clear()


def test_a_scene_listed_twice_is_sent_once(project: Path, world: World) -> None:
    req = _request("scene-04", "scene-04")
    out = _go(project, world, req)
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert world.count("identify") == 1 and world.count("submit") == 1
    assert out["estimated_usd_spent"] == pytest.approx(kl.ESTIMATE_IDENTIFY_USD
                                                       + kl.ESTIMATE_LIPSYNC_USD)


def test_two_scenes_cannot_claim_one_clip(project: Path, world: World) -> None:
    req = _request("scene-04", "scene-07")
    req["scenes"][1]["clip_path"] = "assets/video/scene-04.mp4"
    out = _go(project, world, req)
    assert _scene(out, "scene-07")["status"] == kl.INELIGIBLE
    assert "already requested for scene-04" in _scene(out, "scene-07")["reason"]
    assert world.count("submit") == 1


def test_several_faces_need_an_explicit_choice(project: Path, world: World) -> None:
    world.faces["scene-04"] = [{"face_id": "a"}, {"face_id": "b"}]
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.NEEDS_FACE_CHOICE
    assert world.count("submit") == 0
    _go(project, world, _request("scene-04"))
    assert world.count("identify") == 1                    # not re-identified without a choice
    out = _go(project, world, _request("scene-04", **{"scene-04": {"face_id": "b"}}))
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert _submits(world)[0]["face_choose"][0]["face_id"] == "b"


def test_no_face_found_keeps_the_original(project: Path, world: World) -> None:
    world.faces["scene-04"] = []
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.NO_FACE and world.count("submit") == 0


def test_without_a_key_nothing_is_sent(project: Path, world: World,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KLING_API_KEY")
    out = _go(project, world, _request("scene-04", "scene-07"))
    assert world.calls == []
    assert "KLING_API_KEY" in _scene(out, "scene-04")["reason"]


def test_the_budget_blocks_the_wave_before_any_spend(project: Path, world: World) -> None:
    out = _go(project, world, _request("scene-04", "scene-07"), max_usd=0.5)
    assert world.calls == []
    assert _scene(out, "scene-07")["status"] == kl.BLOCKED_BUDGET


def test_at_most_two_clips_are_in_flight(project: Path, world: World) -> None:
    world.poll_delay = 0.3
    out = _go(project, world, _request("scene-04", "scene-07", "scene-11"), concurrency=2)
    assert all(_scene(out, s)["status"] == kl.DONE for s in ("scene-04", "scene-07", "scene-11"))
    assert world.max_inflight == 2


def test_an_unreadable_ledger_sends_nothing(project: Path, world: World) -> None:
    _go(project, world, _request("scene-04"))
    path = project / kl.KLING_DIR / kl.LEDGER_FILE
    path.write_text(path.read_text()[:50])
    out = _go(project, world, _request("scene-04", "scene-07"))
    assert out["status"] == "error" and "cannot be read" in out["error"]
    assert world.count("submit") == 1
    assert "could not be read" in kl.gate_notes(project)[0]


# ---------------------------------------------------------------------------
# the result is checked; takes never overwrite each other
# ---------------------------------------------------------------------------


def test_a_wrong_length_result_is_rejected(project: Path, world: World, tmp_path: Path) -> None:
    world.output = _clip(tmp_path / "short.mp4", 3, "red")
    clip = project / "assets/video/scene-04.mp4"
    before = _sha(clip)
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.REJECTED_OUTPUT and _sha(clip) == before


def test_a_result_whose_picture_stops_before_the_line_ends_is_rejected(
        project: Path, world: World, tmp_path: Path) -> None:
    # The line runs to 4.95 s. Kling's file is 5 s long only because of its audio track: the
    # picture stops at 4.8 s, so the last words would play over a frozen mouth.
    _tone(project / "assets/audio/vo-sec-04-customer.wav", 4.95, 400)
    world.output = _clip(tmp_path / "short_picture.mp4", 4.8, "red", audio_s=5.0)
    assert _dur(world.output) >= 4.95
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.REJECTED_OUTPUT
    assert "before the customer finishes" in _scene(out, "scene-04")["reason"]


def test_a_regenerated_clip_starts_a_fresh_record_under_a_new_id(
        project: Path, world: World) -> None:
    _go(project, world, _request("scene-04"))
    clip = _clip(project / "assets/video/scene-04.mp4", 5, "blue")      # revise made a new take
    url2 = world.publish("scene-04", clip, "-take2")
    out = _go(project, world, {"scenes": [{"scene_id": "scene-04",
                                           "clip_path": "assets/video/scene-04.mp4",
                                           "video_url": url2}]})
    assert _scene(out, "scene-04")["status"] == kl.DONE and world.count("submit") == 2
    ids = [p["external_task_id"] for p in _submits(world)]
    assert len(set(ids)) == 2                                # ids are unique per take
    ledger = _ledger(project)
    assert len(ledger["scenes"]["scene-04"]["history"]) == 1
    assert out["estimated_usd_spent"] == pytest.approx(2 * (kl.ESTIMATE_IDENTIFY_USD
                                                            + kl.ESTIMATE_LIPSYNC_USD))


def test_a_stale_link_to_the_previous_take_is_refused_before_paying(
        project: Path, world: World) -> None:
    _go(project, world, _request("scene-04"))
    _clip(project / "assets/video/scene-04.mp4", 5, "blue")             # new take, old link
    new_take = _sha(project / "assets/video/scene-04.mp4")
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.INELIGIBLE
    assert "not this clip" in _scene(out, "scene-04")["reason"]
    assert world.count("identify") == 1 and world.count("submit") == 1
    assert _sha(project / "assets/video/scene-04.mp4") == new_take       # the new take is untouched


def test_an_interrupted_apply_never_overwrites_a_new_take(
        project: Path, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = project / "assets/video/scene-04.mp4"
    real_replace = kl._replace
    calls = {"n": 0}

    def disk_full_once(src: Path, dest: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        real_replace(src, dest)

    monkeypatch.setattr(kl, "_replace", disk_full_once)
    out = _go(project, world, _request("scene-04"))              # backup made, apply interrupted
    assert _scene(out, "scene-04")["status"] == kl.SUBMITTED
    assert (project / kl.KLING_DIR / "scene-04.original.mp4").exists()
    _clip(clip, 5, "blue")                                      # the revise made a new take
    new_take = _sha(clip)
    url2 = world.publish("scene-04", clip, "-take2")
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.REJECTED_OUTPUT
    assert _sha(clip) == new_take                               # Kling's old take never lands on it
    out = _go(project, world, {"scenes": [{"scene_id": "scene-04",
                                           "clip_path": "assets/video/scene-04.mp4",
                                           "video_url": url2}]})
    assert _scene(out, "scene-04")["status"] == kl.DONE          # the new take gets its own pass
    assert _sha(project / _scene(out, "scene-04")["original_backup"]) == new_take


def test_a_clip_removed_while_kling_works_does_not_stay_pending(
        project: Path, world: World) -> None:
    world.poll_status = "processing"
    _go(project, world, _request("scene-04"), timeout_s=0.5)
    (project / "assets/video/scene-04.mp4").unlink()
    world.poll_status = "succeed"
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.REJECTED_OUTPUT and out["run_again"] is False
    assert world.count("submit") == 1


def test_the_gate_reports_what_the_clip_holds_now(project: Path, world: World) -> None:
    clip = project / "assets/video/scene-04.mp4"
    original = clip.read_bytes()
    _go(project, world, _request("scene-04"))
    clip.write_bytes(original)                                  # re-ingested: Seedance bytes again
    notes = kl.gate_notes(project)
    assert "scene-04: the original clip is used (Kling's version is kept but not selected)." in notes
    assert kl.summary(project)["scenes"]["scene-04"]["selected"] == "original"
    _clip(clip, 5, "blue")                                      # a new take, pass not re-run
    notes = kl.gate_notes(project)
    assert ("scene-04: the clip was replaced after Kling ran, so Kling is not applied to this "
            "take.") in notes
    assert not any("redone by Kling" in n for n in notes)


def test_select_never_overwrites_a_new_take(project: Path, world: World) -> None:
    _go(project, world, _request("scene-04"))
    _clip(project / "assets/video/scene-04.mp4", 5, "blue")
    new_take = _sha(project / "assets/video/scene-04.mp4")
    res = kl.select(project, "scene-04", "original")
    assert res["status"] == "error" and "new take" in res["error"]
    assert _sha(project / "assets/video/scene-04.mp4") == new_take


def test_a_changed_line_puts_the_original_back_and_syncs_the_new_words(
        project: Path, world: World) -> None:
    clip = project / "assets/video/scene-04.mp4"
    original = _sha(clip)
    _go(project, world, _request("scene-04"))
    assert _sha(clip) == _sha(world.output)
    new_line = _tone(project / "assets/audio/vo-sec-04-customer.wav", 2.6, 450)   # revised words
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.DONE and world.count("submit") == 2
    assert base64.b64decode(_submits(world)[1]["face_choose"][0]["sound_file"]) == \
        new_line.read_bytes()
    rec = _ledger(project)["scenes"]["scene-04"]
    assert rec["original_sha256"] == original              # synced from the untouched original
    assert rec["history"][0]["superseded_because"] == "the customer's line changed"
    assert _sha(project / rec["original_backup"]) == original
    _go(project, world, _request("scene-04"))
    assert world.count("submit") == 2                      # unchanged line: nothing more


# ---------------------------------------------------------------------------
# one pass at a time
# ---------------------------------------------------------------------------


def test_a_second_pass_for_the_same_job_sends_nothing(project: Path, world: World) -> None:
    (project / kl.KLING_DIR).mkdir(parents=True, exist_ok=True)
    lock = project / kl.KLING_DIR / kl.LOCK_FILE
    lock.write_text("other")
    assert _go(project, world, _request("scene-04"))["status"] == "busy"
    lock.write_text(f"{os.getpid()} {socket.gethostname()} now")      # a live pass
    assert _go(project, world, _request("scene-04"))["status"] == "busy"
    assert world.calls == []


def test_the_lock_of_a_killed_pass_is_taken_over(project: Path, world: World) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (project / kl.KLING_DIR).mkdir(parents=True, exist_ok=True)
    (project / kl.KLING_DIR / kl.LOCK_FILE).write_text(f"{dead.pid} {socket.gethostname()} then")
    out = _go(project, world, _request("scene-04"))
    assert _scene(out, "scene-04")["status"] == kl.DONE
    assert not (project / kl.KLING_DIR / kl.LOCK_FILE).exists()


def test_a_lock_older_than_any_pass_is_taken_over_even_if_its_pid_is_reused(
        project: Path, world: World) -> None:
    lock = project / kl.KLING_DIR / kl.LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()} {socket.gethostname()} long-ago")      # a live, unrelated pid
    old = time.time() - kl.STALE_LOCK_S - 60
    os.utime(lock, (old, old))
    assert _scene(_go(project, world, _request("scene-04")), "scene-04")["status"] == kl.DONE


def test_a_skip_is_recorded_and_costs_nothing(project: Path, world: World) -> None:
    out = _go(project, world, _request("scene-04", **{"scene-04": {"skip": "profile view"}}))
    assert _scene(out, "scene-04")["status"] == kl.SKIPPED and world.calls == []


def test_gate_notes_say_what_happened(project: Path, world: World) -> None:
    assert kl.gate_notes(project) == [
        "Kling lip-sync was switched on but did not run, so every clip keeps its Seedance lip-sync."]
    world.poll_status = "failed"
    _go(project, world, _request("scene-04"))
    world.poll_status = "succeed"
    _go(project, world, _request("scene-07"))
    notes = kl.gate_notes(project)
    assert any(n.startswith("scene-04: original clip kept") for n in notes)
    assert any(n.startswith("scene-07: the customer's lip-sync was redone by Kling") for n in notes)
    assert "scene-11: the customer speaks on screen but the clip was not sent to Kling." in notes


def test_the_command_line_the_agent_runs(project: Path) -> None:
    root = Path(kl.__file__).resolve().parents[1]
    req = project / "req.json"
    req.write_text(json.dumps(_request("scene-04", "scene-02")))
    env = {k: v for k, v in os.environ.items() if k != "KLING_API_KEY"}
    done = subprocess.run([sys.executable, "-m", "lib.kling_lipsync", "run", str(project),
                           "--request", str(req), "--default-speaker", "panda", "--max-usd", "5",
                           "--resend-unknown", "scene-04"],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert "KLING_API_KEY" in out["scenes"]["scene-04"]["reason"]
    assert out["scenes"]["scene-02"]["status"] == kl.INELIGIBLE
    assert out["customer_scenes_not_requested"] == ["scene-07", "scene-11"]
    # A Kling reason in Chinese (its 1303 note) must not break a non-UTF-8 stdout.
    path = project / kl.KLING_DIR / kl.LEDGER_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scenes"]["scene-04"]["reason"] = "并发/资源包限制"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    status = subprocess.run([sys.executable, "-m", "lib.kling_lipsync", "status", str(project)],
                            cwd=root, env={**env, "PYTHONIOENCODING": "ascii"},
                            capture_output=True, text=True, timeout=60)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["scenes"]["scene-04"]["reason"] == "并发/资源包限制"
