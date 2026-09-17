"""Per-shot multi-voice casting — schema + VO premix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_schema(name: str) -> dict:
    return json.loads(
        (ROOT / "schemas" / "artifacts" / name).read_text(encoding="utf-8")
    )


def test_script_schema_accepts_speaker_and_legacy_omit():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load_schema("script.schema.json")
    section_props = schema["properties"]["sections"]["items"]["properties"]
    assert section_props["speaker"]["enum"] == ["customer", "panda", "narrator"]

    base = {
        "version": "1.0",
        "title": "cast demo",
        "total_duration_seconds": 8,
        "sections": [
            {
                "id": "s1",
                "text": "Where can I get an eSIM?",
                "start_seconds": 0,
                "end_seconds": 2,
                "speaker": "customer",
            },
            {
                "id": "s2",
                "text": "Panda Mobile!",
                "start_seconds": 2,
                "end_seconds": 4,
                "speaker": "panda",
            },
            {
                "id": "s3",
                "text": "Grab OnePool before you fly.",
                "start_seconds": 4,
                "end_seconds": 8,
                "speaker": "narrator",
            },
        ],
    }
    jsonschema.validate(instance=base, schema=schema)

    legacy = {
        "version": "1.0",
        "title": "single voice",
        "total_duration_seconds": 4,
        "sections": [
            {
                "id": "only",
                "text": "Hello",
                "start_seconds": 0,
                "end_seconds": 4,
            }
        ],
    }
    jsonschema.validate(instance=legacy, schema=schema)

    bad = {
        **base,
        "sections": [
            {
                "id": "x",
                "text": "nope",
                "start_seconds": 0,
                "end_seconds": 1,
                "speaker": "robot",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)


def test_scene_plan_required_assets_speaker_fields():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load_schema("scene_plan.schema.json")
    plan = {
        "version": "1.0",
        "scenes": [
            {
                "id": "scene-1",
                "type": "character_scene",
                "description": "Airport duo",
                "start_seconds": 0,
                "end_seconds": 4,
                "script_section_id": "s1",
                "required_assets": [
                    {
                        "type": "image",
                        "description": "still",
                        "source": "generate",
                    },
                    {
                        "type": "narration",
                        "description": "customer line",
                        "source": "generate",
                        "speaker": "customer",
                        "script_section_id": "s1",
                    },
                    {
                        "type": "narration",
                        "description": "panda line",
                        "source": "generate",
                        "speaker": "panda",
                        "script_section_id": "s2",
                    },
                    {
                        "type": "narration",
                        "description": "narrator CTA",
                        "source": "generate",
                        "speaker": "narrator",
                        "script_section_id": "s3",
                    },
                ],
            }
        ],
    }
    jsonschema.validate(instance=plan, schema=schema)


def test_edit_decisions_narration_segment_speaker():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load_schema("edit_decisions.schema.json")
    # Minimal valid shape: required top-level fields from schema
    required = schema.get("required") or []
    doc: dict = {
        "version": "1.0",
        "cuts": [],
        "audio": {
            "narration": {
                "segments": [
                    {
                        "asset_id": "vo-s1-customer",
                        "start_seconds": 0,
                        "speaker": "customer",
                    },
                    {
                        "asset_id": "vo-s2-panda",
                        "start_seconds": 2,
                        "speaker": "panda",
                    },
                ]
            }
        },
    }
    # Fill any other required keys with minimal placeholders
    props = schema.get("properties") or {}
    for key in required:
        if key in doc:
            continue
        typ = (props.get(key) or {}).get("type")
        if typ == "string":
            doc[key] = "ffmpeg" if key == "render_runtime" else "x"
        elif typ == "array":
            doc[key] = []
        elif typ == "object":
            doc[key] = {}
        elif typ == "number":
            doc[key] = 0
        elif typ == "boolean":
            doc[key] = False
        else:
            doc[key] = "x"
    jsonschema.validate(instance=doc, schema=schema)


def test_premix_voice_tracks_builds_adelay_amix(monkeypatch, tmp_path):
    from tools.video import panda_render as pr
    import subprocess as sp

    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    a.write_bytes(b"RIFF")
    b.write_bytes(b"RIFF")
    out = tmp_path / "mix.wav"

    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True):
        calls.append(list(cmd))

        class R:
            returncode = 0
            stderr = ""
            stdout = "2.0\n" if Path(cmd[0]).name == "ffprobe" else ""

        if Path(cmd[0]).name == "ffmpeg":
            out.write_bytes(b"fake-wav")
        return R()

    monkeypatch.setattr(sp, "run", fake_run)

    pr._premix_voice_tracks(
        [{"path": str(a), "at_s": 0, "speaker": "customer"},
         {"path": str(b), "at_s": 3, "speaker": "panda"}],
        out,
    )
    assert out.is_file()
    assert calls, "ffmpeg should be invoked for multi-track premix"
    ffmpeg_calls = [cmd for cmd in calls if Path(cmd[0]).name == "ffmpeg"]
    assert len(ffmpeg_calls) == 1
    joined = " ".join(ffmpeg_calls[0])
    assert "adelay=3000|3000" in joined
    assert "amix=inputs=2" in joined
    assert "atrim=0:5.000" in joined


def test_panda_render_schema_exposes_voice_tracks():
    from tools.video.panda_render import PandaRender

    audio = PandaRender.input_schema["properties"]["audio"]["properties"]
    assert "voice_tracks" in audio
    assert "path" in audio["voice_tracks"]["items"]["properties"]
    assert "at_s" in audio["voice_tracks"]["items"]["properties"]
