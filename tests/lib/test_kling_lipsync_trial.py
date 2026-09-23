"""The real-trial harness (scripts/kling_lipsync_trial.py): its pure helpers, and a dry run that
must leave the job folder untouched and send nothing. No network, no Kling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import kling_lipsync_trial as trial


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_only_kling_keys_are_read_from_the_env_file(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KLING_API_KEY", raising=False)
    monkeypatch.delenv("KLING_API_BASE_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=secret-other\n"
                   "export KLING_API_KEY=\"abc 123\"\n"
                   "KLING_API_BASE_URL=https://api-singapore.klingai.com  # region\n")
    assert trial.read_kling_env(env) == {"KLING_API_KEY": "abc 123",
                                         "KLING_API_BASE_URL": "https://api-singapore.klingai.com"}
    monkeypatch.setenv("KLING_API_KEY", "from-env")
    assert trial.read_kling_env(env)["KLING_API_KEY"] == "from-env"      # the environment wins


def test_links_are_found_and_the_right_one_is_proved_by_bytes(tmp_path: Path) -> None:
    job = tmp_path / "job_x"
    (job / "artifacts").mkdir(parents=True)
    (job / "artifacts" / "agent_assets.log").write_text(
        'ok "https://cdn.example.net/a/scene-02.mp4" then https://cdn.example.net/b/scene-04.mp4?x=1.'
        "\nrepeat https://cdn.example.net/a/scene-02.mp4")
    (job / "clip.bin").write_bytes(b"https://cdn.example.net/not-text-file.mp4")
    urls = trial.candidate_urls(job)
    assert urls == ["https://cdn.example.net/a/scene-02.mp4",
                    "https://cdn.example.net/b/scene-04.mp4?x=1"]
    served = {urls[0]: _sha(b"other take"), urls[1]: _sha(b"this clip")}
    assert trial.match_url(urls, _sha(b"this clip"), served.get) == (urls[1], 2)
    assert trial.match_url(urls, _sha(b"missing"), served.get) == (None, 2)


def test_the_clip_comes_from_the_manifest_and_ambiguity_stops(tmp_path: Path) -> None:
    job = tmp_path / "job_x"
    (job / "assets" / "video").mkdir(parents=True)
    (job / "assets" / "video" / "scene-04.mp4").write_bytes(b"take")
    assert trial.find_clip(job, "scene-04") == (job / "assets/video/scene-04.mp4", None)
    (job / "assets" / "video" / "scene-04b.mp4").write_bytes(b"take 2")
    rows = [{"id": "c4", "type": "video", "scene_id": "scene-04", "path": "assets/video/scene-04b.mp4",
             "original_url": "https://cdn.example.net/c/scene-04b.mp4"}]
    (job / "checkpoint_assets.json").write_text(json.dumps(
        {"artifacts": {"asset_manifest": {"version": "1.0", "assets": rows}}}))
    assert trial.find_clip(job, "scene-04") == (job / "assets/video/scene-04b.mp4",
                                               "https://cdn.example.net/c/scene-04b.mp4")
    rows.append({**rows[0], "id": "c4-old", "path": "assets/video/scene-04.mp4"})
    (job / "checkpoint_assets.json").write_text(json.dumps(
        {"artifacts": {"asset_manifest": {"version": "1.0", "assets": rows}}}))
    with pytest.raises(SystemExit):
        trial.find_clip(job, "scene-04")


def test_usage_is_the_drop_in_each_resource_pack() -> None:
    before = {"resource_pack_subscribe_infos": [
        {"resource_pack_id": "p1", "resource_pack_name": "Video", "remaining_quantity": 100}]}
    after = {"resource_pack_subscribe_infos": [
        {"resource_pack_id": "p1", "resource_pack_name": "Video", "remaining_quantity": 97.5}]}
    assert trial.usage_delta(before, after) == [{"pack": "Video", "type": None,
                                                  "remaining_before": 100,
                                                  "remaining_after": 97.5, "used": 2.5}]
    assert trial.usage_delta({}, {"error": "x"}) == []


def test_a_dry_run_sends_nothing_and_never_touches_the_job(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KLING_API_KEY", raising=False)
    job = tmp_path / "projects" / "job_x"
    (job / "artifacts").mkdir(parents=True)
    (job / "checkpoint_script.json").write_text(json.dumps({"artifacts": {"script": {"sections": [
        {"id": "sec-04", "speaker": "customer", "text": "Wait", "start_seconds": 10.0,
         "end_seconds": 12.4}]}}}))
    (job / "checkpoint_scene_plan.json").write_text(json.dumps({"artifacts": {"scene_plan": {
        "scenes": [{"id": "scene-04", "start_seconds": 10.0, "end_seconds": 15.0}]}}}))
    (job / "assets" / "video").mkdir(parents=True)
    (job / "assets" / "video" / "scene-04.mp4").write_bytes(b"the clip")
    (job / "assets" / "audio").mkdir(parents=True)
    (job / "assets" / "audio" / "vo-sec-04-customer.wav").write_bytes(b"the line")
    (job / "artifacts" / "agent_assets.log").write_text("got https://cdn.example.net/z/scene-04.mp4")
    before = {p.relative_to(job).as_posix(): p.read_bytes() for p in job.rglob("*") if p.is_file()}
    monkeypatch.setattr(trial, "_fetch_sha", lambda url: _sha(b"the clip")
                        if url.endswith("/z/scene-04.mp4") else None)

    assert trial.main(["--job-dir", str(job), "--scene", "scene-04",
                       "--work", str(tmp_path / "work"), "--dry-run"]) == 0

    after = {p.relative_to(job).as_posix(): p.read_bytes() for p in job.rglob("*") if p.is_file()}
    assert after == before                                           # the job is only read
    report_path = next((tmp_path / "work").rglob("report.json"))
    report = json.loads(report_path.read_text())
    assert report["result"] == "dry run: nothing sent" and report["kling_key"] == "MISSING"
    assert report["eligible"] is True and report["clip"]["link_found_by"] == "search"
    assert report["voice_files"] == ["assets/audio/vo-sec-04-customer.wav"]
    request = json.loads((report_path.parent / "request.json").read_text())
    assert request == {"scenes": [{"scene_id": "scene-04", "clip_path": "assets/video/scene-04.mp4",
                                   "video_url": "https://cdn.example.net/z/scene-04.mp4"}]}
    assert not (report_path.parent / "job_x" / "assets" / "video" / "kling").exists()


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg is required")
def test_the_evidence_steps_work_on_real_media(tmp_path: Path) -> None:
    # These run only after the paid step on the box, so a broken ffmpeg command must show up here.
    import subprocess

    def make(path: Path, color: str) -> Path:
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=96x160:r=24:d=4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                       check=True, capture_output=True)
        return path

    original, kling = make(tmp_path / "o.mp4", "gray"), make(tmp_path / "k.mp4", "gray")
    bed = tmp_path / "bed.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-f", "lavfi",
                    "-i", "sine=frequency=400:sample_rate=16000:d=1.5", "-filter_complex",
                    "[0:a]atrim=0:0.5[s];[s][1:a]concat=n=2:v=0:a=1", str(bed)],
                   check=True, capture_output=True)
    qa = trial._qa(kling, bed, tmp_path / "qa", "scene-04")
    assert qa.get("sample_timestamps") and qa["speech_onset_seconds"] == pytest.approx(0.5, abs=0.1)
    sheet = trial._contact_sheet(original, kling, qa["sample_timestamps"], tmp_path / "sheet.png")
    assert sheet is not None and sheet.stat().st_size > 0
    video = trial._side_by_side(original, kling, bed, 4.0, tmp_path / "sbs.mp4")
    probe = trial._probe_full(video)
    widths = [s.get("width") for s in probe["streams"] if s.get("codec_type") == "video"]
    assert widths and widths[0] >= 2 * 400 and any(
        s.get("codec_type") == "audio" for s in probe["streams"])
    sim = trial._similarity(kling, original)
    assert float(sim["ssim"]) == pytest.approx(1.0, abs=0.01) and sim["psnr_db"]


def test_the_trial_never_runs_inside_the_projects_folder(tmp_path: Path) -> None:
    job = tmp_path / "projects" / "job_x"
    job.mkdir(parents=True)
    (job / "checkpoint_scene_plan.json").write_text("{}")
    with pytest.raises(SystemExit):
        trial.main(["--job-dir", str(job), "--work", str(tmp_path / "projects" / "trials"),
                    "--dry-run"])
