"""Snap measured VO length to a Higgsfield i2v duration + optional hold extend.

Used by panda-video TTS-first assets: generate ElevenLabs first, probe duration,
then pick an integer clip length the model allows. HOLD mouths stay; this only
aligns pacing so the visual slot can carry the VO.
"""

from __future__ import annotations

import math
from typing import Any, Sequence


class DurationAllocationError(ValueError):
    """Raised when measured audio cannot fit a provider-supported scene."""


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
) -> float:
    """Place VO on an allocated timeline without accumulating resume drift."""
    relative = float(original_section_start_seconds) - float(
        original_scene_start_seconds
    )
    result = (
        float(effective_scene_start_seconds)
        + relative
        + float(validated_lip_sync_delta_seconds)
    )
    if result < 0:
        raise ValueError("effective audio start cannot be negative")
    return round(result, 6)


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
    - ``narrative_role``: optional role used to favor useful visual breathing room.

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

        audio_start = max(0.0, float(raw.get("audio_start_seconds") or 0.0))
        audio_end = max(
            audio_start,
            float(raw.get("audio_end_seconds", raw.get("vo_seconds") or 0.0)),
        )
        fixed_duration = raw.get("fixed_i2v_duration")
        allowed_raw = raw.get("allowed_durations")
        if fixed_duration is not None:
            allowed = [int(fixed_duration)] if int(fixed_duration) > 0 else []
        elif allowed_raw:
            allowed = sorted({int(value) for value in allowed_raw if int(value) > 0})
        else:
            allowed = list(range(5, 11))
        if not allowed:
            raise ValueError(f"{scene_id} has no valid provider-supported durations")

        candidates = [duration for duration in allowed if duration + 1e-9 >= audio_end]
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
            }
        )

    overlap_total = overlap * max(0, len(prepared) - 1)
    desired_scene_sum = target + overlap_total
    weight_total = sum(scene["weight"] for scene in prepared)
    ideals = [desired_scene_sum * scene["weight"] / weight_total for scene in prepared]

    # Dynamic programming keeps the best pacing fit for each supported total.
    choices: dict[int, tuple[float, tuple[int, ...]]] = {0: (0.0, ())}
    for scene, ideal in zip(prepared, ideals):
        next_choices: dict[int, tuple[float, tuple[int, ...]]] = {}
        for running_total, (running_penalty, allocation) in choices.items():
            for duration in scene["candidates"]:
                total = running_total + duration
                penalty = running_penalty + ((duration - ideal) ** 2 / max(ideal, 1.0))
                candidate = (penalty, allocation + (duration,))
                current = next_choices.get(total)
                if current is None or candidate < current:
                    next_choices[total] = candidate
        choices = next_choices

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
        timeline.append(
            {
                "scene_id": scene["scene_id"],
                "narrative_role": scene["narrative_role"],
                "planned_duration_seconds": round(
                    scene["planned_duration_seconds"], 3
                ),
                "audio_start_seconds": round(scene["audio_start_seconds"], 3),
                "audio_end_seconds": round(scene["audio_end_seconds"], 3),
                "vo_seconds": round(scene["vo_seconds"], 3),
                "i2v_duration": int(generated),
                "effective_duration_seconds": round(effective, 3),
                "effective_start_seconds": round(start, 3),
                "effective_end_seconds": round(end, 3),
                "tail_hold_seconds": round(hold, 3),
                "safe_tail_seconds": round(
                    max(0.0, effective - scene["audio_end_seconds"]), 3
                ),
            }
        )
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
