"""Decision-log categories used by Panda pipeline instructions must validate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _decision(category: str) -> dict:
    return {
        "decision_id": f"d-{category}",
        "stage": "edit",
        "category": category,
        "subject": "Scene timing",
        "options_considered": [
            {
                "option_id": "extend",
                "label": "Extend the visual hold",
                "score": 1.0,
                "reason": "Preserves locked narration",
            }
        ],
        "selected": "extend",
        "reason": "The narration must finish before the scene ends",
    }


@pytest.mark.parametrize("category", ["pacing", "approval_policy", "character_lock"])
def test_pipeline_instruction_categories_validate(category: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "decision_log.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(
        instance={
            "version": "1.0",
            "project_id": "job-category-contract",
            "decisions": [_decision(category)],
        },
        schema=schema,
    )


def test_panda_edit_director_uses_typed_timeline_pacing_policy() -> None:
    text = (
        ROOT / "skills" / "pipelines" / "panda-video" / "edit-director.md"
    ).read_text(encoding="utf-8")
    assert "timeline_contract" in text
    assert "pacing_revision_required" in text
