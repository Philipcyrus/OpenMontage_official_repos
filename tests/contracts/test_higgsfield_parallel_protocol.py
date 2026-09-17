"""Quality-neutral Higgsfield batching contract."""

from __future__ import annotations

from pathlib import Path

from tools.video.higgsfield_mcp_video import HiggsFieldMCPVideo


ROOT = Path(__file__).resolve().parents[2]


def test_bridge_handshake_encodes_resumable_parallel_wave() -> None:
    result = HiggsFieldMCPVideo().execute(
        {
            "prompt": "on-model panda holds a static pose",
            "model": "seedance_2_0",
            "duration": 5,
            "aspect_ratio": "9:16",
        }
    )
    action = result.data["agent_action_required"]
    joined = " ".join(str(value) for value in action.values()).lower()
    assert action["max_in_flight"] == 4
    assert "preflight every pending clip" in joined
    assert "429" in joined and "max 2" in joined
    assert "scene_id -> job_id" in joined
    assert "poll the complete in-flight set" in joined
    assert "never replay successful jobs" in joined


def test_director_protocol_preserves_take_limit_and_partial_successes() -> None:
    bridge = (ROOT / "skills" / "meta" / "higgsfield-mcp-bridge.md").read_text(
        encoding="utf-8"
    )
    director = (
        ROOT / "skills" / "pipelines" / "panda-video" / "asset-director.md"
    ).read_text(encoding="utf-8")
    combined = f"{bridge}\n{director}".lower()
    assert "take 2 only for unusable" in combined
    assert "metadata.partial_progress.motion_jobs" in combined
    assert "at most **4" in combined
    assert "cap to **2" in combined or "reduce the cap to 2" in combined
    assert "retain" in combined and "partial failure" in combined
