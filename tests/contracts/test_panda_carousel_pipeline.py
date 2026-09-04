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


_CUSTOMER_ELEMENT = "089ddcec-c375-4299-8a65-6d8b757dd81a"
_PANDA_ELEMENT = "4c01c8f9-6cfb-4d8c-9eb9-74cb61462103"


def test_carousel_asset_director_locks_2take_2d_and_elements():
    text = (ROOT / "skills/pipelines/panda-carousel/asset-director.md").read_text(encoding="utf-8")
    assert "2-TAKE" in text or "2 paid" in text
    assert "2D" in text or "2d" in text.lower()
    assert "flat" in text.lower()
    assert "CHARACTER LOCK" in text or "media" in text.lower()
    assert "i2i" in text.lower()


def test_higgsfield_bridge_locks_2take_2d_and_elements():
    text = (ROOT / "skills/meta/higgsfield-mcp-bridge.md").read_text(encoding="utf-8")
    assert "CHARACTER LOCK" in text
    assert "STILLS 2-TAKE HARD RULE" in text
    assert "2D MEDIUM LOCK" in text
    assert _CUSTOMER_ELEMENT in text
    assert _PANDA_ELEMENT in text
    assert "never invent" in text.lower() or "Never invent" in text
