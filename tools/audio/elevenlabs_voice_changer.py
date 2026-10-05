"""ElevenLabs speech-to-speech: re-voice a native-speech clip into a cast voice.

Seedance lip-syncs reliably only to speech it generates itself (quoted dialogue with
``generate_audio: true``). This tool keeps that speech's timing — and therefore the
mouth sync — while replacing the timbre with the project's ElevenLabs cast voice.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

_API = "https://api.elevenlabs.io/v1"
_DEFAULT_MODEL = "eleven_multilingual_sts_v2"
_STT_MODEL = "scribe_v1"
_MATCH_THRESHOLD = 0.85
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def _apply_guides(text: str, guides: list[dict[str, str]] | None) -> str:
    for guide in guides or []:
        word, phonetic = guide.get("word"), guide.get("phonetic")
        if word and phonetic:
            text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", phonetic, text)
    return text


def spoken_form(text: str, pronunciation_guides: list[dict[str, str]] | None = None) -> str:
    """Line as it must appear inside the quoted dialogue of a native-speech prompt.

    Video models spell out capitalised brand tokens letter by letter ("eSIM" ->
    "E-S-I-M"); the script's ``pronunciation_guides`` respell them ("e-sim").
    """
    return _apply_guides(text, pronunciation_guides)


def normalize_words(text: str) -> list[str]:
    """Comparable tokens: Latin words, and one token per CJK character."""
    text = text.lower().replace("’", "'")
    text = re.sub(r"(?<=\w)-(?=\w)", "", text)
    return re.findall(r"[\u4e00-\u9fff]|[a-z0-9']+", text)


def _join_split_guards(tokens: list[str], guarded: set[str]) -> list[str]:
    """STT writes compound brand names as separate words ("OnePool" -> "one pool")."""
    joined: list[str] = []
    i = 0
    while i < len(tokens):
        for span in (3, 2):
            word = "".join(tokens[i:i + span])
            if i + span <= len(tokens) and word in guarded:
                joined.append(word)
                i += span
                break
        else:
            joined.append(tokens[i])
            i += 1
    return joined


def transcript_match(
    expected: str,
    heard: str,
    pronunciation_guides: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compare the script line with what the converted audio actually says."""
    guarded = {
        token
        for guide in pronunciation_guides or []
        for field in ("word", "phonetic")
        for token in normalize_words(guide.get(field, ""))
    }
    want = normalize_words(expected)
    phonetic = normalize_words(_apply_guides(expected, pronunciation_guides))
    got = _join_split_guards(normalize_words(heard), guarded)
    best = max(
        (difflib.SequenceMatcher(a=ref, b=got, autojunk=False) for ref in (want, phonetic)),
        key=lambda m: m.ratio(),
    )
    ref = best.a
    missing: list[str] = []
    extra: list[str] = []
    for tag, i1, i2, j1, j2 in best.get_opcodes():
        if tag in ("replace", "delete"):
            missing.extend(ref[i1:i2])
        if tag in ("replace", "insert"):
            extra.extend(got[j1:j2])
    guarded_missing = [token for token in missing if token in guarded]
    ratio = round(best.ratio(), 3)
    return {
        "ok": ratio >= _MATCH_THRESHOLD and not guarded_missing,
        "ratio": ratio,
        "missing": missing,
        "extra": extra,
        "guarded_missing": guarded_missing,
    }


class ElevenLabsVoiceChanger(BaseTool):
    name = "elevenlabs_voice_changer"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "voice_conversion"
    provider = "elevenlabs"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:ffmpeg"]
    install_instructions = (
        "Set the ELEVENLABS_API_KEY environment variable and install ffmpeg.\n"
        "Get a key at https://elevenlabs.io"
    )
    fallback = None
    fallback_tools = []
    agent_skills = ["elevenlabs"]

    capabilities = [
        "speech_to_speech",
        "voice_conversion",
        "timing_preserving_revoice",
        "transcript_verification",
    ]
    supports = {
        "voice_cloning": True,
        "multilingual": True,
        "offline": False,
        "video_input": True,
    }
    best_for = [
        "re-voicing Seedance native-speech clips into the cast voice without breaking lip-sync",
        "keeping one consistent character voice across generated clips",
    ]
    not_good_for = [
        "changing what is said or how long it takes (timing is preserved exactly)",
        "clips with music or several voices in the audio track",
    ]

    input_schema = {
        "type": "object",
        "required": ["source_path", "voice_id", "output_path"],
        "properties": {
            "source_path": {
                "type": "string",
                "description": "Clip (or audio file) whose speech is re-voiced",
            },
            "voice_id": {"type": "string", "description": "Target ElevenLabs cast voice ID"},
            "output_path": {"type": "string", "description": "Converted speech (.mp3)"},
            "model_id": {"type": "string", "default": _DEFAULT_MODEL},
            "mux_output_path": {
                "type": "string",
                "description": "If set, write source video + converted speech here",
            },
            "expected_text": {
                "type": "string",
                "description": "Script line; enables transcript verification and speech timing",
            },
            "pronunciation_guides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["word", "phonetic"],
                    "properties": {
                        "word": {"type": "string"},
                        "phonetic": {"type": "string"},
                    },
                },
            },
            "remove_background_noise": {"type": "boolean", "default": False},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["source_path", "voice_id", "model_id"]
    side_effects = [
        "writes audio file to output_path",
        "writes muxed video to mux_output_path when set",
        "calls ElevenLabs API",
    ]
    user_visible_verification = [
        "Listen: the line is in the cast voice and every word is intact",
        "Watch: the mouth still opens and closes with the converted speech",
    ]

    def get_status(self) -> ToolStatus:
        if os.environ.get("ELEVENLABS_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.02

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            return ToolResult(success=False, error="No ElevenLabs API key. " + self.install_instructions)
        source = Path(inputs["source_path"])
        if not source.exists():
            return ToolResult(success=False, error=f"source_path not found: {source}")

        start = time.time()
        try:
            result = self._run(inputs, source, api_key)
        except Exception as exc:
            return ToolResult(success=False, error=f"Voice conversion failed: {exc}")
        result.duration_seconds = round(time.time() - start, 2)
        result.cost_usd = self.estimate_cost(inputs)
        return result

    def _run(self, inputs: dict[str, Any], source: Path, api_key: str) -> ToolResult:
        import requests

        voice_id = inputs["voice_id"]
        model_id = inputs.get("model_id", _DEFAULT_MODEL)
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            if source.suffix.lower() in _AUDIO_SUFFIXES:
                speech = source
            else:
                speech = Path(tmp) / "speech.wav"
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
                     "-vn", "-ac", "1", "-ar", "44100", str(speech)],
                    check=True,
                )
            with speech.open("rb") as fh:
                response = requests.post(
                    f"{_API}/speech-to-speech/{voice_id}",
                    headers={"xi-api-key": api_key},
                    files={"audio": fh},
                    data={
                        "model_id": model_id,
                        "remove_background_noise": str(
                            bool(inputs.get("remove_background_noise", False))
                        ).lower(),
                    },
                    timeout=180,
                )
            response.raise_for_status()
            output.write_bytes(response.content)

        audio_seconds = _probe_duration(output)
        data: dict[str, Any] = {
            "provider": self.provider,
            "model": model_id,
            "voice_id": voice_id,
            "source": str(source),
            "output": str(output),
            "audio_duration_s": audio_seconds,
            "usage": {
                "platform": "elevenlabs",
                "unit": "audio_seconds",
                "amount": audio_seconds,
                "source": "actual",
            },
        }
        artifacts = [str(output)]

        expected = inputs.get("expected_text")
        if expected:
            transcript = self._transcribe(output, api_key)
            words = [w for w in transcript.get("words", []) if w.get("type") == "word"]
            data["transcript"] = transcript.get("text", "")
            data["words"] = [
                {"text": w["text"], "start": w["start"], "end": w["end"]} for w in words
            ]
            data["speech_start_s"] = round(words[0]["start"], 3) if words else None
            data["speech_end_s"] = round(words[-1]["end"], 3) if words else None
            data["transcript_match"] = transcript_match(
                expected, data["transcript"], inputs.get("pronunciation_guides")
            )

        mux = inputs.get("mux_output_path")
        if mux:
            mux_path = Path(mux)
            mux_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-i", str(output),
                 "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-shortest", str(mux_path)],
                check=True,
            )
            data["muxed_output"] = str(mux_path)
            artifacts.append(str(mux_path))

        return ToolResult(success=True, data=data, artifacts=artifacts, model=model_id)

    def _transcribe(self, audio: Path, api_key: str) -> dict[str, Any]:
        import requests

        with audio.open("rb") as fh:
            response = requests.post(
                f"{_API}/speech-to-text",
                headers={"xi-api-key": api_key},
                files={"file": fh},
                data={"model_id": _STT_MODEL},
                timeout=120,
            )
        response.raise_for_status()
        return response.json()


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return round(float(out), 3)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return None
