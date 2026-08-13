"""Per-project cost/time report (lib/cost_report.py).

Verifies native-unit aggregation across the three sources a real run leaves on disk:
Higgsfield credits (asset_manifest), ElevenLabs usage (events.jsonl), generation time
(timing.jsonl) — with actual/estimated labelling and no cross-platform USD total.
"""

import json

from lib import cost_report as cr


def _seed(proj, *, manifest=None, events=None, timing=None):
    adir = proj / "artifacts"
    adir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (adir / "asset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if events is not None:
        with open(proj / "events.jsonl", "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    if timing is not None:
        with open(adir / "timing.jsonl", "w", encoding="utf-8") as f:
            for t in timing:
                f.write(json.dumps(t) + "\n")


def test_full_report_native_units(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "PROJECTS_DIR", tmp_path)
    job = "job_test"
    _seed(
        tmp_path / job,
        manifest={"version": "1.0", "assets": [
            {"id": "img1", "type": "image", "path": "a.png", "source_tool": "higgsfield",
             "scene_id": "s1", "provider": "higgsfield", "credits": 4, "credits_source": "actual"},
            {"id": "vid1", "type": "video", "path": "a.mp4", "source_tool": "higgsfield_mcp_video",
             "scene_id": "s1", "provider": "higgsfield", "credits": 12, "credits_source": "actual"},
            {"id": "vid2", "type": "video", "path": "b.mp4", "source_tool": "higgsfield_mcp_video",
             "scene_id": "s2", "provider": "higgsfield", "credits": 6, "credits_source": "estimated"},
            {"id": "nar1", "type": "narration", "path": "n.mp3", "source_tool": "elevenlabs_tts",
             "scene_id": "s1"},  # no credits — ElevenLabs, counted from events not manifest
        ]},
        events=[
            {"event": "finish", "tool": "elevenlabs_tts",
             "usage": {"platform": "elevenlabs", "unit": "characters", "amount": 320, "source": "actual"}},
            {"event": "finish", "tool": "elevenlabs_tts",
             "usage": {"platform": "elevenlabs", "unit": "characters", "amount": 540, "source": "actual"}},
            {"event": "finish", "tool": "music_gen",
             "usage": {"platform": "elevenlabs_music", "unit": "seconds", "amount": 30, "source": "actual"}},
            {"event": "finish", "tool": "panda_render"},  # no usage -> ignored
            {"event": "start", "tool": "elevenlabs_tts"},  # not a finish -> ignored
        ],
        timing=[
            {"stage": "script", "seconds": 42.1},
            {"stage": "stills", "seconds": 300.0},
            {"stage": "assets_media", "seconds": 1200.0},
        ],
    )

    summary = cr.write_report(job)
    hf = summary["platforms"]["higgsfield"]
    assert hf["unit"] == "credits"
    assert hf["total"] == 22 and hf["actual"] == 16 and hf["estimated"] == 6
    assert hf["count"] == 3  # the narration asset has no credits and is excluded

    el = summary["platforms"]["elevenlabs"]
    assert el["unit"] == "characters" and el["total"] == 860 and el["calls"] == 2
    assert summary["platforms"]["elevenlabs_music"]["total"] == 30

    t = summary["time"]
    assert t["total_active_seconds"] == 1542.1
    assert [s["stage"] for s in t["stages"]] == ["script", "stills", "assets_media"]

    # no cross-platform USD roll-up by design
    assert "usd" not in json.dumps(summary).lower() or "no cross-platform" in summary["units"]

    # both artifacts written
    adir = tmp_path / job / "artifacts"
    assert (adir / "cost_report.json").is_file()
    md = (adir / "cost_report.md").read_text(encoding="utf-8")
    assert "Higgsfield" in md and "ElevenLabs" in md and "Generation time" in md


def test_empty_project_is_valid(tmp_path, monkeypatch):
    """A job with no generation yet still yields a valid zeroed report (never raises)."""
    monkeypatch.setattr(cr, "PROJECTS_DIR", tmp_path)
    (tmp_path / "job_empty" / "artifacts").mkdir(parents=True)
    summary = cr.write_report("job_empty")
    assert summary["platforms"]["higgsfield"]["total"] == 0
    assert summary["time"]["total_active_seconds"] == 0
    assert "elevenlabs" not in summary["platforms"]  # no usage events -> no bucket


def test_credits_default_to_estimated(tmp_path, monkeypatch):
    """An asset with credits but no credits_source counts as estimated, not actual."""
    monkeypatch.setattr(cr, "PROJECTS_DIR", tmp_path)
    _seed(tmp_path / "job_x", manifest={"version": "1.0", "assets": [
        {"id": "v", "type": "video", "path": "v.mp4", "source_tool": "higgsfield_mcp_video",
         "scene_id": "s1", "credits": 9},
    ]})
    hf = cr.write_report("job_x")["platforms"]["higgsfield"]
    assert hf["total"] == 9 and hf["estimated"] == 9 and hf["actual"] == 0


def test_superseded_revise_credits_are_counted(tmp_path, monkeypatch):
    """Live still is 2 credits; two prior revises of 2 each → report 6, not 2."""
    monkeypatch.setattr(cr, "PROJECTS_DIR", tmp_path)
    _seed(tmp_path / "job_rev", manifest={
        "version": "1.0",
        "assets": [{
            "id": "slide-1-still", "type": "image", "path": "assets/images/slide-1.png",
            "scene_id": "slide-1", "credits": 2, "credits_source": "actual",
            "model": "nano_banana_flash",
        }],
        "metadata": {
            "budget": {"spent_credits": 6},
            "revisions": [
                {"slide": "slide-1", "mode": "edit", "prior_credits": 2, "new_credits": 2},
                {"slide": "slide-1", "mode": "fresh", "prior_credits": 2, "new_credits": 2},
            ],
        },
    })
    hf = cr.write_report("job_rev")["platforms"]["higgsfield"]
    assert hf["total"] == 6 and hf["actual"] == 6
    assert hf["count"] == 3


def test_spent_credits_fills_gap_without_revisions(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "PROJECTS_DIR", tmp_path)
    _seed(tmp_path / "job_gap", manifest={
        "version": "1.0",
        "assets": [{"id": "img", "type": "image", "credits": 2, "credits_source": "actual"}],
        "metadata": {"budget": {"spent_credits": 6}},
    })
    hf = cr.write_report("job_gap")["platforms"]["higgsfield"]
    assert hf["total"] == 6 and hf["actual"] == 6


def test_checkpoint_manifest_wins_over_stale_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "PROJECTS_DIR", tmp_path)
    job = tmp_path / "job_ck"
    _seed(job, manifest={
        "version": "1.0",
        "assets": [{"id": "img", "type": "image", "credits": 2, "credits_source": "actual"}],
        "metadata": {"budget": {"spent_credits": 4}, "revisions": [
            {"slide": "slide-1", "prior_credits": 2},
        ]},
    })
    (job / "checkpoint_assets.json").write_text(json.dumps({
        "stage": "assets",
        "artifacts": {"asset_manifest": {
            "version": "1.0",
            "assets": [{"id": "img", "type": "image", "credits": 2, "credits_source": "actual"}],
            "metadata": {"budget": {"spent_credits": 6}, "revisions": [
                {"slide": "slide-1", "prior_credits": 2},
                {"slide": "slide-1", "prior_credits": 2},
            ]},
        }},
    }), encoding="utf-8")
    hf = cr.write_report("job_ck")["platforms"]["higgsfield"]
    assert hf["total"] == 6 and hf["actual"] == 6
