"""Snap measured VO length to a Higgsfield i2v duration + optional hold extend.

Used by panda-video TTS-first assets: generate ElevenLabs first, probe duration,
then pick an integer clip length the model allows. HOLD mouths stay; this only
aligns pacing so the visual slot can carry the VO.
"""

from __future__ import annotations

import math
from typing import Sequence


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
