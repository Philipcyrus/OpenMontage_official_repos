"""Native-speech re-voicing: pronunciation respelling and transcript verification."""

from __future__ import annotations

from tools.audio.elevenlabs_voice_changer import (
    ElevenLabsVoiceChanger,
    normalize_words,
    spoken_form,
    transcript_match,
)

LINE = "No problem, let's get you a Panda Mobile eSIM."
GUIDES = [{"word": "eSIM", "phonetic": "e-sim"}]


def test_spoken_form_respells_brand_tokens_seedance_would_spell_out():
    assert spoken_form(LINE, GUIDES) == "No problem, let's get you a Panda Mobile e-sim."
    assert spoken_form("eSIMs and eSIM", GUIDES) == "eSIMs and e-sim"
    assert spoken_form(LINE) == LINE


def test_respelled_and_written_forms_normalize_alike():
    assert normalize_words("e-sim") == normalize_words("eSIM") == ["esim"]


def test_exact_transcript_passes():
    result = transcript_match(LINE, "No problem. Let's get you a Panda Mobile eSIM", GUIDES)
    assert result["ok"] is True
    assert result["ratio"] == 1.0


def test_misheard_brand_word_fails_even_when_the_rest_matches():
    # job_cfb6fd099504: Seedance spelled out "eSIM" and Scribe heard "ESM".
    result = transcript_match(LINE, "No problem. Let's get you a Panda Mobile ESM", GUIDES)
    assert result["ratio"] > 0.85
    assert result["guarded_missing"] == ["esim"]
    assert result["ok"] is False


def test_compound_brand_name_split_by_stt_still_matches():
    # job_af5ae2175a71: Seedance said "OnePool" correctly; Scribe wrote "one pool".
    line = "Get a Panda Mobile eSIM with OnePool, one data pool, every country."
    guides = GUIDES + [{"word": "OnePool", "phonetic": "wun-pool"}]
    heard = "Get a Panda Mobile eSIM with one pool, one data pool every country"
    result = transcript_match(line, heard, guides)
    assert result["ok"] is True
    assert result["ratio"] == 1.0
    assert transcript_match(line, "Get a Panda Mobile eSIM with one data pool every country",
                            guides)["guarded_missing"] == ["onepool"]


def test_dropped_words_fail():
    result = transcript_match(LINE, "No problem.", GUIDES)
    assert result["ok"] is False
    assert "panda" in result["missing"]


def test_translated_line_fails():
    # job_cfb6fd099504 sec-06: Seedance spoke the English line in Mandarin.
    result = transcript_match(
        "Data in over a hundred countries, set up in two minutes.",
        "数据经抄到百快搁严方式影摄像国家，两分钟设立。",
    )
    assert result["ok"] is False
    assert result["ratio"] == 0.0


def test_ad_libbed_extra_sentence_fails():
    # job_cfb6fd099504 sec-07: the line was followed by an invented second sentence.
    result = transcript_match(
        "No more roaming bills for me.",
        "No more roaming bills for me. I don't need a difference on the 2A Life",
    )
    assert result["ok"] is False
    assert result["missing"] == []
    assert result["extra"][:3] == ["i", "don't", "need"]


def test_mandarin_lines_compare_per_character():
    assert normalize_words("两分钟设立 eSIM") == ["两", "分", "钟", "设", "立", "esim"]
    assert transcript_match("两分钟设立。", "两分钟设立")["ok"] is True
    assert transcript_match("两分钟设立。", "三分钟")["ok"] is False


def test_missing_key_and_missing_source_fail_cleanly(monkeypatch, tmp_path):
    tool = ElevenLabsVoiceChanger()
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    result = tool.execute(
        {"source_path": str(tmp_path / "x.mp4"), "voice_id": "v", "output_path": str(tmp_path / "o.mp3")}
    )
    assert result.success is False and "API key" in result.error
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    result = tool.execute(
        {"source_path": str(tmp_path / "x.mp4"), "voice_id": "v", "output_path": str(tmp_path / "o.mp3")}
    )
    assert result.success is False and "not found" in result.error


def test_tool_is_registered_with_a_resolvable_skill():
    from tools.tool_registry import registry

    registry.discover()
    tool = registry.get("elevenlabs_voice_changer")
    assert tool is not None
    assert tool.capability == "voice_conversion"
    assert tool.agent_skills == ["elevenlabs"]
