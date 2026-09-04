"""panda-image is a truncated sibling: idea → scene_plan → one still → done."""

from pathlib import Path

from lib.pipeline_loader import get_stage_order, get_stage_skill, load_pipeline

ROOT = Path(__file__).resolve().parent.parent.parent


def test_image_stage_order_is_truncated():
    manifest = load_pipeline("panda-image")
    assert get_stage_order(manifest) == ["idea", "scene_plan", "assets"]
    assert manifest["name"] == "panda-image"
    assert "script" not in get_stage_order(manifest)


def test_image_assets_are_stills_only():
    manifest = load_pipeline("panda-image")
    assets = next(s for s in manifest["stages"] if s["name"] == "assets")
    assert assets["tools_available"] == ["image_selector"]
    assert assets["human_approval_default"] is True
    assert "higgsfield_mcp_video" not in assets["tools_available"]
    assert "elevenlabs_tts" not in assets["tools_available"]


def test_image_directors_exist():
    manifest = load_pipeline("panda-image")
    for stage in ("idea", "scene_plan", "assets"):
        skill = get_stage_skill(manifest, stage)
        path = ROOT / "skills" / f"{skill}.md"
        assert path.is_file(), f"missing director {path}"


def test_image_idea_is_ungated():
    manifest = load_pipeline("panda-image")
    idea = next(s for s in manifest["stages"] if s["name"] == "idea")
    assert idea["human_approval_default"] is False


def test_image_asset_director_locks_2take_2d_and_elements():
    text = (ROOT / "skills/pipelines/panda-image/asset-director.md").read_text(encoding="utf-8")
    assert "2 paid" in text or "2-TAKE" in text
    assert "2D" in text or "2d" in text.lower()
    assert "flat" in text.lower()
    assert "i2i" in text.lower()
    assert "CHARACTER LOCK" in text or "media" in text.lower()


def test_panda_playbook_is_2d_and_valid():
    from styles.playbook_loader import load_playbook

    pb = load_playbook("panda")
    prefix = pb["asset_generation"]["image_prompt_prefix"].lower()
    neg = pb["asset_generation"]["image_negative_prompt"].lower()
    assert "2d" in prefix or "flat" in prefix
    assert "3d" in neg or "cgi" in neg or "pixar" in neg
    assert "photoreal" in neg
