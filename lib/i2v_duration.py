"""Snap measured VO length to a Higgsfield i2v duration + optional hold extend.

Used by panda-video TTS-first assets: generate ElevenLabs first, probe duration,
then pick an integer clip length the model allows. HOLD mouths stay; this only
aligns pacing so the visual slot can carry the VO.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Sequence


class DurationAllocationError(ValueError):
    """Raised when measured audio cannot fit a provider-supported scene."""


LIPSYNC_SPEAKERS = frozenset({"customer", "panda"})
DEFAULT_BREATH_SECONDS = 0.25
DEFAULT_MIN_GAP_SECONDS = 0.4
MAX_LIPSYNC_DELTA_SECONDS = 2.0

SPEECH_MODES = ("native", "audio_reference")
# Seedance native speech (quoted dialogue, generate_audio:true), measured on
# job_cfb6fd099504: ~0.25s before the first word, then ~2.6 words per second —
# markedly slower than the ElevenLabs reading of the same line.
NATIVE_SPEECH_LEAD_SECONDS = 0.25
NATIVE_SPEECH_WORDS_PER_SECOND = 2.6
# Not yet measured on a Mandarin job; conservative so the clip covers the line.
NATIVE_SPEECH_CJK_CHARS_PER_SECOND = 3.5
NATIVE_TO_TTS_RATIO = 1.65
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def estimate_native_speech_seconds(
    text: str | None = None, *, tts_seconds: float | None = None
) -> float:
    """Clip-relative end of the last word when Seedance speaks ``text`` itself."""
    text = text or ""
    cjk = len(_CJK_CHAR.findall(text))
    words = len(_CJK_CHAR.sub(" ", text).split())
    if cjk or words:
        spoken = cjk / NATIVE_SPEECH_CJK_CHARS_PER_SECOND + words / NATIVE_SPEECH_WORDS_PER_SECOND
        return round(NATIVE_SPEECH_LEAD_SECONDS + spoken, 3)
    if tts_seconds and tts_seconds > 0:
        return round(float(tts_seconds) * NATIVE_TO_TTS_RATIO, 3)
    raise ValueError("estimate_native_speech_seconds needs text or tts_seconds")


_ROLE_SLACK_WEIGHTS = {
    "establish_context": 1.15,
    "call_to_action": 1.15,
    "resolution": 1.1,
    "introduce_subject": 1.0,
    "deliver_payload": 0.9,
}


def effective_audio_start(
    *,
    effective_scene_start_seconds: float,
    original_section_start_seconds: float,
    original_scene_start_seconds: float,
    validated_lip_sync_delta_seconds: float = 0.0,
    previous_audio_end_seconds: float | None = None,
) -> float:
    """Place VO on an allocated timeline without accumulating resume drift.

    Only valid for a scene with one VO file, or one whose lines still fit their
    script slots. ``previous_audio_end_seconds`` is the scene-local *measured*
    end of the line before this one; when the script offset lands before it,
    the two voices would play on top of each other, so this raises and the
    caller must use :func:`build_scene_subshots` instead.
    """
    relative = float(original_section_start_seconds) - float(
        original_scene_start_seconds
    )
    if (
        previous_audio_end_seconds is not None
        and relative + float(validated_lip_sync_delta_seconds)
        < float(previous_audio_end_seconds) - 1e-6
    ):
        raise ValueError(
            f"section starts at scene-local {relative:.3f}s but the previous line "
            f"runs until {float(previous_audio_end_seconds):.3f}s; voices would "
            "overlap — place this scene with build_scene_subshots"
        )
    result = (
        float(effective_scene_start_seconds)
        + relative
        + float(validated_lip_sync_delta_seconds)
    )
    if result < 0:
        raise ValueError("effective audio start cannot be negative")
    return round(result, 6)


def _allowed_options(allowed: Sequence[int] | None) -> list[int]:
    if allowed:
        opts = sorted({int(value) for value in allowed if int(value) > 0})
        if opts:
            return opts
    return list(range(5, 11))


def build_scene_subshots(
    scene_id: str,
    sections: Sequence[dict[str, Any]],
    *,
    scene_script_start_seconds: float | None = None,
    on_screen_speakers: Iterable[str] = LIPSYNC_SPEAKERS,
    allowed_durations: Sequence[int] | None = None,
    breath_seconds: float = DEFAULT_BREATH_SECONDS,
    min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS,
    speech_mode: str = "native",
) -> dict[str, Any]:
    """Split one scene's dialogue into ordered, non-overlapping speaker subshots.

    Every section must provide ``section_id``, ``speaker``, ``measured_seconds``
    (probed ElevenLabs length), ``script_start_seconds`` and
    ``script_end_seconds``; ``path`` and ``text`` are carried through when present.

    Script timestamps give the order of lines and their deliberate pauses only.
    Each line starts after the previous line's *measured* end plus the script
    gap when that gap is at least ``min_gap_seconds``, otherwise plus
    ``breath_seconds``. A line that runs long pushes the next one later.

    On-screen ``customer`` / ``panda`` lines become ``speaking`` subshots, cut to
    the speech plus its pause. Narrator lines and real leading silence become
    ``fill`` subshots with a closed mouth.

    ``speech_mode="native"`` (default): Seedance speaks the quoted line itself and
    the speech is re-voiced into the cast voice afterwards. Before generation the
    line length is estimated from ``text`` (or the TTS length). After generation,
    pass each speaking section's ``generated_i2v_duration`` with
    ``measured_seconds`` = the re-voiced speech end (clip-relative) and ``path`` =
    the re-voiced file; the subshot then keeps that clip length.

    ``speech_mode="audio_reference"``: the ElevenLabs line is attached as the
    clip's only audio reference (legacy; Seedance does not reliably follow it).
    """
    sid = str(scene_id or "").strip()
    if not sid:
        raise ValueError("scene_id is required")
    if speech_mode not in SPEECH_MODES:
        raise ValueError(f"speech_mode must be one of {SPEECH_MODES}, got {speech_mode!r}")
    if not sections:
        raise ValueError(f"{sid} has no sections to split into subshots")
    breath = max(0.0, float(breath_seconds))
    min_gap = max(0.0, float(min_gap_seconds))
    speakers_on_screen = {str(s).strip().lower() for s in on_screen_speakers}
    opts = _allowed_options(allowed_durations)

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(sections):
        section_id = str(raw.get("section_id") or "").strip()
        if not section_id:
            raise ValueError(f"{sid} section {index} is missing section_id")
        if section_id in seen:
            raise ValueError(f"{sid} has duplicate section_id {section_id}")
        seen.add(section_id)
        measured = float(raw.get("measured_seconds") or 0.0)
        if measured <= 0:
            raise ValueError(f"{sid}/{section_id} needs a positive measured_seconds")
        start = float(raw["script_start_seconds"])
        end = float(raw.get("script_end_seconds", start + measured))
        if end < start:
            raise ValueError(f"{sid}/{section_id} script_end_seconds precedes its start")
        generated = raw.get("generated_i2v_duration")
        ordered.append(
            {
                "section_id": section_id,
                "speaker": str(raw.get("speaker") or "narrator").strip().lower(),
                "measured_seconds": measured,
                "script_start_seconds": start,
                "script_end_seconds": end,
                "path": raw.get("path"),
                "text": raw.get("text"),
                "generated_i2v_duration": float(generated) if generated else None,
            }
        )
    ordered.sort(key=lambda item: (item["script_start_seconds"], item["section_id"]))

    scene_start = (
        float(scene_script_start_seconds)
        if scene_script_start_seconds is not None
        else ordered[0]["script_start_seconds"]
    )

    subshots: list[dict[str, Any]] = []
    cursor = 0.0

    lead = ordered[0]["script_start_seconds"] - scene_start
    if lead >= min_gap:
        subshots.append(
            {
                "subshot_id": f"{sid}-lead_in",
                "kind": "fill",
                "role": "lead_in",
                "section_id": None,
                "speaker": None,
                "vo_path": None,
                "vo_seconds": 0.0,
                "relative_start_seconds": 0.0,
                "duration_seconds": round(lead, 3),
                "audio_end_seconds": 0.0,
                "lipsync": False,
                "i2v_duration": None,
                "audio_reference_path": None,
                "fill_treatment": "still",
            }
        )
        cursor = lead

    for index, section in enumerate(ordered):
        vo = section["measured_seconds"]
        if index + 1 < len(ordered):
            gap = ordered[index + 1]["script_start_seconds"] - section["script_end_seconds"]
            pause = gap if gap >= min_gap else breath
        else:
            pause = breath
        slot = section["script_end_seconds"] - section["script_start_seconds"]
        speaking = section["speaker"] in speakers_on_screen
        native = speaking and speech_mode == "native"
        generated = section["generated_i2v_duration"] if native else None
        if native and generated is None:
            vo = estimate_native_speech_seconds(section["text"], tts_seconds=vo)
        row: dict[str, Any] = {
            "subshot_id": f"{sid}-{section['section_id']}",
            "kind": "speaking" if speaking else "fill",
            "role": "dialogue" if speaking else (
                "narration" if section["speaker"] == "narrator" else "off_screen_dialogue"
            ),
            "section_id": section["section_id"],
            "speaker": section["speaker"],
            "vo_path": section["path"],
            "vo_seconds": round(vo, 3),
            "script_slot_seconds": round(slot, 3),
            "script_overrun_seconds": round(max(0.0, vo - slot), 3),
            "lipsync": speaking,
            "audio_reference_path": section["path"] if speaking and not native else None,
        }
        if native:
            row["speech_mode"] = "native"
            row["dialogue_text"] = section["text"]
            row["native_speech_estimated"] = generated is None
        if speaking and generated is not None:
            if vo > generated + 1e-6:
                raise DurationAllocationError(
                    f"{sid}/{section['section_id']} speech ends at {vo:.3f}s but the "
                    f"generated clip is only {generated:g}s"
                )
            row["i2v_duration"] = int(round(generated))
            pause = max(0.0, min(pause, row["i2v_duration"] - vo))
            row["fill_treatment"] = None
        elif speaking:
            if vo > opts[-1] + 1e-9:
                raise DurationAllocationError(
                    f"{sid}/{section['section_id']} line is {vo:.3f}s, beyond provider "
                    f"maximum {opts[-1]}s; shorten the line before i2v generation"
                )
            covering = [d for d in opts if d + 1e-9 >= vo + pause]
            if covering:
                i2v = covering[0]
            else:
                i2v = opts[-1]
                pause = max(0.0, i2v - vo)
            row["i2v_duration"] = int(i2v)
            row["fill_treatment"] = None
        else:
            row["i2v_duration"] = None
            row["fill_treatment"] = (
                "still" if vo + pause < opts[0] else "still_or_closed_mouth_motion"
            )
        row["relative_start_seconds"] = round(cursor, 3)
        row["duration_seconds"] = round(vo + pause, 3)
        row["audio_end_seconds"] = round(vo, 3)
        subshots.append(row)
        cursor += vo + pause

    return {
        "scene_id": sid,
        "content_duration_seconds": round(cursor, 3),
        "speaking_subshot_count": sum(1 for row in subshots if row["kind"] == "speaking"),
        "subshots": subshots,
    }


def place_scene_subshots(
    scene_subshots: dict[str, Any],
    *,
    effective_scene_start_seconds: float,
    effective_duration_seconds: float | None = None,
    validated_offsets: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Lay one scene's subshots on the master timeline, one cut per subshot.

    ``validated_offsets`` maps ``section_id`` to the signed lip-sync correction
    QA validated for *that* speaking subshot only. A positive value (mouth
    lags voice) delays that voice inside its subshot; a negative value (mouth
    leads) trims the head of that clip instead. Sibling lines get no offset;
    when a delayed voice no longer fits, its subshot grows and later subshots
    move later rather than overlapping it.

    The returned ``voice_tracks`` feed ``panda_render`` ``audio.voice_tracks``
    and never overlap one another.
    """
    offsets = dict(validated_offsets or {})
    scene_id = scene_subshots["scene_id"]
    rows = scene_subshots["subshots"]
    known = {row["section_id"] for row in rows if row.get("section_id")}
    for section_id, delta in offsets.items():
        if section_id not in known:
            raise ValueError(f"{scene_id} has no subshot for offset section {section_id}")
        if abs(float(delta)) > MAX_LIPSYNC_DELTA_SECONDS + 1e-9:
            raise ValueError(
                f"{scene_id}/{section_id} lip-sync offset {float(delta):.3f}s exceeds "
                f"±{MAX_LIPSYNC_DELTA_SECONDS:.1f}s"
            )

    start = float(effective_scene_start_seconds)
    cursor = start
    placed: list[dict[str, Any]] = []
    voice_tracks: list[dict[str, Any]] = []
    for row in rows:
        delta = float(offsets.get(row.get("section_id") or "", 0.0))
        if delta and row["kind"] != "speaking":
            raise ValueError(
                f"{scene_id}/{row['section_id']} is a fill; only speaking subshots take "
                "a lip-sync offset"
            )
        source_in = max(0.0, -delta)
        voice_offset = max(0.0, delta)
        duration = max(float(row["duration_seconds"]), voice_offset + float(row["vo_seconds"]))
        if row["kind"] == "speaking":
            available = float(row["i2v_duration"]) - source_in
            if duration > available + 1e-6:
                raise ValueError(
                    f"{scene_id}/{row['section_id']} needs {duration:.3f}s of motion but "
                    f"only {available:.3f}s remains after the lip-sync trim"
                )
        entry = dict(row)
        entry.update(
            {
                "scene_id": scene_id,
                "start_seconds": round(cursor, 3),
                "end_seconds": round(cursor + duration, 3),
                "duration_seconds": round(duration, 3),
                "source_in_seconds": round(source_in, 3),
                "voice_offset_seconds": round(voice_offset, 3),
                "audio_end_seconds": round(voice_offset + float(row["vo_seconds"]), 3),
                "lip_sync_offset_applied_seconds": round(delta, 3) if delta else None,
            }
        )
        placed.append(entry)
        if row.get("vo_path"):
            voice_tracks.append(
                {
                    "path": row["vo_path"],
                    "at_s": round(cursor + voice_offset, 3),
                    "speaker": row.get("speaker"),
                    "section_id": row.get("section_id"),
                    "subshot_id": row["subshot_id"],
                    "duration_s": float(row["vo_seconds"]),
                }
            )
        cursor += duration

    content = cursor - start
    if effective_duration_seconds is not None:
        total = float(effective_duration_seconds)
        tail = total - content
        if tail < -1e-6:
            raise ValueError(
                f"{scene_id} subshots need {content:.3f}s but the scene is allocated "
                f"{total:.3f}s; re-run allocate_scene_durations with the new content length"
            )
        if tail > 1e-6:
            placed.append(
                {
                    "subshot_id": f"{scene_id}-tail",
                    "scene_id": scene_id,
                    "kind": "fill",
                    "role": "tail",
                    "section_id": None,
                    "speaker": None,
                    "vo_path": None,
                    "vo_seconds": 0.0,
                    "lipsync": False,
                    "i2v_duration": None,
                    "audio_reference_path": None,
                    "fill_treatment": "still_or_closed_mouth_motion",
                    "start_seconds": round(cursor, 3),
                    "end_seconds": round(start + total, 3),
                    "duration_seconds": round(tail, 3),
                    "source_in_seconds": 0.0,
                    "voice_offset_seconds": 0.0,
                    "audio_end_seconds": 0.0,
                    "lip_sync_offset_applied_seconds": None,
                }
            )
            cursor = start + total

    overlaps = find_voice_overlaps(voice_tracks)
    if overlaps:
        raise ValueError(f"{scene_id} voice tracks overlap: {overlaps}")
    return {
        "scene_id": scene_id,
        "effective_start_seconds": round(start, 3),
        "effective_end_seconds": round(cursor, 3),
        "content_duration_seconds": round(content, 3),
        "subshots": placed,
        "voice_tracks": voice_tracks,
    }


def find_voice_overlaps(
    tracks: Sequence[dict[str, Any]],
    *,
    tolerance_seconds: float = 0.05,
) -> list[str]:
    """Return a description of every pair of voice tracks that play at once.

    Each track needs ``at_s`` and ``duration_s``; ``section_id`` or ``path`` is
    used to name it.
    """
    timed = []
    for index, track in enumerate(tracks):
        at = float(track.get("at_s", 0) or 0)
        dur = float(track["duration_s"])
        label = str(track.get("section_id") or track.get("path") or index)
        timed.append((at, at + dur, label))
    timed.sort()
    problems: list[str] = []
    for i, (a_start, a_end, a_label) in enumerate(timed):
        for b_start, b_end, b_label in timed[i + 1:]:
            if b_start >= a_end - tolerance_seconds:
                break
            overlap = min(a_end, b_end) - b_start
            problems.append(
                f"{a_label} ({a_start:.3f}-{a_end:.3f}s) and {b_label} "
                f"({b_start:.3f}-{b_end:.3f}s) overlap {overlap:.3f}s"
            )
    return problems


def snap_i2v_duration(
    vo_seconds: float,
    *,
    allowed: Sequence[int] | None = None,
    min_s: int = 5,
    max_s: int = 10,
) -> dict[str, float | int]:
    """Map VO length to an i2v ``duration`` and any on-screen hold extend.

    Parameters
    ----------
    vo_seconds:
        Measured narration length for the scene (sum of section VO files).
    allowed:
        Model-allowed durations from ``models_explore``. When omitted, uses every
        integer second from ``min_s`` through ``max_s`` inclusive.
    min_s / max_s:
        Bounds when ``allowed`` is omitted; also used to clamp an empty/invalid
        ``allowed`` list.

    Returns
    -------
    dict with:
      - ``vo_seconds`` (float, rounded)
      - ``i2v_duration`` (int) — pass to Higgsfield ``generate_video``
      - ``hold_extend_seconds`` (float) — how much longer the scene slot must
        run past the i2v clip so the full VO plays (0 when clip covers VO)
    """
    vo = max(0.0, float(vo_seconds))
    if allowed:
        opts = sorted({int(x) for x in allowed if int(x) > 0})
    else:
        lo, hi = int(min_s), int(max_s)
        if hi < lo:
            lo, hi = hi, lo
        opts = list(range(lo, hi + 1))
    if not opts:
        opts = [max(1, int(min_s))]

    # Prefer the shortest allowed duration that can cover the VO.
    need = math.ceil(vo) if vo > 0 else opts[0]
    covering = [d for d in opts if d >= need]
    if covering:
        i2v = covering[0]
        hold = 0.0
    else:
        i2v = opts[-1]
        hold = max(0.0, vo - float(i2v))

    return {
        "vo_seconds": round(vo, 3),
        "i2v_duration": int(i2v),
        "hold_extend_seconds": round(hold, 3),
    }


def allocate_scene_durations(
    scenes: Sequence[dict[str, Any]],
    target_duration_seconds: float,
    *,
    tolerance_fraction: float = 0.05,
    transition_overlap_seconds: float = 0.0,
    max_tail_hold_seconds: float = 2.0,
) -> dict[str, Any]:
    """Allocate unequal, audio-safe scene durations near a requested total.

    Every scene must provide ``scene_id`` and may provide:

    - ``audio_end_seconds``: end of scene-local VO, including leading silence;
    - ``vo_seconds``: measured narration length (used when audio_end is absent);
    - ``allowed_durations``: provider-supported integer i2v durations;
    - ``fixed_i2v_duration``: already-approved sample duration, when immutable;
    - ``planned_duration_seconds``: the scene-plan duration, used as a weight;
    - ``narrative_role``: optional role used to favor useful visual breathing room;
    - ``subshot_content_seconds``: ``content_duration_seconds`` from
      :func:`build_scene_subshots`. Such a scene is several cuts, not one clip:
      it may take its content length plus 0–10 whole seconds, the excess
      becomes a closed-mouth ``tail_fill_seconds``, and ``i2v_duration`` is null.

    Provider-supported durations are preferred over synthetic holds. The returned
    cumulative timeline accounts for transition overlap, so narration can be
    placed at ``effective_start_seconds + original_scene_local_offset`` without
    changing its relationship to lip-synced mouth motion.
    """
    target = float(target_duration_seconds)
    tolerance = float(tolerance_fraction)
    overlap = float(transition_overlap_seconds)
    max_hold = float(max_tail_hold_seconds)
    if target <= 0:
        raise ValueError("target_duration_seconds must be positive")
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance_fraction must be in [0, 1)")
    if overlap < 0:
        raise ValueError("transition_overlap_seconds cannot be negative")
    if max_hold < 0:
        raise ValueError("max_tail_hold_seconds cannot be negative")
    if not scenes:
        raise ValueError("scenes cannot be empty")

    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(scenes):
        scene_id = str(raw.get("scene_id") or "").strip()
        if not scene_id:
            raise ValueError(f"scene {index} is missing scene_id")
        if scene_id in seen:
            raise ValueError(f"duplicate scene_id: {scene_id}")
        seen.add(scene_id)

        subshot_content = raw.get("subshot_content_seconds")
        audio_start = max(0.0, float(raw.get("audio_start_seconds") or 0.0))
        audio_end = max(
            audio_start,
            float(
                raw.get(
                    "audio_end_seconds",
                    subshot_content
                    if subshot_content is not None
                    else raw.get("vo_seconds") or 0.0,
                )
            ),
        )
        fixed_duration = raw.get("fixed_i2v_duration")
        allowed_raw = raw.get("allowed_durations")
        if subshot_content is not None:
            content = float(subshot_content)
            if content <= 0:
                raise ValueError(f"{scene_id} subshot_content_seconds must be positive")
            audio_end = max(audio_end, content)
            allowed = [round(content + extra, 3) for extra in range(11)]
        elif fixed_duration is not None:
            allowed = [int(fixed_duration)] if int(fixed_duration) > 0 else []
        elif allowed_raw:
            allowed = sorted({int(value) for value in allowed_raw if int(value) > 0})
        else:
            allowed = list(range(5, 11))
        if not allowed:
            raise ValueError(f"{scene_id} has no valid provider-supported durations")

        candidates = [duration for duration in allowed if duration + 1e-6 >= audio_end]
        if not candidates:
            raise DurationAllocationError(
                f"{scene_id} audio ends at {audio_end:.3f}s, beyond provider maximum "
                f"{allowed[-1]}s; revise TTS pacing before i2v generation"
            )

        planned = max(
            0.001,
            float(raw.get("planned_duration_seconds") or candidates[0]),
        )
        role = str(raw.get("narrative_role") or "")
        weight = planned * _ROLE_SLACK_WEIGHTS.get(role, 1.0)
        prepared.append(
            {
                "scene_id": scene_id,
                "audio_start_seconds": audio_start,
                "audio_end_seconds": audio_end,
                "vo_seconds": max(0.0, float(raw.get("vo_seconds") or 0.0)),
                "planned_duration_seconds": planned,
                "narrative_role": role or None,
                "weight": weight,
                "candidates": candidates,
                "subshot_content_seconds": (
                    float(subshot_content) if subshot_content is not None else None
                ),
            }
        )

    overlap_total = overlap * max(0, len(prepared) - 1)
    desired_scene_sum = target + overlap_total
    weight_total = sum(scene["weight"] for scene in prepared)
    ideals = [desired_scene_sum * scene["weight"] / weight_total for scene in prepared]

    # Dynamic programming keeps the best pacing fit for each supported total.
    # Totals are keyed in milliseconds so subshot scenes can use exact lengths.
    choices_ms: dict[int, tuple[float, tuple[float, ...]]] = {0: (0.0, ())}
    for scene, ideal in zip(prepared, ideals):
        next_choices: dict[int, tuple[float, tuple[float, ...]]] = {}
        for running_ms, (running_penalty, allocation) in choices_ms.items():
            for duration in scene["candidates"]:
                total_ms = running_ms + int(round(float(duration) * 1000))
                penalty = running_penalty + ((duration - ideal) ** 2 / max(ideal, 1.0))
                candidate = (penalty, allocation + (duration,))
                current = next_choices.get(total_ms)
                if current is None or candidate < current:
                    next_choices[total_ms] = candidate
        choices_ms = next_choices
    choices = {total_ms / 1000.0: value for total_ms, value in choices_ms.items()}

    lower = target * (1.0 - tolerance)
    upper = target * (1.0 + tolerance)
    in_band = [
        (total, value)
        for total, value in choices.items()
        if lower - 1e-9 <= total - overlap_total <= upper + 1e-9
    ]

    status = "within_target_band"
    if in_band:
        chosen_total, (_penalty, allocation) = min(
            in_band,
            key=lambda item: (
                abs((item[0] - overlap_total) - target),
                (item[0] - overlap_total) < target,
                item[1][0],
                item[1][1],
            ),
        )
        holds = [0.0] * len(prepared)
    else:
        # Choose the closest supported allocation. A small shortfall may be
        # filled with bounded post-speech holds; a larger miss requires review.
        chosen_total, (_penalty, allocation) = min(
            choices.items(),
            key=lambda item: (
                abs((item[0] - overlap_total) - target),
                item[1][0],
                item[1][1],
            ),
        )
        output_without_holds = chosen_total - overlap_total
        shortfall = max(0.0, target - output_without_holds)
        holds = [0.0] * len(prepared)
        if shortfall <= max_hold * len(prepared) + 1e-9:
            remaining = shortfall
            for index in sorted(
                range(len(prepared)),
                key=lambda i: (-prepared[i]["weight"], i),
            ):
                hold = min(max_hold, remaining)
                holds[index] = hold
                remaining -= hold
                if remaining <= 1e-9:
                    break
            chosen_total += shortfall
            status = "within_target_band_with_holds"
        else:
            status = "pacing_revision_required"

    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    for index, (scene, generated, hold) in enumerate(
        zip(prepared, allocation, holds)
    ):
        effective = float(generated) + hold
        start = cursor
        end = start + effective
        content = scene["subshot_content_seconds"]
        row = {
            "scene_id": scene["scene_id"],
            "narrative_role": scene["narrative_role"],
            "planned_duration_seconds": round(scene["planned_duration_seconds"], 3),
            "audio_start_seconds": round(scene["audio_start_seconds"], 3),
            "audio_end_seconds": round(scene["audio_end_seconds"], 3),
            "vo_seconds": round(scene["vo_seconds"], 3),
            "i2v_duration": None if content is not None else int(generated),
            "effective_duration_seconds": round(effective, 3),
            "effective_start_seconds": round(start, 3),
            "effective_end_seconds": round(end, 3),
            "tail_hold_seconds": round(hold, 3),
            "safe_tail_seconds": round(
                max(0.0, effective - scene["audio_end_seconds"]), 3
            ),
        }
        if content is not None:
            row["subshot_content_seconds"] = round(content, 3)
            row["tail_fill_seconds"] = round(max(0.0, effective - content), 3)
        timeline.append(row)
        cursor = end - (overlap if index < len(prepared) - 1 else 0.0)

    output_duration = sum(
        scene["effective_duration_seconds"] for scene in timeline
    ) - overlap_total
    within_band = lower - 1e-6 <= output_duration <= upper + 1e-6
    return {
        "version": "1.0",
        "target_duration_seconds": round(target, 3),
        "tolerance_fraction": round(tolerance, 4),
        "minimum_duration_seconds": round(lower, 3),
        "maximum_duration_seconds": round(upper, 3),
        "transition_overlap_seconds": round(overlap, 3),
        "output_duration_seconds": round(output_duration, 3),
        "status": status if within_band else "pacing_revision_required",
        "within_target_band": within_band,
        "scenes": timeline,
    }
