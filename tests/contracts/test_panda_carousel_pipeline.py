"""panda-carousel is a truncated sibling of panda-video: stills-only, no compose."""

from pathlib import Path

from lib.pipeline_loader import get_stage_order, get_stage_skill, load_pipeline

ROOT = Path(__file__).resolve().parent.parent.parent


def test_carousel_stage_order_is_truncated():
    manifest = load_pipeline("panda-carousel")
    assert get_stage_order(manifest) == ["idea", "script", "scene_plan", "assets"]
    assert manifest["name"] == "panda-carousel"


def test_carousel_assets_are_stills_only():
    manifest = load_pipeline("panda-carousel")
    assets = next(s for s in manifest["stages"] if s["name"] == "assets")
    assert assets["tools_available"] == ["image_selector"]
    assert assets["human_approval_default"] is True
    assert "higgsfield_mcp_video" not in assets["tools_available"]
    assert "elevenlabs_tts" not in assets["tools_available"]


def test_carousel_directors_exist():
    manifest = load_pipeline("panda-carousel")
    for stage in ("idea", "script", "scene_plan", "assets"):
        skill = get_stage_skill(manifest, stage)
        path = ROOT / "skills" / f"{skill}.md"
        assert path.is_file(), f"missing director {path}"


def test_carousel_idea_is_ungated():
    manifest = load_pipeline("panda-carousel")
    idea = next(s for s in manifest["stages"] if s["name"] == "idea")
    assert idea["human_approval_default"] is False
