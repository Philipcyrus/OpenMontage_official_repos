"""Per-speaker subshots: sequential lines, single-line lip-sync, narrator fills."""

from __future__ import annotations

import pytest

from lib.i2v_duration import (
    DurationAllocationError,
    allocate_scene_durations,
    build_scene_subshots,
    effective_audio_start,
    estimate_native_speech_seconds,
    find_voice_overlaps,
    place_scene_subshots,
)


def _sc11_sections() -> list[dict]:
    # Shaped after job_996f845ca55e scene 11: every line ran longer than its
    # script slot, and placing them at script offsets stacked the voices.
    return [
        {"section_id": "s21", "speaker": "narrator", "measured_seconds": 3.1,
         "script_start_seconds": 60.0, "script_end_seconds": 62.0,
         "path": "assets/audio/vo-s21-narrator.mp3"},
        {"section_id": "s22", "speaker": "panda", "measured_seconds": 4.2,
         "script_start_seconds": 62.0, "script_end_seconds": 65.0,
         "path": "assets/audio/vo-s22-panda.mp3"},
        {"section_id": "s23", "speaker": "customer", "measured_seconds": 3.0,
         "script_start_seconds": 65.2, "script_end_seconds": 68.0,
         "path": "assets/audio/vo-s23-customer.mp3"},
    ]


def _sc11() -> dict:
    return build_scene_subshots(
        "sc11", _sc11_sections(), scene_script_start_seconds=60.0,
        allowed_durations=[5, 6, 7, 8, 9, 10], speech_mode="audio_reference",
    )


def test_script_offsets_would_stack_the_job_voices():
    with pytest.raises(ValueError, match="overlap"):
        effective_audio_start(
            effective_scene_start_seconds=100.0,
            original_section_start_seconds=62.0,
            original_scene_start_seconds=60.0,
            previous_audio_end_seconds=3.1,
        )


def test_overrunning_lines_are_laid_end_to_end():
    plan = _sc11()
    rows = {row["section_id"]: row for row in plan["subshots"]}
    assert [row["subshot_id"] for row in plan["subshots"]] == [
        "sc11-s21", "sc11-s22", "sc11-s23",
    ]
    assert rows["s21"]["relative_start_seconds"] == 0.0
    assert rows["s22"]["relative_start_seconds"] == pytest.approx(3.35)
    assert rows["s23"]["relative_start_seconds"] == pytest.approx(7.8)
    assert rows["s22"]["script_overrun_seconds"] == pytest.approx(1.2)
    assert plan["content_duration_seconds"] == pytest.approx(11.05)
    assert plan["speaking_subshot_count"] == 2


def test_narrator_line_is_a_closed_mouth_fill_never_an_audio_reference():
    narrator = _sc11()["subshots"][0]
    assert narrator["kind"] == "fill"
    assert narrator["role"] == "narration"
    assert narrator["lipsync"] is False
    assert narrator["audio_reference_path"] is None
    assert narrator["i2v_duration"] is None
    assert narrator["fill_treatment"] == "still"
    assert narrator["vo_path"].endswith("vo-s21-narrator.mp3")


def test_speaking_subshot_references_only_its_own_line_at_minimum_length():
    rows = {row["section_id"]: row for row in _sc11()["subshots"]}
    panda = rows["s22"]
    assert panda["kind"] == "speaking"
    assert panda["audio_reference_path"] == "assets/audio/vo-s22-panda.mp3"
    assert panda["i2v_duration"] == 5
    assert panda["duration_seconds"] == pytest.approx(4.45)
    customer = rows["s23"]
    assert customer["audio_reference_path"] == "assets/audio/vo-s23-customer.mp3"
    assert customer["i2v_duration"] == 5
    assert customer["duration_seconds"] == pytest.approx(3.25)


def test_real_script_pause_and_leading_silence_are_kept():
    plan = build_scene_subshots(
        "sc2",
        [
            {"section_id": "a", "speaker": "panda", "measured_seconds": 2.0,
             "script_start_seconds": 11.0, "script_end_seconds": 13.0},
            {"section_id": "b", "speaker": "customer", "measured_seconds": 2.0,
             "script_start_seconds": 14.0, "script_end_seconds": 16.0},
        ],
        scene_script_start_seconds=10.0,
        speech_mode="audio_reference",
    )
    kinds = [(row["subshot_id"], row["kind"]) for row in plan["subshots"]]
    assert kinds == [("sc2-lead_in", "fill"), ("sc2-a", "speaking"), ("sc2-b", "speaking")]
    assert plan["subshots"][0]["duration_seconds"] == pytest.approx(1.0)
    assert plan["subshots"][1]["duration_seconds"] == pytest.approx(3.0)
    assert plan["subshots"][2]["relative_start_seconds"] == pytest.approx(4.0)


def _native_sections(**panda_extra) -> list[dict]:
    return [
        {"section_id": "s02", "speaker": "customer", "measured_seconds": 2.191,
         "script_start_seconds": 4.0, "script_end_seconds": 6.5,
         "text": "I just landed and my phone has no signal at all.",
         "path": "assets/audio/vo-s02-customer.mp3"},
        {"section_id": "s03", "speaker": "panda", "measured_seconds": 2.251,
         "script_start_seconds": 6.5, "script_end_seconds": 9.0,
         "text": "No problem, let's get you a Panda Mobile eSIM.",
         "path": "assets/audio/vo-s03-panda.mp3", **panda_extra},
    ]


def test_native_speech_is_the_default_and_sized_for_seedance_pacing():
    plan = build_scene_subshots("sc02", _native_sections(), allowed_durations=range(4, 16))
    customer, panda = plan["subshots"]
    # job_cfb6fd099504 measured 4.50s and 3.72s for these lines; ElevenLabs read them in ~2.2s.
    assert customer["vo_seconds"] == pytest.approx(4.481, abs=0.01)
    assert customer["i2v_duration"] == 5
    assert panda["vo_seconds"] == pytest.approx(3.712, abs=0.01)
    assert panda["i2v_duration"] == 4
    for row in (customer, panda):
        assert row["speech_mode"] == "native"
        assert row["native_speech_estimated"] is True
        assert row["audio_reference_path"] is None
    assert panda["dialogue_text"] == "No problem, let's get you a Panda Mobile eSIM."


def test_native_estimate_counts_mandarin_characters_not_spaces():
    assert estimate_native_speech_seconds("两分钟设立") == pytest.approx(0.25 + 5 / 3.5, abs=1e-3)
    assert estimate_native_speech_seconds("熊猫 eSIM") == pytest.approx(
        0.25 + 2 / 3.5 + 1 / 2.6, abs=1e-3
    )


def test_native_estimate_falls_back_to_tts_length_without_text():
    assert estimate_native_speech_seconds(tts_seconds=2.0) == pytest.approx(3.3)
    with pytest.raises(ValueError):
        estimate_native_speech_seconds()


def test_generated_native_clip_keeps_its_length_and_measured_speech():
    plan = build_scene_subshots(
        "sc02",
        _native_sections(
            measured_seconds=3.72,
            path="assets/audio/native/s03.panda_voice.mp3",
            generated_i2v_duration=4,
        ),
        allowed_durations=range(4, 16),
    )
    panda = plan["subshots"][1]
    assert panda["native_speech_estimated"] is False
    assert panda["i2v_duration"] == 4
    assert panda["vo_seconds"] == pytest.approx(3.72)
    assert panda["duration_seconds"] == pytest.approx(3.97)
    assert panda["vo_path"].endswith("s03.panda_voice.mp3")
    placed = place_scene_subshots(plan, effective_scene_start_seconds=0.0)
    assert find_voice_overlaps(placed["voice_tracks"]) == []


def test_generated_native_clip_shorter_than_its_speech_is_rejected():
    with pytest.raises(DurationAllocationError, match="generated clip"):
        build_scene_subshots(
            "sc02",
            _native_sections(measured_seconds=4.3, generated_i2v_duration=4),
            allowed_durations=range(4, 16),
        )


def test_unknown_speech_mode_is_rejected():
    with pytest.raises(ValueError, match="speech_mode"):
        build_scene_subshots("sc02", _native_sections(), speech_mode="dub")


def test_line_beyond_provider_max_is_a_pacing_error():
    with pytest.raises(DurationAllocationError):
        build_scene_subshots(
            "sc3",
            [{"section_id": "x", "speaker": "panda", "measured_seconds": 11.0,
              "script_start_seconds": 0.0, "script_end_seconds": 9.0}],
            allowed_durations=[5, 10],
        )


def test_placement_feeds_sequential_voice_tracks_and_a_tail_fill():
    placed = place_scene_subshots(
        _sc11(), effective_scene_start_seconds=100.0, effective_duration_seconds=12.0,
    )
    starts = [(t["section_id"], t["at_s"]) for t in placed["voice_tracks"]]
    assert starts == [("s21", 100.0), ("s22", 103.35), ("s23", 107.8)]
    assert find_voice_overlaps(placed["voice_tracks"]) == []
    tail = placed["subshots"][-1]
    assert tail["subshot_id"] == "sc11-tail"
    assert tail["kind"] == "fill"
    assert tail["duration_seconds"] == pytest.approx(0.95)
    assert placed["effective_end_seconds"] == pytest.approx(112.0)


def test_positive_offset_delays_only_that_voice_and_pushes_later_lines():
    placed = place_scene_subshots(
        _sc11(), effective_scene_start_seconds=100.0, effective_duration_seconds=12.0,
        validated_offsets={"s22": 0.3},
    )
    tracks = {t["section_id"]: t["at_s"] for t in placed["voice_tracks"]}
    assert tracks["s21"] == 100.0
    assert tracks["s22"] == pytest.approx(103.65)
    assert tracks["s23"] == pytest.approx(107.85)
    rows = {row["subshot_id"]: row for row in placed["subshots"]}
    assert rows["sc11-s22"]["lip_sync_offset_applied_seconds"] == pytest.approx(0.3)
    assert rows["sc11-s21"]["lip_sync_offset_applied_seconds"] is None
    assert rows["sc11-s23"]["lip_sync_offset_applied_seconds"] is None
    assert find_voice_overlaps(placed["voice_tracks"]) == []


def test_negative_offset_trims_that_clip_head_and_moves_no_voice():
    baseline = place_scene_subshots(_sc11(), effective_scene_start_seconds=100.0)
    placed = place_scene_subshots(
        _sc11(), effective_scene_start_seconds=100.0, validated_offsets={"s23": -0.4},
    )
    assert placed["voice_tracks"] == baseline["voice_tracks"]
    rows = {row["subshot_id"]: row for row in placed["subshots"]}
    assert rows["sc11-s23"]["source_in_seconds"] == pytest.approx(0.4)
    assert rows["sc11-s22"]["source_in_seconds"] == 0.0


def test_offset_cannot_target_a_fill_or_trim_through_the_line():
    with pytest.raises(ValueError, match="fill"):
        place_scene_subshots(
            _sc11(), effective_scene_start_seconds=0.0, validated_offsets={"s21": 0.2},
        )
    with pytest.raises(ValueError, match="lip-sync trim"):
        place_scene_subshots(
            _sc11(), effective_scene_start_seconds=0.0, validated_offsets={"s22": -0.6},
        )


def test_placement_refuses_content_longer_than_its_allocation():
    with pytest.raises(ValueError, match="allocate_scene_durations"):
        place_scene_subshots(
            _sc11(), effective_scene_start_seconds=0.0, effective_duration_seconds=10.0,
        )


def test_find_voice_overlaps_names_both_lines():
    problems = find_voice_overlaps([
        {"section_id": "s21", "at_s": 0.0, "duration_s": 3.1},
        {"section_id": "s22", "at_s": 2.0, "duration_s": 4.2},
        {"section_id": "s23", "at_s": 6.3, "duration_s": 1.0},
    ])
    assert len(problems) == 1
    assert "s21" in problems[0] and "s22" in problems[0]
    assert find_voice_overlaps([
        {"section_id": "a", "at_s": 0.0, "duration_s": 2.0},
        {"section_id": "b", "at_s": 1.98, "duration_s": 1.0},
    ]) == []


def test_allocator_gives_subshot_scenes_an_exact_length_and_null_i2v():
    content = _sc11()["content_duration_seconds"]
    result = allocate_scene_durations(
        [
            {"scene_id": "sc10", "audio_end_seconds": 4.6, "allowed_durations": [5, 10],
             "planned_duration_seconds": 6},
            {"scene_id": "sc11", "subshot_content_seconds": content,
             "planned_duration_seconds": 12},
        ],
        target_duration_seconds=17,
    )
    assert result["within_target_band"] is True
    rows = {row["scene_id"]: row for row in result["scenes"]}
    assert rows["sc10"]["i2v_duration"] in (5, 10)
    sc11 = rows["sc11"]
    assert sc11["i2v_duration"] is None
    assert sc11["subshot_content_seconds"] == pytest.approx(content)
    assert sc11["effective_duration_seconds"] >= content
    assert sc11["tail_fill_seconds"] == pytest.approx(
        sc11["effective_duration_seconds"] - content
    )
    placed = place_scene_subshots(
        _sc11(),
        effective_scene_start_seconds=sc11["effective_start_seconds"],
        effective_duration_seconds=sc11["effective_duration_seconds"],
    )
    assert placed["effective_end_seconds"] == pytest.approx(sc11["effective_end_seconds"])
