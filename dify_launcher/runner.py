"""Runners drive a job through the pipeline gates.

A runner advances a job to its NEXT human-approval gate, then stops. Each HTTP call
(start / respond) moves the job forward one leg. This mirrors how the real agent works:
it runs until a checkpoint writes `awaiting_human`, then pauses for Dify.

Two runners:
  - MockRunner        : no LLM, no Higgsfield. Fakes script + a TEXT scene plan, generates
                        mock media in the assets stage, and REALLY renders a clean master via
                        panda_render. Lets us test the whole Dify handshake + gates, no EC2.
  - ClaudeCodeRunner  : the EC2 path — invokes Claude Code headless against the engine repo.
                        Skeleton only; swap it in where the box has `claude` + OpenRouter + MCP.

Gate sequence (matches pipeline_defs/panda-video.yaml — upstream shape + Panda cost gates):
    start ─▶ GATE 1 approve_script ─▶ GATE 2 approve_scene_plan (TEXT)
          ─▶ [GATE 2.5 approve_hero_still] ─▶ GATE 3 approve_stills
          ─▶ [GATE 3.5 approve_motion_sample] ─▶ GATE 4 approve_assets ─▶ GATE 5 approve_final
          ─▶ GATE 6 approve_brand ─▶ done
scene_plan produces a TEXT plan only (no media). The assets stage runs in human-reviewed phases
(all stage="assets"): first (default on) ONE HERO STILL look-lock; then the full STILLS storyboard
(cheap — approve remaining frames before video); then, when motion_sample is on (default off),
ONE still is animated into a MOTION SAMPLE; then the full media (clips + voice + music) in
asset_manifest. Pauses are distinguished by partial_progress.phase
("hero_still" | "stills" | "motion_sample" | full). Opt out of look-lock with options.hero_still=false.
panda-image has no hero gate (its single stills gate is the look-lock). approve_brand is
launcher-only after the last content gate.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from dify_launcher import store
from dify_launcher.storyboard_preview import (
    apply_storyboard_preview,
    is_storyboard_name,
    is_superseded_still,
    ordered_still_basenames,
    still_basenames,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[1]

# ordered human-approval gates. Note: approve_hero_still + approve_stills + approve_motion_sample
# + approve_assets are pauses of the SINGLE `assets` stage — see _sync/_do_hero_still/_do_stills.
# approve_hero_still is ON by default (opt out with hero_still:false); approve_motion_sample only
# when motion_sample is on (default false).
GATES = ["approve_script", "approve_scene_plan", "approve_hero_still", "approve_stills",
         "approve_motion_sample", "approve_assets", "approve_final", "approve_brand"]
# gates from the previous storyboard-stills flow — resuming one is refused with a migration note
_LEGACY_GATES = {"approve_storyboard", "approve_clips"}


def _motion_sample_enabled(state: dict[str, Any]) -> bool:
    """Whether to insert the one-clip motion-sample cost gate (job option, default OFF)."""
    v = (state.get("options") or {}).get("motion_sample", False)
    return str(v).lower() not in ("false", "0", "no", "off", "")


def _hero_still_enabled(state: dict[str, Any]) -> bool:
    """Whether to insert the one-still look-lock gate (default ON; opt out with hero_still:false).

    Never for panda-image — its single approve_stills gate is already the look-lock.
    """
    if _is_image(state):
        return False
    v = (state.get("options") or {}).get("hero_still", True)
    return str(v).lower() not in ("false", "0", "no", "off", "")


def _hero_scene_index(scene_plan: dict[str, Any]) -> int:
    """Index of the look-lock still: hero_moment scene, else first scene."""
    scenes = scene_plan.get("scenes") or []
    if not scenes:
        return 0
    for i, sc in enumerate(scenes):
        if isinstance(sc, dict) and sc.get("hero_moment"):
            return i
    return 0


def _assets_phase_from_checkpoint(latest: dict[str, Any],
                                  arts: Optional[dict[str, Any]] = None) -> tuple[Optional[str], dict[str, Any]]:
    """Resolve assets pause phase from top-level partial_progress, with nested fallbacks."""
    pp = latest.get("partial_progress") if isinstance(latest.get("partial_progress"), dict) else {}
    phase = pp.get("phase")
    if phase:
        return str(phase), dict(pp)
    # Agent mistake: nest under checkpoint metadata.partial_progress (seen in production).
    meta_cp = latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
    nested_pp = meta_cp.get("partial_progress") if isinstance(meta_cp.get("partial_progress"), dict) else {}
    if nested_pp.get("phase"):
        merged = dict(pp)
        merged.update(nested_pp)
        return str(nested_pp["phase"]), merged
    blob = arts if isinstance(arts, dict) else {}
    if not blob:
        blob = latest.get("artifacts") if isinstance(latest.get("artifacts"), dict) else {}
    manif = blob.get("asset_manifest") if isinstance(blob.get("asset_manifest"), dict) else {}
    if not manif:
        raw = blob.get("_checkpoint_artifacts")
        if isinstance(raw, dict) and isinstance(raw.get("asset_manifest"), dict):
            manif = raw["asset_manifest"]
    meta = manif.get("metadata") if isinstance(manif.get("metadata"), dict) else {}
    nested = meta.get("stage_phase") or meta.get("phase")
    if not nested:
        return None, dict(pp)
    merged = dict(pp)
    merged["phase"] = nested
    if meta.get("hero_scene_id") and "hero_scene_id" not in merged:
        merged["hero_scene_id"] = meta["hero_scene_id"]
    if isinstance(meta.get("look_notes"), list) and "look_notes" not in merged:
        merged["look_notes"] = list(meta["look_notes"])
    return str(nested), merged


def _stills_only_media(arts: dict[str, Any]) -> bool:
    """True when we have storyboard stills but no clips/final yet (pre-video cost gate)."""
    return bool(arts.get("stills")) and not arts.get("clips") and not arts.get("final")


def _resolve_assets_gate(phase: Optional[str], arts: dict[str, Any]) -> str:
    """Map assets phase (+ stills-only inference) to a launcher gate."""
    known = {"hero_still": "approve_hero_still",
             "stills": "approve_stills",
             "motion_sample": "approve_motion_sample",
             "budget_hold": "budget_exceeded"}
    if phase in known:
        return known[phase]
    if _stills_only_media(arts):
        return "approve_stills"
    return "approve_assets"


def _lip_sync_warning_suffix(artifacts: Optional[dict[str, Any]]) -> str:
    """Human-facing unresolved lip-sync summary carried through both media gates."""
    blob = artifacts if isinstance(artifacts, dict) else {}
    manifest = blob.get("asset_manifest") if isinstance(blob.get("asset_manifest"), dict) else {}
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    qa = metadata.get("lip_sync_qa") if isinstance(metadata.get("lip_sync_qa"), dict) else {}
    warnings = qa.get("unresolved_warnings") if isinstance(qa.get("unresolved_warnings"), list) else []
    scenes = qa.get("scenes") if isinstance(qa.get("scenes"), dict) else {}
    affected = [
        str(scene_id)
        for scene_id, report in scenes.items()
        if isinstance(report, dict) and report.get("unresolved_warning")
    ]
    if not warnings and not affected:
        return ""
    scene_text = ", ".join(affected) if affected else "unknown"
    return (
        f" Lip-sync QA warning remains after the bounded correction for scene(s): {scene_text}. "
        "Review those scenes before deciding; this warning does not block delivery."
    )


def _safe_checkpoint_question(checkpoint: dict[str, Any], fallback: str) -> str:
    """Compose agent-authored gate copy onto the mandatory launcher question.

    Custom copy is additive only: lip-sync warnings and revise instructions from
    ``fallback`` must never be dropped when the agent supplies a ``question``.
    """
    raw = checkpoint.get("question")
    if not isinstance(raw, str):
        return fallback
    custom = raw.replace("\x00", "").strip()
    if not custom:
        return fallback
    # If the agent already included the mandatory wording, keep their text.
    if fallback and fallback in custom:
        return custom[:4000]
    if not fallback:
        return custom[:4000]
    composed = f"{custom} {fallback}".strip()
    return composed[:4000]


def _question_for_gate(gate: Optional[str], *, stage: Optional[str] = None,
                       artifacts: Optional[dict[str, Any]] = None) -> str:
    """Human-facing question for a gate. Shared by MockRunner and ClaudeCodeRunner._sync.

    Mochi / Dify MUST key user-facing \"X is ready\" copy off `gate` (and may use this
    `question`), never `stage` alone — hero / stills / motion / full assets all share
    stage=\"assets\".
    """
    if gate == "approve_script":
        return "Approve the script, or request a revision."
    if gate == "approve_scene_plan":
        return "Approve the scene plan (text), or request a revision."
    if gate == "approve_hero_still":
        return ("Approve this HERO still (look lock) — palette, character, lighting, "
                "wardrobe — or request a revision. Remaining storyboard stills are generated "
                "only after this look is locked.")
    if gate == "approve_stills":
        return ("Approve the stills (one per scene) — on-model and well-composed? — or "
                "request a revision. No video is generated until the stills are approved.")
    if gate == "approve_motion_sample":
        return ("Approve the MOTION on this one sample clip (camera, animation, how the panda "
                "moves) before all clips are generated — or request a revision of the motion.")
    if gate == "approve_assets":
        return (
            "Approve the generated media (clips + audio), or request revision of "
            "specific shots (send {\"decision\":\"revise\",\"shots\":[i,...]})."
            + _lip_sync_warning_suffix(artifacts)
        )
    if gate == "approve_final":
        return (
            "Approve the finished (unbranded) video, or request a revision. "
            "Branding is the next gate and does not flow through animation."
            + _lip_sync_warning_suffix(artifacts)
        )
    if stage:
        return f"Approve {stage}, or request a revision."
    return "Approve, or request a revision."


def _budget_cap(state: dict[str, Any]) -> Optional[int]:
    """Approved Higgsfield credit ceiling for the run (job option `max_higgsfield_credits`).
    None = no cap (unlimited — today's behavior). Credits are the authoritative enforcement unit."""
    v = (state.get("options") or {}).get("max_higgsfield_credits")
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_BRAND_SPEAKERS = ("panda", "customer", "narrator")


def _resolve_voice_id(narrator: str, language: str) -> Optional[str]:
    """The literal ElevenLabs voice id for narrator+language from config/panda-elements.json.

    Resolved HERE, in the launcher, so a prompt can carry the id itself instead of an instruction
    to go look it up — the same reason the Higgsfield Element UUIDs are interpolated (CHARACTER
    LOCK). Returns None when the pair does not resolve; callers turn that into a gate blocker,
    never a silent downgrade. Never raises: a missing or malformed config yields None like any
    other unresolvable pair."""
    try:
        with open(_ENGINE_ROOT / "config" / "panda-elements.json", encoding="utf-8") as fh:
            voices = (json.load(fh) or {}).get("voices") or {}
    except (OSError, ValueError):
        return None
    entry = voices.get(str(narrator or "").strip().lower())
    if not isinstance(entry, dict):
        return None
    vid = entry.get(str(language or "").strip().lower())
    return vid.strip() if isinstance(vid, str) and vid.strip() else None


def _resolve_voice_cast(language: str) -> dict[str, Optional[str]]:
    """All three brand speaker voice ids for a language (None where unconfigured)."""
    lang = str(language or "").strip().lower()
    return {sp: _resolve_voice_id(sp, lang) for sp in _BRAND_SPEAKERS}


def _voice_line(options: dict[str, Any]) -> str:
    """VOICE CAST — brand speaker map for multi-voice scripts (CHARACTER LOCK counterpart).

    Carried by EVERY prompt whose leg can call ElevenLabs, not just the start prompt: a leg is a
    cold `claude -p`, so a voice named once at job start is gone by the time the media leg runs.

    `options.narrator` is the **default** speaker for sections that omit `speaker`.
    `options.voice_id` remains a global override that forces every line to one id."""
    opts = options or {}
    lang = str(opts.get("language", "en")).lower()
    default_speaker = str(opts.get("narrator", "panda")).lower()
    fallback = (" Only if ElevenLabs ITSELF is unavailable (an infrastructure failure — never a "
                "missing id) may you fall back to Higgsfield audio, and you must record that "
                "decision in decision_log.")

    override = opts.get("voice_id")
    if override:
        return (f"VOICE CAST — OVERRIDE: use ElevenLabs voice_id='{override}' for EVERY narration "
                "line (explicit override from the job). Ignore section.speaker brand ids. "
                "Never substitute another voice." + fallback)

    cast = _resolve_voice_cast(lang)
    missing = [sp for sp, vid in cast.items() if not vid]
    if missing:
        miss = ", ".join(f"{sp}/{lang}" for sp in missing)
        return (f"VOICE CAST — BLOCKER: language='{lang}' is missing brand voice id(s) in "
                f"config/panda-elements.json `voices` for: {miss}. Do NOT improvise a voice, do NOT "
                "borrow a neighbouring language, and do NOT fall back to Higgsfield audio to get "
                "past this. Generate everything else, then stop at the gate "
                "(status='awaiting_human') and name the unconfigured speaker/language pair(s) "
                "in the question.")

    lines = "\n".join(f"  {sp}={cast[sp]}" for sp in _BRAND_SPEAKERS)
    default_id = cast.get(default_speaker) or cast["panda"]
    if default_speaker not in cast or not cast.get(default_speaker):
        # Unknown default speaker name — still emit the cast, but flag default fallback to panda.
        default_note = (f"default speaker '{default_speaker}' is not a brand speaker — use "
                        f"panda={cast['panda']} for untagged sections")
    else:
        default_note = (f"default speaker={default_speaker} → voice_id='{default_id}' "
                        f"(options.narrator)")

    return (f"VOICE CAST — language={lang}; {default_note}\n"
            f"{lines}\n"
            "Pick the id from this map using each script section's `speaker` "
            "(`customer`|`panda`|`narrator`). Untagged sections use the default speaker above. "
            "One shot may have multiple timed sections (up to all three speakers); generate one "
            "TTS file per section (e.g. vo-{section_id}-{speaker}.mp3). Narration with any id "
            "not in this map is a defect; do not ship it." + fallback)


def _pair_scale_lock_line() -> str:
    """Literal panda/customer scale contract carried into every cold media leg."""
    ratio, tolerance = 0.58, 0.05
    try:
        with open(_ENGINE_ROOT / "config" / "panda-elements.json", encoding="utf-8") as fh:
            lock = (
                (json.load(fh) or {})
                .get("character_references", {})
                .get("pair_scale_lock", {})
            )
        ratio = float(lock.get("panda_height_ratio", ratio))
        tolerance = float(lock.get("ratio_tolerance", tolerance))
    except (OSError, TypeError, ValueError):
        pass
    low, high = ratio - tolerance, ratio + tolerance
    return (
        "PAIR SCALE LOCK — binding whenever panda + customer share a frame: standing customer "
        f"height=1.00; panda ear-top height={ratio:.2f} (acceptable {low:.2f}-{high:.2f}). "
        "Both feet share the same ground line; panda ear-top aligns around the customer's lower "
        "chest / upper abdomen. Customer stays upright and relaxed; panda stays upright, broad, "
        "rounded, short-legged, and bipedal. Preserve this relative scale, body proportions, "
        "posture, and ground plane in every still and every i2v frame — no growth, shrinkage, "
        "depth trick, crouch, or camera move that changes apparent ratio. Review every paired "
        "still plus beginning/middle/end clip frames; persist metadata.character_scale_qa and "
        "surface scene-specific warnings at the assets gate."
    )


def _audio_lipsync_enabled(options: Optional[dict[str, Any]]) -> bool:
    """True unless options.audio_lipsync is explicitly false/off (default ON).

    Missing key, None, or blank string all mean ON — only explicit false/0/no/off opt out.
    """
    if not options or "audio_lipsync" not in options:
        return True
    raw = options.get("audio_lipsync")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s == "":
        return True
    return s not in ("false", "0", "no", "off")


def _ensure_audio_lipsync_default(options: Optional[dict[str, Any]],
                                  pipeline: str = "panda-video") -> dict[str, Any]:
    """Materialize audio_lipsync:true on panda-video when the key was omitted."""
    opts = dict(options or {})
    if pipeline == "panda-video" and "audio_lipsync" not in opts:
        opts["audio_lipsync"] = True
    return opts


def _audio_lipsync_line(options: Optional[dict[str, Any]]) -> str:
    """AUDIO LIPSYNC — Seedance audio_references for on-screen customer/panda (default on)."""
    if not _audio_lipsync_enabled(options):
        return ("AUDIO LIPSYNC — OFF for this job. Keep TTS-first duration-driven i2v with HOLD "
                "LOCK (mouth frozen) and lay ElevenLabs VO at compose. Do NOT pass "
                "audio_references.\n")
    return (
        "AUDIO LIPSYNC — ON (default; pass options.audio_lipsync:false to opt out). "
        "TTS-first still mandatory. For on-screen customer/panda speaking scenes that produce "
        "a video clip: after probing VO duration, use Higgsfield model seedance_2_0 with "
        "medias start_image=approved still and audio_references=that scene's timing-preserving "
        "ElevenLabs VO bed (MCP media_upload), generate_audio:false, duration from "
        "the full-scene timeline allocation (or snap_i2v_duration for a pre-allocation motion sample). "
        "Prompt: keep 2D + Element LOCK; animate mouth/jaw to lip-sync the attached audio; "
        "subtle idle only — no walking, no new person, no photoreal/3D. Do NOT mouth-freeze "
        "(drop HOLD LOCK for these shots only). Narrator-only / text_card / no-face scenes stay "
        "HOLD or static. Multi-speaker on one clip: build one timing-preserving scene-local bed "
        "using each section's offset relative to scene start (adelay + amix); preserve pauses "
        "and overlaps and never join files back-to-back. At compose, move each original VO with "
        "its allocated scene while preserving that immutable scene-local offset. If audio_references "
        "upload/generate fails: fall back to HOLD + duration-only i2v, log in decision_log, "
        "continue. Compose still mutes native AAC (silent when generate_audio:false) and lays "
        "the same ElevenLabs VO bed.\n"
    )


def _lip_sync_qa_line(options: Optional[dict[str, Any]]) -> str:
    """Bounded local QA/retry instruction for audio-driven Panda clips."""
    if not _audio_lipsync_enabled(options):
        return ""
    return (
        "LIP-SYNC QA — before approve_assets, run lipsync_qa on every audio_lipsync:true "
        "customer/panda clip with its exact scene-local VO bed; mark narrator/HOLD clips skipped. "
        "Review the sampled mouth frames and persist asset_manifest.metadata.lip_sync_qa. "
        "On fail_timing, apply and locally re-check the measured edit offset without regeneration. "
        "On fail_generation, preflight and regenerate only that scene once with the same model, "
        "VO, still, and duration plus immediate-speaking/face-visible direction; checkpoint the "
        "retry job id immediately and retain both takes. Never spend on inconclusive/tool failure, "
        "never submit attempt 3, and never retry passing scenes. Select the better take. A second "
        "failure still reaches approve_assets with an unresolved warning.\n"
    )


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_MANDARIN_CJK_MIN = 8


def _brief_looks_mandarin(brief: str) -> bool:
    """True when the brief has enough CJK ideographs to be Mandarin content (not one brand glyph)."""
    return len(_CJK_RE.findall(brief or "")) >= _MANDARIN_CJK_MIN


def _coerce_language_from_brief(options: Optional[dict[str, Any]],
                                brief: str) -> tuple[dict[str, Any], bool]:
    """Brief wins over stale language:en — Mandarin briefs coerce to zh for VOICE CAST.

    Returns (options_copy, coerced). Explicit voice_id wins (no coerce). Non-en language left alone.
    """
    opts = dict(options or {})
    if opts.get("voice_id"):
        return opts, False
    lang = str(opts.get("language", "") or "").strip().lower()
    if lang and lang != "en":
        return opts, False
    if not _brief_looks_mandarin(brief):
        return opts, False
    opts["language"] = "zh"
    return opts, True


def _apply_language_coerce(state: dict[str, Any]) -> bool:
    """Persist coerced options onto job state. Returns True when language was flipped to zh."""
    opts, coerced = _coerce_language_from_brief(state.get("options"), state.get("brief") or "")
    state["options"] = _ensure_audio_lipsync_default(opts, _pipeline_of(state))
    if coerced:
        state["language_coerced_from_brief"] = True
    return coerced


def _language_lock_note(options: dict[str, Any], *, coerced: bool = False) -> str:
    """When language is zh, tell the agent not to block asking which language to use."""
    if str(options.get("language", "")).lower() != "zh":
        return ""
    if coerced:
        return ("LANGUAGE — set to zh from Mandarin brief content (overrode stale language:en). "
                "Use zh narration/captions and the VOICE CAST map above. Do NOT stop to re-ask "
                "language — write the checkpoint and gate as usual.\n")
    return ("LANGUAGE — zh is locked for this job. Do NOT stop to re-ask language — write the "
            "checkpoint and gate as usual.\n")


_DEFAULT_PIPELINE = os.environ.get("PANDA_PIPELINE_TYPE", "panda-video")


def _pipeline_of(state: dict[str, Any]) -> str:
    """Per-job pipeline name. Falls back to PANDA_PIPELINE_TYPE / panda-video."""
    p = state.get("pipeline") or _DEFAULT_PIPELINE
    return str(p).strip() or _DEFAULT_PIPELINE


_STILLS_TERMINAL = frozenset({"panda-carousel", "panda-image"})


def _is_carousel(state: dict[str, Any]) -> bool:
    return _pipeline_of(state) == "panda-carousel"


def _is_image(state: dict[str, Any]) -> bool:
    return _pipeline_of(state) == "panda-image"


def _is_stills_terminal(state: dict[str, Any]) -> bool:
    """Carousel and single-image jobs finish at approve_stills (no motion/clips/compose)."""
    return _pipeline_of(state) in _STILLS_TERMINAL


def _script_gate_enabled(state: dict[str, Any]) -> bool:
    """False when options.gates is set and omits script (carousel gate-collapse). Default True."""
    gates = (state.get("options") or {}).get("gates")
    if not gates:
        return True
    names = {str(g).lower().replace("approve_", "").strip() for g in gates}
    return "script" in names


# stills revise at GATE 3 (carousel + video): explicit mode, else infer from the note
_FRESH_NOTE_RE = re.compile(
    r"\b(regenerate|redo|new|from scratch|different scene|start over|fresh)\b", re.I)
_EDIT_NOTE_RE = re.compile(
    r"\b(change|fix|remove|keep|edit|adjust|replace)\b", re.I)


def _stills_revise_mode(response: dict[str, Any]) -> str:
    """Resolve stills revise mode: explicit `fresh`/`edit`, else infer. Default fresh."""
    raw = (response or {}).get("mode")
    if raw is not None and str(raw).strip():
        m = str(raw).strip().lower()
        if m in ("fresh", "edit"):
            return m
    note = str((response or {}).get("answer") or "")
    shots = (response or {}).get("shots") or []
    if _FRESH_NOTE_RE.search(note):
        return "fresh"
    if shots and _EDIT_NOTE_RE.search(note):
        return "edit"
    return "fresh"


def _revise_shot_indices(response: dict[str, Any], n: int) -> list[int]:
    """1-based `shots` from /respond; empty means all. Returns 0-based indices in range."""
    shots = (response or {}).get("shots") or []
    if not shots or n <= 0:
        return list(range(max(n, 0)))
    out: list[int] = []
    for s in shots:
        try:
            i = int(s)
        except (TypeError, ValueError):
            continue
        if i >= 1:
            i -= 1
        if 0 <= i < n and i not in out:
            out.append(i)
    return out or list(range(n))


def _still_basename(name: Any) -> str:
    return Path(str(name)).name


def _still_abs_paths(job_id: str, state: Optional[dict[str, Any]], shots: list[Any],
                     projects_dir: Optional[Path] = None) -> list[str]:
    """Absolute paths of current stills (flagged shots if set, else all)."""
    names = [_still_basename(n) for n in (state or {}).get("artifacts", {}).get("stills") or []
             if not is_superseded_still(n)]
    if not names:
        return []
    indices = _revise_shot_indices({"shots": shots}, len(names))
    engine_images = (projects_dir / job_id / "assets" / "images") if projects_dir else None
    paths: list[str] = []
    for i in indices:
        basename = names[i]
        if engine_images is not None:
            eng = engine_images / basename
            if eng.is_file():
                paths.append(str(eng.resolve()))
                continue
        p = store.artifact_path(job_id, basename)
        paths.append(str(p.resolve()) if p.exists() else str(p))
    return paths


# carousel slide canvas — caller sets options.aspect_ratio (default 4:5). Do not coerce to 4:5/1:1.
_CAROUSEL_PIXEL_SIZES = {
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "3:4": (1080, 1440),
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "4:3": (1440, 1080),
}


def _stills_aspect(options: Optional[dict[str, Any]] = None,
                   state: Optional[dict[str, Any]] = None,
                   pipeline: Optional[str] = None) -> str:
    """Job option `aspect_ratio`. Default 1:1 for panda-image, 4:5 otherwise. Pass-through."""
    p = pipeline or _pipeline_of(state or {})
    default = "1:1" if p == "panda-image" else "4:5"
    opts = options if options is not None else ((state or {}).get("options") or {})
    raw = str((opts or {}).get("aspect_ratio") or default).strip()
    return raw or default


def _carousel_aspect(options: Optional[dict[str, Any]] = None,
                     state: Optional[dict[str, Any]] = None) -> str:
    """Job option `aspect_ratio`, default 4:5 (carousel / video). Pass-through."""
    return _stills_aspect(options=options, state=state)


def _carousel_pixel_size(ratio: str) -> tuple[int, int]:
    """Mock placeholder size for a carousel ratio. Unknown W:H → 1080 on the short side."""
    key = str(ratio or "4:5").strip().lower().replace(" ", "")
    if key in _CAROUSEL_PIXEL_SIZES:
        return _CAROUSEL_PIXEL_SIZES[key]
    m = re.match(r"^(\d+)x(\d+)$", key)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w > 0 and h > 0:
            return (w, h)
    m = re.match(r"^(\d+):(\d+)$", key)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            if a <= b:
                return (1080, max(1, round(1080 * b / a)))
            return (max(1, round(1080 * a / b)), 1080)
    return _CAROUSEL_PIXEL_SIZES["4:5"]


def _script_to_markdown(script: dict[str, Any]) -> str:
    """Human-readable script.md for Dify's file-preview slot (JSON stays on artifacts.script)."""
    title = str(script.get("title") or "Script")
    lines = [f"# {title}", ""]
    dur = script.get("total_duration_seconds")
    if dur is not None:
        lines.append(f"_Duration: {dur}s_")
        lines.append("")
    for i, sec in enumerate(script.get("sections") or [], 1):
        if not isinstance(sec, dict):
            continue
        label = sec.get("label") or sec.get("id") or f"Section {i}"
        start, end = sec.get("start_seconds"), sec.get("end_seconds")
        timing = ""
        if start is not None or end is not None:
            timing = f" ({start}s–{end}s)"
        lines.append(f"## {label}{timing}")
        text = str(sec.get("text") or "").strip()
        if text:
            lines.append(text)
        directions = str(sec.get("speaker_directions") or "").strip()
        if directions:
            lines.append("")
            lines.append(f"*{directions}*")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _scene_plan_to_markdown(plan: dict[str, Any]) -> str:
    """Human-readable scene_plan.md for Dify's file-preview slot."""
    lines = ["# Scene plan", ""]
    meta = plan.get("metadata") if isinstance(plan.get("metadata"), dict) else {}
    ratio = meta.get("aspect_ratio")
    if ratio:
        lines.append(f"_Aspect ratio: {ratio}_")
        lines.append("")
    for i, sc in enumerate(plan.get("scenes") or [], 1):
        if not isinstance(sc, dict):
            continue
        sid = sc.get("id") or f"scene-{i}"
        lines.append(f"## {sid}")
        desc = str(sc.get("description") or "").strip()
        if desc:
            lines.append(desc)
        bits = []
        if sc.get("framing"):
            bits.append(f"Framing: {sc['framing']}")
        if sc.get("movement"):
            bits.append(f"Movement: {sc['movement']}")
        if bits:
            lines.append("")
            lines.append("; ".join(str(b) for b in bits))
        caps = sc.get("captions") if isinstance(sc.get("captions"), dict) else {}
        if caps:
            lines.append("")
            if caps.get("zh"):
                lines.append(f"- zh: {caps['zh']}")
            if caps.get("en"):
                lines.append(f"- en: {caps['en']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_text_previews(job_id: str, arts: dict[str, Any],
                         gate: Optional[str] = None) -> dict[str, Any]:
    """Write script.md / scene_plan.md from inline JSON. `preview` is THIS gate's file only.

    Does not replace artifacts.script / artifacts.scene_plan dicts. Does not put .md in stills.
    A structured script dict still wins over a stray .md (never treat cost_report.md as script).
    """
    store.ensure_job(job_id)
    script = arts.get("script")
    if isinstance(script, dict):
        store.artifact_path(job_id, "script.md").write_text(
            _script_to_markdown(script), encoding="utf-8")
        arts["script_md"] = "script.md"
    elif isinstance(script, str) and Path(str(script)).name.lower() == "script.md":
        if store.artifact_path(job_id, "script.md").is_file():
            arts["script_md"] = "script.md"
    plan = arts.get("scene_plan")
    if isinstance(plan, dict):
        store.artifact_path(job_id, "scene_plan.md").write_text(
            _scene_plan_to_markdown(plan), encoding="utf-8")
        arts["scene_plan_md"] = "scene_plan.md"
    preview_name = None
    if gate == "approve_script" and arts.get("script_md"):
        preview_name = "script.md"
    elif gate == "approve_scene_plan" and arts.get("scene_plan_md"):
        preview_name = "scene_plan.md"
    if preview_name:
        arts["preview"] = [preview_name]
    else:
        arts.pop("preview", None)
    return arts


def _apply_previews(job_id: str, arts: dict[str, Any],
                    gate: Optional[str] = None) -> dict[str, Any]:
    """Unified preview resolver for the single Dify `preview` slot.

    Dual-surfaces the text .md copies (script.md / scene_plan.md) and, at the stills
    gate, the storyboard composite — then picks ONE preview for the current gate:
    script.md at approve_script, scene_plan.md at approve_scene_plan, the single hero
    PNG at approve_hero_still, the storyboard at approve_stills, and nothing at any
    other gate. Inline JSON on artifacts.script / artifacts.scene_plan is left untouched.
    """
    # Always write the .md copies (so downloads exist); no preview set here.
    _write_text_previews(job_id, arts, None)
    # Storyboard writes its files + preview only at approve_stills; drops preview otherwise.
    apply_storyboard_preview(job_id, arts,
                             "approve_stills" if gate == "approve_stills" else None)
    # Text gates / hero look-lock own the preview slot over the (now-cleared) storyboard default.
    if gate == "approve_script" and arts.get("script_md"):
        arts["preview"] = ["script.md"]
    elif gate == "approve_scene_plan" and arts.get("scene_plan_md"):
        arts["preview"] = ["scene_plan.md"]
    elif gate == "approve_hero_still":
        names = still_basenames(arts)
        if names:
            arts["stills"] = names
            arts["preview"] = [names[0]]
        else:
            arts.pop("preview", None)
    elif gate != "approve_stills":
        arts.pop("preview", None)
    return arts


class BrandError(ValueError):
    """Raised by brand_job when the job cannot be branded. `.status_code` is the HTTP code."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _artifact_files_ready(job_id: str, names: list[str]) -> bool:
    return bool(names) and all(store.artifact_path(job_id, n).is_file() for n in names)


def _brand_video_master(src: Path, dest: Path, prof: dict[str, Any]) -> None:
    """Overlay the BGC wordmark on a copy of the UGC master. Keeps audio."""
    from montage_svc.render import ffmpeg_ops as ff  # noqa: WPS433
    from montage_svc.render.overlays import scene_overlay  # noqa: WPS433

    w, h = ff.probe_size(src)
    dur = ff.probe_duration(src)
    with tempfile.TemporaryDirectory(prefix="dify-brand-") as td:
        png = Path(td) / "logo.png"
        drawn = scene_overlay(prof, w, h, None, [], png, with_logo=True)
        if not drawn or not png.is_file():
            raise BrandError("BGC logo overlay produced no image", 500)
        # Cover the whole cut (duration + slack) so the last frames keep the mark.
        try:
            ff.overlay_timed(src, [(png, 0.0, max(dur + 1.0, 0.05))], dest)
        except ff.RenderError as e:
            raise BrandError(f"video brand overlay failed: {e}", 500) from e


def _open_brand_gate(state: dict[str, Any]) -> dict[str, Any]:
    """Pause for BGC overlay choice. UGC artifacts are already on the job."""
    arts = dict(state.get("artifacts") or {})
    arts.setdefault("branded", False)
    state.update(
        status="awaiting_human", stage="brand", gate="approve_brand",
        question="Apply the BGC wordmark to copies of the approved stills/final? "
                 "Approve to brand, skip to keep UGC, or revise to decide later. "
                 "Branding does not flow through animation.",
        artifacts=arts,
    )
    return state


def _mark_brand_done(state: dict[str, Any], resolved: str) -> dict[str, Any]:
    state["brand_resolved"] = resolved
    state.update(status="done", gate=None, question=None)
    return state


def _apply_brand(state: dict[str, Any], profile: str = "bgc") -> dict[str, Any]:
    """Stamp BGC copies of stills and/or the video master. Does not change status."""
    profile = (profile or "bgc").strip().lower()
    if profile != "bgc":
        raise BrandError("only profile 'bgc' is supported for the brand pass", 400)

    arts = dict(state.get("artifacts") or {})
    job_id = state["job_id"]
    stills = [_still_basename(n) for n in (arts.get("stills") or [])
              if not is_superseded_still(n)]
    final_name = _still_basename(arts.get("final") or "")
    final_src = store.artifact_path(job_id, final_name) if final_name.endswith(".mp4") else None
    has_final = bool(final_src and final_src.is_file())
    branded_final_name = "final.bgc.mp4"
    branded_final_path = store.artifact_path(job_id, branded_final_name)

    if not stills and not has_final:
        raise BrandError("no stills or final to brand", 409)

    existing_stills = [_still_basename(n) for n in (arts.get("branded_stills") or [])]
    stills_ready = _artifact_files_ready(job_id, existing_stills)
    video_ready = has_final and branded_final_path.is_file()

    if (not stills or stills_ready) and (not has_final or video_ready):
        if stills_ready:
            arts["branded_stills"] = existing_stills
        if video_ready:
            arts["branded_final"] = branded_final_name
        arts["branded"] = True
        state["artifacts"] = arts
        return state

    _VENDOR = _ENGINE_ROOT / "vendor"
    os.environ.setdefault("MONTAGE_BRAND_DIR", str(_VENDOR / "brand"))
    os.environ.setdefault("MONTAGE_DATA_DIR", str(_VENDOR / "data"))
    if str(_VENDOR) not in sys.path:
        sys.path.insert(0, str(_VENDOR))
    from montage_svc.storage import ensure_profiles, load_profile  # noqa: WPS433
    from montage_svc.render.overlays import draw_logo  # noqa: WPS433
    from PIL import Image

    ensure_profiles()
    prof = load_profile("bgc")

    if stills and not stills_ready:
        branded: list[str] = []
        for name in stills:
            src = store.artifact_path(job_id, name)
            if not src.is_file():
                raise BrandError(f"still {name!r} is missing from the job store", 409)
            out_name = f"{Path(name).stem}.bgc.png"
            img = Image.open(src).convert("RGBA")
            draw_logo(img, prof)
            img.save(store.artifact_path(job_id, out_name))
            branded.append(out_name)
        arts["branded_stills"] = branded
    elif stills_ready:
        arts["branded_stills"] = existing_stills

    if has_final and not video_ready:
        assert final_src is not None
        _brand_video_master(final_src, branded_final_path, prof)
        arts["branded_final"] = branded_final_name
    elif video_ready:
        arts["branded_final"] = branded_final_name

    arts["branded"] = True
    state["artifacts"] = arts
    return state


def _resolve_brand_gate(state: dict[str, Any], decision: str) -> dict[str, Any]:
    """approve = stamp then done; skip = UGC done; revise = stay at approve_brand."""
    decision = (decision or "approve").strip().lower()
    if decision == "revise":
        return _open_brand_gate(state)
    if decision == "skip":
        arts = dict(state.get("artifacts") or {})
        arts["branded"] = False
        state["artifacts"] = arts
        return _mark_brand_done(state, "skipped")
    if decision == "approve":
        _apply_brand(state)
        return _mark_brand_done(state, "applied")
    raise ValueError(f"approve_brand expects approve|skip|revise, got {decision!r}")


def brand_job(state: dict[str, Any], profile: str = "bgc") -> dict[str, Any]:
    """Stamp the BGC wordmark onto approved stills and/or the video master.

    Job must be `done` (after skip, or a second pass). At `approve_brand` use
    POST /respond instead. Idempotent per output. UGC originals stay.
    """
    if state.get("gate") == "approve_brand":
        raise BrandError("at approve_brand use POST /respond (approve|skip|revise), not /brand", 409)
    if state.get("status") != "done":
        raise BrandError(f"job is {state.get('status')!r}, not done — finish the brand gate first", 409)
    return _apply_brand(state, profile)


# Placeholder per-asset Higgsfield credits the MockRunner uses to exercise budget enforcement
# (the real runner uses each asset's actual get_cost credits from the manifest).
_MOCK_STILL_CREDITS = 4
_MOCK_CLIP_CREDITS = 14

_MIGRATION_MSG = (
    "This job was created under the previous storyboard-stills flow (gate "
    "'{gate}'), which no longer exists after the scene_plan revert to the upstream "
    "text-plan + assets architecture. The job is still readable, but cannot be resumed. "
    "Please start a new job."
)


def _legacy_migration(state: dict[str, Any]) -> dict[str, Any]:
    """A saved job sitting at a removed gate: leave it readable, fail with a clear message."""
    state.update(status="failed", gate=None,
                 question=_MIGRATION_MSG.format(gate=state.get("gate")))
    return state


class Runner:
    """Interface. advance() takes the job state + an optional human response and returns
    the updated state, stopping at the next gate (or done)."""

    def start(self, state: dict[str, Any]) -> dict[str, Any]:  # noqa: D401
        raise NotImplementedError

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# MockRunner — testable end to end with no LLM / no Higgsfield
# ---------------------------------------------------------------------------

class MockRunner(Runner):
    def start(self, state: dict[str, Any]) -> dict[str, Any]:
        state.setdefault("pipeline", _pipeline_of(state))
        _apply_language_coerce(state)
        if _is_image(state):
            return self._do_scene_plan(state, {})
        if not _script_gate_enabled(state):
            self._do_script(state, {})
            return self._do_scene_plan(state, {})
        return self._do_script(state, {})

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        decision = (response or {}).get("decision", "approve")
        gate = state.get("gate")

        if gate in _LEGACY_GATES:
            return _legacy_migration(state)

        # a raised budget cap may accompany any response — persist it onto the job options
        new_cap = (response or {}).get("max_higgsfield_credits")
        if new_cap is not None:
            state.setdefault("options", {})["max_higgsfield_credits"] = new_cap

        # cancel: only meaningful at the budget hold — stop the job, spend nothing more
        if decision == "cancel":
            state.update(status="failed", gate=None,
                         question="Job cancelled at the budget gate — no further Higgsfield credits spent.")
            return state

        if gate == "approve_brand":
            return _resolve_brand_gate(state, decision)

        # revise: regenerate the CURRENT gate's artifact, stay at the same gate
        if decision == "revise":
            regen = {
                "approve_script": self._do_script,
                "approve_scene_plan": self._do_scene_plan,
                "approve_hero_still": self._do_hero_still,
                "approve_stills": self._do_stills,
                "approve_motion_sample": self._do_motion_sample,
                "approve_assets": self._do_assets,
                "budget_exceeded": self._do_assets,          # revise the requested generation to fit
                "approve_final": self._do_production,
            }.get(gate)
            if not regen:
                raise ValueError(f"cannot revise from gate {gate!r}")
            return regen(state, response)

        # approve: advance to the next stage/gate
        if gate == "budget_exceeded":
            return self._do_assets(state, response)          # cap raised → retry the batch (re-checks)
        if gate == "approve_script":
            return self._do_scene_plan(state, response)
        if gate == "approve_scene_plan":
            if _hero_still_enabled(state):
                return self._do_hero_still(state, response)  # assets: one hero look-lock still
            return self._do_stills(state, response)          # assets: full stills (hero_still off)
        if gate == "approve_hero_still":
            return self._do_stills(state, response)          # look locked → remaining storyboard stills
        if gate == "approve_stills":
            if _is_stills_terminal(state):
                return self._finish_stills_job(state)   # terminal — no motion / clips / compose
            # assets phase 2: one motion sample first (if enabled), else straight to full media
            if _motion_sample_enabled(state):
                return self._do_motion_sample(state, response)
            return self._do_assets(state, response)
        if gate == "approve_motion_sample":
            return self._do_assets(state, response)       # assets phase 3: all clips + audio + manifest
        if gate == "approve_assets":
            return self._do_production(state, response)
        if gate == "approve_final":
            return _open_brand_gate(state)
        raise ValueError(f"cannot resume from gate {gate!r}")

    # --- GATE 1: script ----------------------------------------------------
    def _do_script(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        brief = state.get("brief", "")
        note = (response or {}).get("answer")
        explain = "Explain the tip." + (f" (revised: {note})" if note else "")
        script_obj = {
            "version": "1.0",
            "title": "Script (mock)",
            "total_duration_seconds": 9.0,
            "sections": [
                {"id": "s1", "label": "Open", "text": "Open on the Panda mascot.",
                 "start_seconds": 0, "end_seconds": 3,
                 "speaker_directions": f"Brief: {brief}"},
                {"id": "s2", "label": "Explain", "text": explain,
                 "start_seconds": 3, "end_seconds": 6},
                {"id": "s3", "label": "CTA", "text": "CTA.",
                 "start_seconds": 6, "end_seconds": 9},
            ],
        }
        arts = {**state.get("artifacts", {}), "script": script_obj}
        _write_text_previews(job_id, arts, "approve_script")
        state.update(
            stage="script", status="awaiting_human", gate="approve_script",
            question=_question_for_gate("approve_script"),
            artifacts=arts,
        )
        return state

    # --- GATE 2: TEXT scene plan (no media generated here) -----------------
    def _do_scene_plan(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Produce ONLY a schema-valid, TEXT scene plan (upstream contract). No stills, no
        media — each scene declares required_assets the assets stage fulfils later."""
        job_id = state["job_id"]
        brief = state.get("brief", "")
        note = (response or {}).get("answer")
        stills_only = _is_stills_terminal(state)
        n = 1 if _is_image(state) else 3
        scenes = []
        for i in range(n):
            role = ("hook", "content", "cta")[i] if i < 3 else "content"
            if _is_image(state):
                role = "cta"
            asset = {"type": "image", "description": f"Panda keyframe for scene {i+1}",
                     "source": "generate"}
            scene = {
                "id": f"scene-{i+1}",
                "type": "generated",
                "description": (f"Scene {i+1} for: {brief}"
                                + (f" (revised: {note})" if note else "")),
                "start_seconds": float(i * 3),
                "end_seconds": float((i + 1) * 3),
                "framing": "medium",
                "movement": "static",
                "narrative_role": {"hook": "establish_context", "content": "deliver_payload",
                                   "cta": "call_to_action"}[role],
                "required_assets": [asset] if stills_only else [
                    asset,
                    {"type": "video", "description": f"Motion clip for scene {i+1}",
                     "source": "generate"},
                ],
            }
            if stills_only:
                scene["captions"] = {
                    "en": f"Slide {i+1} {role}",
                    "zh": f"第{i+1}页 {role}",
                }
            # Middle scene is the look-lock beat when we have 3+ scenes (tests + mock parity).
            if n >= 3 and i == 1:
                scene["hero_moment"] = True
            scenes.append(scene)
        scene_plan = {"version": "1.0", "scenes": scenes}
        if stills_only:
            scene_plan["metadata"] = {"aspect_ratio": _stills_aspect(state=state)}
        store.artifact_path(job_id, "scene_plan.json").write_text(
            json.dumps(scene_plan, indent=2), encoding="utf-8")
        # Surface the plan inline (dict) so Dify can review it as TEXT — no stills here.
        # Also write scene_plan.md and set preview to that file (current gate only).
        arts = {k: v for k, v in state.get("artifacts", {}).items() if k != "stills"}
        arts["scene_plan"] = scene_plan
        _write_text_previews(job_id, arts, "approve_scene_plan")
        state.update(
            stage="scene_plan", status="awaiting_human", gate="approve_scene_plan",
            question=_question_for_gate("approve_scene_plan"),
            artifacts=arts,
        )
        return state

    # --- GATE 2.5: assets PHASE 0 — one HERO STILL (look lock before storyboard) --
    def _do_hero_still(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Generate ONE hero still from the approved scene plan — look-lock before the full
        storyboard. Scene with hero_moment, else scene 1. Revise stays here (fresh|edit);
        look_notes accumulate for the remaining-stills pass."""
        job_id = state["job_id"]
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        n = max(len(scenes), 1)
        hero_idx = _hero_scene_index(scene_plan)
        hero_sid = scenes[hero_idx]["id"] if hero_idx < len(scenes) else f"scene-{hero_idx + 1}"

        look_notes = list(state.get("look_notes") or [])
        decision = (response or {}).get("decision", "approve")
        note = (response or {}).get("answer") or ""
        if decision == "revise" and note.strip():
            look_notes.append(note.strip())

        all_names = [f"still_{i:02d}.png" for i in range(n)]
        existing = [_still_basename(x) for x in (state.get("artifacts", {}).get("stills") or [])
                    if not is_superseded_still(x) and not is_storyboard_name(x)]
        if existing:
            all_names[hero_idx] = existing[0]

        if decision == "revise" and existing:
            if _stills_revise_mode(response) == "edit":
                edited = self._edit_existing_stills(
                    job_id, [all_names[hero_idx]], [0], note)
                stills = [edited[0]]
            else:
                filled = self._placeholder_stills(
                    job_id, n, state=state, indices=[hero_idx], existing=all_names)
                stills = [filled[hero_idx]]
        else:
            filled = self._placeholder_stills(
                job_id, n, state=state, indices=[hero_idx], existing=all_names)
            stills = [filled[hero_idx]]

        arts = {k: v for k, v in state.get("artifacts", {}).items()
                if k not in ("clips", "asset_manifest")}
        arts["stills"] = stills
        arts["hero_scene_id"] = hero_sid
        _apply_previews(job_id, arts, "approve_hero_still")
        state["look_notes"] = look_notes
        state.update(
            stage="assets", status="awaiting_human", gate="approve_hero_still",
            question=_question_for_gate("approve_hero_still"),
            artifacts=arts,
        )
        return state

    # --- GATE 3: assets PHASE 1 — stills only (cheap, pre-video cost gate) --
    def _do_stills(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Generate STILLS ONLY from the approved scene plan — one per scene, NO video yet.
        This is the cost/visual checkpoint: approve the look (on-model panda, composition)
        before any expensive image->video spend. Real gen is Higgsfield image generation; the
        mock makes placeholder stills. On the box this checkpoint carries
        partial_progress.phase='stills' so the launcher surfaces it as approve_stills.

        When arriving from an approved hero look-lock, keep the hero PNG and generate only
        the remaining scenes (mock: placeholders; real agent: look-reference i2i).

        Revise at this gate is dual-mode: `mode=fresh` regenerates placeholders; `mode=edit`
        draws the note onto copies of the existing PNGs (keeps size/colors). Honor `shots`."""
        job_id = state["job_id"]
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        n = len(scenes) or 3
        existing = [_still_basename(x) for x in (state.get("artifacts", {}).get("stills") or [])
                    if not is_superseded_still(x) and not is_storyboard_name(x)]
        decision = (response or {}).get("decision", "approve")
        if decision == "revise" and existing:
            indices = _revise_shot_indices(response, len(existing))
            if _stills_revise_mode(response) == "edit":
                stills = self._edit_existing_stills(
                    job_id, existing, indices, (response or {}).get("answer") or "")
            else:
                stills = self._placeholder_stills(
                    job_id, max(n, len(existing)), state=state,
                    indices=indices, existing=existing)
        else:
            # Coming from hero look-lock: one still at hero index — fill the rest, keep hero.
            if len(existing) == 1 and n > 1:
                hero_idx = _hero_scene_index(scene_plan)
                base = [f"still_{i:02d}.png" for i in range(n)]
                base[hero_idx] = existing[0]
                rest = [i for i in range(n) if i != hero_idx]
                stills = self._placeholder_stills(
                    job_id, n, state=state, indices=rest, existing=base)
            else:
                stills = self._placeholder_stills(job_id, n, state=state)
        # entering the stills phase drops any clips/manifest from a prior pass
        arts = {k: v for k, v in state.get("artifacts", {}).items()
                if k not in ("clips", "asset_manifest")}
        arts["stills"] = stills
        _apply_previews(job_id, arts, "approve_stills")
        state.update(
            stage="assets", status="awaiting_human", gate="approve_stills",
            question=_question_for_gate("approve_stills"),
            artifacts=arts,
        )
        return state

    # --- GATE 3.5: assets PHASE 2 — one MOTION SAMPLE (approve motion before batching) --
    def _do_motion_sample(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Animate ONE hero still into a sample clip so the reviewer approves the motion/animation
        feel BEFORE all clips are generated (the biggest cost/time gate). Real gen is one
        Higgsfield image_to_video; the mock renders a short clip from the first still. On the box
        this checkpoint carries partial_progress.phase='motion_sample' -> approve_motion_sample."""
        job_id = state["job_id"]
        stills = list(state.get("artifacts", {}).get("stills", []))
        if not stills:
            stills = self._placeholder_stills(job_id, 3)
        hero = stills[0]                                          # hero = scene 1 in the mock
        sample_name = "motion_sample.mp4"
        self._render_clean([str(store.artifact_path(job_id, hero))],
                           str(store.artifact_path(job_id, sample_name)))
        # entering the motion-sample phase drops any full clips/manifest from a prior pass
        arts = {k: v for k, v in state.get("artifacts", {}).items()
                if k not in ("clips", "asset_manifest")}
        arts["stills"] = stills
        arts["motion_sample"] = sample_name
        _apply_previews(job_id, arts, "approve_motion_sample")
        state.update(
            stage="assets", status="awaiting_human", gate="approve_motion_sample",
            question=_question_for_gate("approve_motion_sample"),
            artifacts=arts,
        )
        return state

    # --- GATE 4: assets PHASE 3 — TTS-first VO, then duration-driven i2v + manifest
    def _do_assets(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """TTS-first then animate APPROVED stills into motion clips (duration from measured VO)
        plus music; record everything in asset_manifest. Real gen is ElevenLabs then Higgsfield
        MCP (image_to_video); the mock renders a short clip per still. Reviewer approves the set
        or revises specific shots (response.shots) — only those regenerate."""
        job_id = state["job_id"]
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        stills = list(state.get("artifacts", {}).get("stills", []))
        n = len(stills) or (len(scenes) or 3)
        if len(stills) != n:                                     # safety: stills come from GATE 3
            stills = self._placeholder_stills(job_id, n)
        only = set((response or {}).get("shots", []))  # optional: regenerate specific shots

        # HARD PRE-GENERATION BUDGET BLOCK: if animating the batch would push cumulative Higgsfield
        # spend past the approved credit cap, generate NOTHING and pause for a human decision.
        cap = _budget_cap(state)
        if cap is not None:
            spent = len(stills) * _MOCK_STILL_CREDITS \
                + (_MOCK_CLIP_CREDITS if state.get("artifacts", {}).get("motion_sample") else 0)
            requested = n * _MOCK_CLIP_CREDITS               # clips about to be animated
            if spent + requested > cap:
                return self._budget_hold(state, cap, spent, requested)

        clips = list(state.get("artifacts", {}).get("clips", []))
        if len(clips) != n:
            clips = [None] * n
        for i in range(n):
            if only and i not in only and clips[i]:
                continue  # keep already-approved shot
            clip_name = f"clip_{i:02d}.mp4"
            self._render_clean(
                [str(store.artifact_path(job_id, stills[i]))],
                str(store.artifact_path(job_id, clip_name)),
            )
            clips[i] = clip_name

        assets = []
        for i in range(n):
            sid = scenes[i]["id"] if i < len(scenes) else f"scene-{i+1}"
            assets.append({"id": f"img-{i:02d}", "type": "image", "path": stills[i],
                           "source_tool": "mock_still", "scene_id": sid, "provider": "higgsfield",
                           "credits": _MOCK_STILL_CREDITS, "credits_source": "estimated"})
            assets.append({"id": f"vid-{i:02d}", "type": "video", "path": clips[i],
                           "source_tool": "mock_clip", "scene_id": sid, "provider": "higgsfield",
                           "credits": _MOCK_CLIP_CREDITS, "credits_source": "estimated"})
        asset_manifest = {"version": "1.0", "assets": assets, "total_cost_usd": 0.0}
        store.artifact_path(job_id, "asset_manifest.json").write_text(
            json.dumps(asset_manifest, indent=2), encoding="utf-8")

        arts = {**state.get("artifacts", {}), "stills": stills, "clips": clips,
                "asset_manifest": asset_manifest}
        _apply_previews(job_id, arts, "approve_assets")
        state.update(
            stage="assets", status="awaiting_human", gate="approve_assets",
            question=_question_for_gate("approve_assets"),
            artifacts=arts,
        )
        return state

    # --- BUDGET HOLD: hard pre-generation block inside the assets lifecycle -
    def _budget_hold(self, state: dict[str, Any], cap: int, spent: int, requested: int) -> dict[str, Any]:
        """Nothing was generated. Pause and require the human to raise the cap, revise, or cancel."""
        projected = spent + requested
        arts = dict(state.get("artifacts") or {})
        _apply_previews(state["job_id"], arts, "budget_exceeded")
        state.update(
            stage="assets", status="awaiting_human", gate="budget_exceeded",
            question=(f"BUDGET HOLD — generating the requested clips would use ~{requested} more "
                      f"Higgsfield credits ({spent} already spent → ~{projected} total), over the "
                      f"approved cap of {cap}. NO clips were generated. Respond with one of: raise "
                      "the cap {\"decision\":\"approve\",\"max_higgsfield_credits\":<n>}; revise the "
                      "plan {\"decision\":\"revise\",\"answer\":\"…\"}; or cancel {\"decision\":\"cancel\"}."),
            artifacts=arts,
        )
        return state

    # --- edit/compose (no gates) -> GATE 4 clean master --------------------
    def _do_production(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        clips = state.get("artifacts", {}).get("clips", [])
        scene_paths = [str(store.artifact_path(job_id, c)) for c in clips]
        if not scene_paths:
            scene_paths = [self._placeholder_stills(job_id, 2)[0]]

        out = store.artifact_path(job_id, "final.mp4")
        self._render_clean(scene_paths, str(out))

        arts = {**state.get("artifacts", {}), "final": "final.mp4", "branded": False}
        _apply_previews(job_id, arts, "approve_final")
        state.update(
            stage="compose", status="awaiting_human", gate="approve_final",
            question=_question_for_gate("approve_final"),
            artifacts=arts,
        )
        return state

    # --- helpers -----------------------------------------------------------
    def _finish_stills_job(self, state: dict[str, Any]) -> dict[str, Any]:
        """Approve stills on carousel/image: write image-only asset_manifest, then brand gate."""
        job_id = state["job_id"]
        stills = list(state.get("artifacts", {}).get("stills") or [])
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        assets = []
        for i, name in enumerate(stills):
            sid = scenes[i]["id"] if i < len(scenes) else f"scene-{i+1}"
            assets.append({"id": f"img-{i:02d}", "type": "image", "path": name,
                           "source_tool": "mock_still", "scene_id": sid, "provider": "higgsfield",
                           "credits": _MOCK_STILL_CREDITS, "credits_source": "estimated"})
        asset_manifest = {"version": "1.0", "assets": assets, "total_cost_usd": 0.0}
        store.artifact_path(job_id, "asset_manifest.json").write_text(
            json.dumps(asset_manifest, indent=2), encoding="utf-8")
        arts = {**state.get("artifacts", {}), "stills": stills,
                "asset_manifest": asset_manifest, "branded": False}
        _apply_previews(job_id, arts, None)
        state.update(
            artifacts=arts,
        )
        return _open_brand_gate(state)

    def _placeholder_stills(self, job_id: str, n: int,
                            state: Optional[dict[str, Any]] = None,
                            indices: Optional[list[int]] = None,
                            existing: Optional[list[str]] = None) -> list[str]:
        from PIL import Image, ImageDraw
        colors = [(11, 11, 11), (253, 197, 13), (30, 30, 30)]
        if state and _is_stills_terminal(state):
            size = _carousel_pixel_size(_stills_aspect(state=state))
        else:
            size = (1080, 1920)
        if existing:
            names = [_still_basename(x) for x in existing]
            while len(names) < n:
                names.append(f"still_{len(names):02d}.png")
        else:
            names = [f"still_{i:02d}.png" for i in range(n)]
        targets = list(range(len(names))) if indices is None else indices
        for i in targets:
            if not (0 <= i < len(names)):
                continue
            img = Image.new("RGB", size, colors[i % len(colors)])
            d = ImageDraw.Draw(img)
            d.text((60, size[1] // 2), f"Scene {i+1}", fill=(255, 255, 255))
            p = store.artifact_path(job_id, names[i])
            img.save(p)
            names[i] = p.name
        return names

    def _edit_existing_stills(self, job_id: str, names: list[str],
                              indices: list[int], note: str) -> list[str]:
        """Image-to-image mock: stamp the revision note onto copies of existing stills.
        Keeps original size and colors; only the flagged indices are rewritten."""
        from PIL import Image, ImageDraw
        out = [_still_basename(x) for x in names]
        for i in indices:
            if not (0 <= i < len(out)):
                continue
            p = store.artifact_path(job_id, out[i])
            img = Image.open(p).convert("RGB")
            w, h = img.size
            d = ImageDraw.Draw(img)
            bar_h = min(80, max(40, h // 16))
            d.rectangle([0, h - bar_h, w, h], fill=(0, 0, 0))
            d.text((20, h - bar_h + 10), f"EDIT: {(note or '')[:120]}", fill=(255, 255, 255))
            img.save(p)
        return out

    def _render_clean(self, scene_paths: list[str], out_path: str) -> None:
        """Real render via the folded panda_render tool (clean/ugc, no branding)."""
        if str(_ENGINE_ROOT) not in sys.path:
            sys.path.insert(0, str(_ENGINE_ROOT))
        from tools.video.panda_render import PandaRender
        res = PandaRender().execute({
            "scenes": [{"media_path": p, "duration_s": 2.5} for p in scene_paths],
            "fps": 30, "grade": "none",
            "output_path": out_path,
        })
        if not res.success:
            raise RuntimeError(f"panda_render failed: {getattr(res, 'error', '?')}")


# ---------------------------------------------------------------------------
# ClaudeCodeRunner — the EC2 path (real agent)
# ---------------------------------------------------------------------------

# OpenMontage stage  ->  launcher gate name (matches pipeline_defs/panda-video.yaml).
# NOTE: the `assets` stage is deliberately absent here — it surfaces TWO gates chosen by the
# checkpoint's partial_progress.phase: "stills" -> approve_stills (pre-video), else
# approve_assets (full media). See _sync() and _gate_stage().
_STAGE_GATE = {
    "script": "approve_script",
    "scene_plan": "approve_scene_plan",   # TEXT plan (no media)
    "compose": "approve_final",
}


class ClaudeCodeRunner(Runner):
    """Drives the REAL agent: Claude Code headless against the engine repo.

    Each start()/resume() runs the agent until it writes an `awaiting_human` checkpoint (or
    the pipeline finishes), then mirrors the checkpoint + artifacts into the launcher job
    store. OpenMontage's resume is checkpoint-based, so we don't depend on a CLI session —
    every leg is a fresh `claude -p` that reads the latest checkpoint and continues.

    Config (env):
      CLAUDE_BIN          claude CLI path (default "claude")
      CLAUDE_EXTRA_ARGS   extra CLI args, space-split (e.g. "--dangerously-skip-permissions")
      CLAUDE_TIMEOUT_S    per-leg timeout seconds (default 3600)
      PANDA_PIPELINE_TYPE pipeline manifest name (default "panda-video")
      OPENMONTAGE_PROJECTS_DIR  checkpoints/projects root (default engine/projects)
      LLM (OpenRouter): ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN inherited from process env

    >>> VERIFY ON THE BOX <<<  the exact `claude` flags, the agent's stop-at-gate behavior,
    and the artifact key/paths the panda-video skills emit (see _mirror_artifacts). The
    checkpoint calls below use the real lib/checkpoint API.
    """

    def __init__(self) -> None:
        if str(_ENGINE_ROOT) not in sys.path:
            sys.path.insert(0, str(_ENGINE_ROOT))
        from lib.paths import PROJECTS_DIR
        self._projects_dir = PROJECTS_DIR
        self._bin = os.environ.get("CLAUDE_BIN", "claude")
        self._extra = os.environ.get("CLAUDE_EXTRA_ARGS", "").split()
        self._timeout = int(os.environ.get("CLAUDE_TIMEOUT_S", "3600"))

    # -- lifecycle ----------------------------------------------------------
    def start(self, state: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        pipeline = _pipeline_of(state)
        state["pipeline"] = pipeline
        coerced = _apply_language_coerce(state)
        from lib import checkpoint as cp
        title_prefix = {"panda-carousel": "Panda carousel", "panda-image": "Panda image"}.get(
            pipeline, "Panda video")
        cp.init_project(job_id, title=(state.get("brief") or title_prefix)[:80],
                        pipeline_type=pipeline)
        start_label = "scene_plan" if pipeline == "panda-image" else "script"
        self._run_agent(self._start_prompt(job_id, state.get("brief", ""),
                                           state.get("options") or {}, pipeline,
                                           language_coerced=coerced),
                        job_id, start_label)
        state = self._sync(state)
        # Gate-collapse: options.gates omits script → auto-approve GATE 1 and continue.
        if (state.get("status") == "awaiting_human" and state.get("gate") == "approve_script"
                and not _script_gate_enabled(state)):
            self._approve_stage(job_id, "script", pipeline)
            self._run_agent(self._continue_prompt(job_id, pipeline), job_id, "scene_plan")
            state = self._sync(state)
        return state

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        if state.get("gate") in _LEGACY_GATES:
            return _legacy_migration(state)
        from lib import checkpoint as cp
        gate = state.get("gate")
        decision = (response or {}).get("decision", "approve")

        if gate == "approve_brand":
            return _resolve_brand_gate(state, decision)

        stage = self._gate_stage(gate)

        # BUDGET HOLD — the agent blocked a generation that would exceed max_higgsfield_credits.
        # The human must raise the cap, revise the requested generation, or cancel. Nothing was spent.
        if gate == "budget_exceeded":
            new_cap = (response or {}).get("max_higgsfield_credits")
            if new_cap is not None:                       # persist a raised cap for later legs
                state.setdefault("options", {})["max_higgsfield_credits"] = new_cap
            if decision == "cancel":
                state.update(status="failed", gate=None,
                             question="Job cancelled at the budget gate — no further Higgsfield credits spent.")
                return state
            if decision == "approve":
                self._run_agent(self._budget_raised_prompt(job_id, new_cap), job_id, "assets_media")
                return self._run_until_assets_gate(state, label="assets_media")
            # revise — reduce/cheapen the requested generation to fit the cap
            self._run_agent(self._revise_prompt(
                job_id, "assets (BUDGET HOLD — reduce or cheapen the requested Higgsfield generation "
                        "to fit the approved max_higgsfield_credits cap, re-check the budget hard-rule, "
                        "then continue)", response or {}),
                job_id, "assets_revise")
            return self._run_until_assets_gate(state, label="assets_revise")

        # Approving a within-assets phase (hero / stills / motion sample) must NOT complete the
        # assets stage — it only unlocks the next phase WITHIN the same stage. Continue the agent
        # from the latest checkpoint so it does the next phase and stops again at the next gate.
        # IMPORTANT: handle approve_hero_still BEFORE the generic decision=="approve" fallthrough
        # that calls _approve_stage — otherwise hero approve wrongly completes assets and skips
        # the stills/storyboard gate.
        if gate == "approve_hero_still":
            if decision != "approve":
                self._run_agent(self._revise_prompt(
                    job_id, "assets (HERO STILL phase — revise the one look-lock still and stop "
                            "again at the hero_still gate; do NOT generate the remaining storyboard "
                            "stills yet)",
                    response or {}, state=state),
                    job_id, "hero_still_revise")
                return self._sync(state)
            # Approve: generate remaining stills. If the agent exits early (asks a question)
            # without writing phase=stills, the hero checkpoint would resurface — continue
            # until we leave approve_hero_still (capped).
            return self._run_after_hero_approved(state)

        if gate == "approve_stills":
            if decision != "approve":
                self._run_agent(self._revise_prompt(
                    job_id, "assets (STILLS phase — revise the flagged stills and stop again "
                            "at the stills gate; do NOT generate video yet)",
                    response or {}, state=state),
                    job_id, "stills_revise")
                return self._sync(state)
            if _is_stills_terminal(state):
                # carousel / image are stills-terminal: complete assets and let _sync mark done
                self._approve_stage(job_id, "assets", _pipeline_of(state))
                return self._sync(state)
            if _motion_sample_enabled(state):
                # one hero clip first — approve the motion before batching all clips
                self._run_agent(self._motion_sample_prompt(job_id, state.get("options")),
                                job_id, "motion_sample")
                return self._run_until_assets_gate(state, label="motion_sample")
            # motion sample disabled: animate all approved stills straight away
            self._run_agent(self._stills_approved_prompt(job_id, state.get("options")),
                            job_id, "assets_media")
            return self._run_until_assets_gate(state, label="assets_media")

        if gate == "approve_motion_sample":
            if decision == "approve":
                self._run_agent(self._motion_approved_prompt(job_id, state.get("options")),
                                job_id, "assets_media")
                return self._run_until_assets_gate(state, label="assets_media")
            self._run_agent(self._revise_prompt(
                job_id, "assets (MOTION SAMPLE phase — regenerate ONLY the sample clip per the "
                        "feedback, keep partial_progress.phase='motion_sample', do NOT batch the "
                        "remaining clips yet)", response or {}),
                job_id, "motion_sample_revise")
            return self._run_until_assets_gate(state, label="motion_sample_revise")

        # Approving full media must run edit (ungated) → compose and land on approve_final.
        # A single continue leg that asks a question and exits leaves status=running/gate=null
        # (Dify "Agent Door sent no reply"). Cap retries then fail clearly — same pattern as
        # _run_after_hero_approved.
        if gate == "approve_assets" and decision == "approve":
            self._approve_stage(job_id, stage, _pipeline_of(state))
            return self._run_until_final_gate(state)

        if decision == "approve":
            self._approve_stage(job_id, stage, _pipeline_of(state))
            # Approving the LAST gate finishes the job — there is no next stage to run, so do
            # NOT spin up a pointless agent turn; just report done.
            nxt = cp.get_next_stage(self._projects_dir, job_id, _pipeline_of(state))
            if nxt is None:
                return self._sync(state)
            prompt = self._continue_prompt(job_id, _pipeline_of(state))
            label = nxt
        else:
            prompt = self._revise_prompt(job_id, stage, response or {})
            label = f"{stage}_revise"
        self._run_agent(prompt, job_id, label)
        return self._sync(state)

    # -- agent invocation ---------------------------------------------------
    _TRANSIENT = ("connection closed", "api error", "overloaded", "rate limit",
                  "timeout", "timed out", " 500", " 502", " 503", " 529")
    # Claude Code prints this when stdin is an open-but-empty pipe (uvicorn / systemd).
    # It is noise — never the real failure. Healthcheck already uses DEVNULL for the same reason.
    _STDIN_WARN = "no stdin data received"

    def _agent_failure_text(self, proc: Any) -> str:
        """Prefer actionable stdout (auth/API) over Claude's harmless stdin warning on stderr."""
        out = (getattr(proc, "stdout", None) or "").strip()
        err = (getattr(proc, "stderr", None) or "").strip()
        err_lines = [ln for ln in err.splitlines()
                     if self._STDIN_WARN not in ln.lower()]
        err_clean = "\n".join(err_lines).strip()
        parts = [p for p in (out, err_clean) if p]
        return "\n".join(parts).strip()

    def _run_agent(self, prompt: str, job_id: str = "", label: str = "") -> None:
        """Run `claude -p` once per leg. Retries on TRANSIENT API/network errors (a dropped
        connection shouldn't kill a long leg); the agent resumes from the latest checkpoint,
        so re-running the same prompt is safe. Records the leg's wall-time (all attempts) to
        the project's timing.jsonl for the per-project generation-time report."""
        import subprocess
        import time
        prompt = prompt + self._screenshot_facts(job_id)
        attempts = int(os.environ.get("CLAUDE_MAX_ATTEMPTS", "3"))
        last = ""
        started = time.monotonic()
        try:
            for i in range(attempts):
                proc = subprocess.run(
                    [self._bin, "-p", prompt, *self._extra],
                    cwd=str(_ENGINE_ROOT), capture_output=True, text=True,
                    stdin=subprocess.DEVNULL, timeout=self._timeout,
                )
                self._write_agent_log(job_id, label, i, proc)
                if proc.returncode == 0:
                    return
                # Search BOTH streams, minus Claude's harmless "no stdin data" warning.
                # `stderr or stdout` masked the real error: the warning is on stderr and
                # matches no _TRANSIENT pattern, so a retryable failure (529 Overloaded)
                # broke out on attempt 1 and the job died. stdin=DEVNULL above stops the
                # warning at source; the filter keeps another emitter from re-masking it.
                last = self._agent_failure_text(proc)
                low = last.lower()
                transient = any(s in low for s in self._TRANSIENT)
                if not transient or i == attempts - 1:
                    break
                time.sleep(3 * (i + 1))  # brief backoff, then let the agent resume from checkpoint
            tail = last.splitlines()[-8:]
            raise RuntimeError("claude failed: " + " | ".join(tail))
        finally:
            self._record_timing(job_id, label, round(time.monotonic() - started, 2))

    def _screenshot_facts(self, job_id: str) -> str:
        """USER SCREENSHOTS facts for jobs with uploads; "" otherwise. Never raises."""
        if not job_id:
            return ""
        try:
            from dify_launcher import screens
            return screens.facts(self._projects_dir / job_id)
        except Exception:  # noqa: BLE001 — facts are an extra; never block a leg
            return ""

    def _screenshot_question(self, state: dict[str, Any], gate: Optional[str],
                             arts: dict[str, Any], question: str) -> str:
        """`question` plus screenshot checks + board for this gate (jobs with uploads only).

        Never raises: this runs inside _sync, between the state mutation and its save.
        """
        try:
            from dify_launcher import screens
            lang = str((state.get("options") or {}).get("language") or "zh")
            notes = screens.apply_gate(self._projects_dir, state["job_id"], gate, arts,
                                       language=lang)
            return screens.question_with_notes(question, notes) if notes else question
        except Exception:  # noqa: BLE001 — a check must never break the gate
            return question

    def _write_agent_log(self, job_id: str, label: str, attempt: int, proc: Any) -> None:
        """Persist an agent leg's stdout/stderr to projects/{job}/artifacts/agent_{label}.log.

        The agent's output is otherwise discarded on a clean (rc=0) exit, so a leg that runs but
        writes no checkpoint ("agent produced no checkpoint") leaves no trace of WHY. This keeps
        the full transcript per stage for post-hoc debugging. Behaviour-neutral; never raises.
        """
        if not job_id:
            return
        try:
            adir = self._projects_dir / job_id / "artifacts"
            adir.mkdir(parents=True, exist_ok=True)
            with open(adir / f"agent_{label or 'leg'}.log", "a", encoding="utf-8") as f:
                f.write(f"\n===== attempt {attempt} rc={getattr(proc, 'returncode', '?')} =====\n")
                out = getattr(proc, "stdout", "") or ""
                err = getattr(proc, "stderr", "") or ""
                if out:
                    f.write("--- stdout ---\n" + out)
                if err:
                    f.write("\n--- stderr ---\n" + err)
                f.write("\n")
        except OSError:
            pass

    def _record_timing(self, job_id: str, label: str, seconds: float) -> None:
        """Append one leg's wall-time to projects/{job}/artifacts/timing.jsonl. Never raises."""
        if not job_id:
            return
        try:
            from datetime import datetime, timezone
            adir = self._projects_dir / job_id / "artifacts"
            adir.mkdir(parents=True, exist_ok=True)
            entry = {"ts": datetime.now(timezone.utc).isoformat(),
                     "stage": label or "unknown", "seconds": seconds}
            with open(adir / "timing.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    # -- checkpoint <-> launcher state --------------------------------------
    def _assets_cp_in_progress(self, job_id: str) -> bool:
        """True when the assets checkpoint is mid-generation (clips may still be rendering)."""
        from lib import checkpoint as cp
        cp_assets = cp.read_checkpoint(self._projects_dir, job_id, "assets") or {}
        return cp_assets.get("status") == "in_progress"

    def _run_until_assets_gate(self, state: dict[str, Any], *, label: str) -> dict[str, Any]:
        """After a media leg, keep continuing while assets stay in_progress (capped).

        Agents often exit early while Higgsfield motion jobs render. Without this loop,
        _sync used to mis-classify stills-only in_progress as approve_stills. Cap via
        CLAUDE_IN_PROGRESS_MAX (default 8) so a stuck render cannot spin forever.
        """
        job_id = state["job_id"]
        max_extra = int(os.environ.get("CLAUDE_IN_PROGRESS_MAX", "8"))
        state = self._sync(state)
        n = 0
        while (state.get("status") == "running" and self._assets_cp_in_progress(job_id)
               and n < max_extra):
            n += 1
            self._run_agent(self._assets_in_progress_prompt(job_id, state.get("options")),
                            job_id, f"{label}_continue_{n}")
            state = self._sync(state)
        if (state.get("status") == "running" and self._assets_cp_in_progress(job_id)
                and n >= max_extra):
            arts = state.get("artifacts") or {}
            state.update(
                status="running", stage="assets", gate=None,
                question=("assets generation still in progress after "
                          f"{max_extra} continue attempts — Higgsfield motion jobs may still be "
                          "rendering; poll GET /jobs/{id} or respond again later to resume"),
                artifacts=arts,
            )
        return state

    def _run_after_hero_approved(self, state: dict[str, Any]) -> dict[str, Any]:
        """Generate remaining stills after hero look-lock; do not resurface approve_hero_still.

        Seen on carousel job_b2fe9c0335af: stills leg stopped to ask a caption question without
        writing phase=stills, so _sync re-showed the hero gate. Cap via CLAUDE_STILLS_AFTER_HERO_MAX
        (default 3); if still stuck, fail clearly instead of looping the hero UI.
        """
        job_id = state["job_id"]
        self._run_agent(self._hero_approved_prompt(job_id, state), job_id, "stills")
        max_extra = int(os.environ.get("CLAUDE_STILLS_AFTER_HERO_MAX", "3"))
        state = self._sync(state)
        n = 0
        while state.get("gate") == "approve_hero_still" and n < max_extra:
            n += 1
            self._run_agent(
                self._stills_after_hero_continue_prompt(job_id, state),
                job_id, f"stills_after_hero_{n}")
            state = self._sync(state)
        if state.get("gate") == "approve_hero_still":
            state.update(
                status="failed", gate=None,
                question=("stills generation after hero approve did not advance: checkpoint still "
                          "has phase=hero_still after "
                          f"{max_extra} continue attempt(s). The agent stopped without writing "
                          "partial_progress.phase=stills — start a new job or retry respond."),
            )
        return state

    def _stuck_before_final_gate(self, state: dict[str, Any]) -> bool:
        """True while the post-approve-assets worker has not reached a terminal/gated state.

        This predicate is only used inside _run_until_final_gate, where assets are already
        approved. Do not depend on get_next_stage(): a checkpoint/state disagreement was enough
        to leave a completed edit permanently `running` with no gate.
        """
        return state.get("status") == "running" and state.get("gate") is None

    def _final_retry_gate(self, state: dict[str, Any], reason: str) -> dict[str, Any]:
        """Return a resumable media-approval gate without discarding generated assets."""
        try:
            synced = self._sync(state)
        except Exception:  # keep the original state if checkpoint sync itself is unhealthy
            synced = dict(state)
        if synced.get("gate") == "approve_final":
            return synced
        synced.update(
            status="awaiting_human",
            stage="assets",
            gate="approve_assets",
            question=(
                "Final assembly stalled after clip approval. Generated stills, clips, VO, and "
                f"music are kept. Approve to retry edit/compose only; Higgsfield media will not "
                f"be regenerated. Detail: {reason}"
                + _lip_sync_warning_suffix(synced.get("artifacts"))
            ),
        )
        return synced

    def _run_until_final_gate(self, state: dict[str, Any]) -> dict[str, Any]:
        """After approve_assets: run edit → compose until approve_final or a resumable retry gate.

        Seen on job_84e41f0738fc: the edit leg asked about a VO overrun and exited without a
        checkpoint. _sync then left status=running / gate=null / \"stage assets completed; next:
        edit\" — Dify cannot /respond and shows \"Agent Door sent no reply\". Cap via
        CLAUDE_EDIT_COMPOSE_MAX (default 3); never leave a dead running poll state or discard
        paid media.
        """
        job_id = state["job_id"]
        pipeline = _pipeline_of(state)
        try:
            self._run_agent(self._assets_approved_prompt(job_id, pipeline), job_id, "edit")
        except Exception as exc:  # timeout/auth/provider failure remains safely resumable
            return self._final_retry_gate(state, f"initial edit/compose leg failed: {exc}")
        max_extra = int(os.environ.get("CLAUDE_EDIT_COMPOSE_MAX", "3"))
        state = self._sync(state)
        n = 0
        while self._stuck_before_final_gate(state) and n < max_extra:
            n += 1
            try:
                self._run_agent(
                    self._edit_compose_continue_prompt(job_id, pipeline),
                    job_id, f"edit_continue_{n}")
            except Exception as exc:
                return self._final_retry_gate(
                    state, f"edit/compose continuation {n} failed: {exc}")
            state = self._sync(state)
        if self._stuck_before_final_gate(state):
            return self._final_retry_gate(
                state,
                "agent stopped on an ungated edit/compose stage after "
                f"{max_extra} continuation attempt(s)",
            )
        return state

    def _stills_after_hero_continue_prompt(self, job_id: str,
                                           state: Optional[dict[str, Any]] = None) -> str:
        pipe = _pipeline_of(state or {})
        carousel = (
            " This is panda-carousel: bake primary-language captions from scene_plan into stills "
            "per skills/pipelines/panda-carousel/asset-director.md (no later caption stage). "
            "If look_notes say 'no text baked in' but the brief/scene_plan require captions, "
            "FOLLOW the brief/scene_plan and bake captions — do not stop to ask."
            if pipe == "panda-carousel" else
            " Prefer pipeline defaults; do not stop to ask clarifying questions."
        )
        return (
            f"For project_id: {job_id}, the HERO STILL was ALREADY APPROVED. You previously "
            "stopped without writing partial_progress.phase=\"stills\". Do NOT reopen or revise "
            "the hero gate. Do NOT ask the human a question — decide and proceed."
            f"{carousel} KEEP the approved hero PNG. Generate any remaining scene stills under "
            "LOOK LOCK: import the hero style once, preflight all remaining take-1 stills, "
            "enforce the complete-batch budget, then submit with max 4 Higgsfield jobs in flight "
            "(2 after a 429), poll the set together, and run take 2 only for unusable take-1 "
            "results. Record them in asset_manifest, rewrite the assets checkpoint "
            "status='awaiting_human' with top-level partial_progress={{\"phase\":\"stills\"}}, "
            f"and STOP. {_pair_scale_lock_line()} That is the only valid next pause."
        )

    def _assets_in_progress_prompt(self, job_id: str,
                                   options: Optional[dict[str, Any]] = None) -> str:
        return (
            f"For project_id: {job_id}, the `assets` stage is IN PROGRESS (not awaiting stills). "
            "Read checkpoint_assets.json — especially metadata.partial_progress (motion job ids, "
            "narration/music markers). Do NOT regenerate stills or reopen the stills gate. "
            "TTS-FIRST: finish every missing ElevenLabs VO + audio_probe, then run the full-scene "
            "allocate_scene_durations timeline allocation before queueing remaining image_to_video. "
            "Preserve the requested total within ±5% with unequal audio-driven scene lengths and "
            "persist metadata.timeline_contract. Follow the AUDIO LIPSYNC line below for "
            "customer/panda clips (seedance_2_0 + audio_references when on). Poll every queued "
            "Higgsfield job until complete, download clips into assets/video/, finish any remaining "
            f"music, record everything in asset_manifest. {_pair_scale_lock_line()} Then rewrite the assets checkpoint "
            "status='awaiting_human' WITHOUT partial_progress.phase='stills' (and without "
            "motion_and_audio_in_flight) so the launcher surfaces approve_assets (full media). If "
            "jobs are still rendering, update the in_progress checkpoint with current job ids and "
            "STOP — the launcher will re-invoke you. Do NOT use /loop or background timers that "
            "exit the turn early.\n\n"
            + _voice_line(options or {}) + "\n" + _audio_lipsync_line(options)
            + _lip_sync_qa_line(options)
        )

    def _sync(self, state: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        from lib import checkpoint as cp
        latest = cp.get_latest_checkpoint(self._projects_dir, job_id)
        if not latest:
            state.update(status="failed", question="agent produced no checkpoint")
            return state
        stage, status = latest.get("stage"), latest.get("status")
        arts = self._mirror_artifacts(job_id, latest.get("artifacts", {}))
        self._write_cost_report(job_id)     # refresh the report files (API-only; not attached to arts)
        if status == "failed":
            _apply_previews(job_id, arts, None)
            state.update(status="failed", stage=stage, gate=None,
                         question=latest.get("error", "stage failed"), artifacts=arts)
        elif status == "awaiting_human":
            if stage == "assets":
                # assets pauses at: hero look-lock, stills, motion sample, CONDITIONAL budget hold,
                # then full media. Phase may be top-level partial_progress OR nested under
                # metadata.partial_progress / asset_manifest.metadata.stage_phase (agent mistakes).
                # Stills-only media with no phase → approve_stills (never approve_assets).
                phase, pp = _assets_phase_from_checkpoint(latest, arts)
                gate = _resolve_assets_gate(phase, arts)
                if isinstance(pp.get("look_notes"), list):
                    state["look_notes"] = list(pp["look_notes"])
                if pp.get("hero_scene_id"):
                    arts.setdefault("hero_scene_id", pp["hero_scene_id"])
                # Backfill top-level partial_progress when we inferred stills and checkpoint
                # lacked it (keeps future legs / resume honest).
                if (gate == "approve_stills"
                        and not (isinstance(latest.get("partial_progress"), dict)
                                 and latest["partial_progress"].get("phase"))):
                    self._backfill_assets_phase(job_id, latest, "stills", pp)
            else:
                gate = _STAGE_GATE.get(stage, f"approve_{stage}")
            _apply_previews(job_id, arts, gate)
            fallback_question = _question_for_gate(
                gate, stage=stage, artifacts=arts)
            question = self._screenshot_question(
                state, gate, arts, _safe_checkpoint_question(latest, fallback_question))
            state.update(status="awaiting_human", stage=stage, gate=gate,
                         question=question, artifacts=arts)
        elif status == "in_progress" or status not in ("completed",):
            # Mid-generation (or unknown) — keep running. NEVER treat as completed stills-only
            # recovery (that reopened approve_stills while Kling jobs were still rendering).
            _apply_previews(job_id, arts, None)
            phase, _pp = _assets_phase_from_checkpoint(latest, arts)
            phase_txt = f" ({phase})" if phase else ""
            state.update(
                status="running", stage=stage, gate=None,
                question=(f"{stage or 'pipeline'} generation in progress{phase_txt} — "
                          "poll GET /jobs/{id} until status is awaiting_human"),
                artifacts=arts,
            )
        else:  # completed
            # Recover skipped stills/storyboard gate after hero look-lock.
            # ONLY when status is completed — not in_progress mid-clip-render.
            if (stage == "assets" and not _is_stills_terminal(state)
                    and _stills_only_media(arts)):
                phase, pp = _assets_phase_from_checkpoint(latest, arts)
                gate = "approve_hero_still" if phase == "hero_still" else "approve_stills"
                if isinstance(pp.get("look_notes"), list):
                    state["look_notes"] = list(pp["look_notes"])
                if pp.get("hero_scene_id"):
                    arts.setdefault("hero_scene_id", pp["hero_scene_id"])
                if gate == "approve_stills":
                    self._backfill_assets_phase(job_id, latest, "stills", pp)
                _apply_previews(job_id, arts, gate)
                fallback_question = _question_for_gate(
                    gate, stage="assets", artifacts=arts)
                question = self._screenshot_question(
                    state, gate, arts, _safe_checkpoint_question(latest, fallback_question))
                state.update(status="awaiting_human", stage="assets", gate=gate,
                             question=question, artifacts=arts)
                return state
            nxt = cp.get_next_stage(self._projects_dir, job_id, _pipeline_of(state))
            _apply_previews(job_id, arts, None)
            if nxt is None:
                state["artifacts"] = arts
                if state.get("brand_resolved"):
                    state.update(status="done", stage=stage, gate=None, question=None)
                    return state
                return _open_brand_gate(state)
            else:
                state.update(status="running", stage=stage, gate=None,
                             question=f"stage {stage} completed; next: {nxt}", artifacts=arts)
        return state

    def _backfill_assets_phase(self, job_id: str, latest: dict[str, Any],
                               phase: str, pp: Optional[dict[str, Any]] = None) -> None:
        """Rewrite assets checkpoint as awaiting_human with top-level partial_progress.phase."""
        from lib import checkpoint as cp
        merged = dict(pp or {})
        merged["phase"] = phase
        try:
            cp.write_checkpoint(
                self._projects_dir, job_id, "assets", "awaiting_human",
                latest.get("artifacts") or {},
                pipeline_type=latest.get("pipeline_type") or _DEFAULT_PIPELINE,
                human_approval_required=True, human_approved=False,
                partial_progress=merged,
            )
        except (OSError, ValueError, TypeError):
            pass

    def _mirror_artifacts(self, job_id: str, artifacts: dict[str, Any]) -> dict[str, Any]:
        """Copy the job's artifact files into the launcher store, grouped by kind for Dify.

        Real panda-video checkpoints describe assets in rich structured text — filenames are
        embedded in prose (e.g. "...keyframe for the Hook beat: scene-1.png"), NOT exposed as
        clean path fields — so walking checkpoint strings misses them. Instead mirror by
        scanning the engine's CANONICAL project layout on disk, which init_project always
        creates: assets/images -> stills, assets/video -> clips, renders -> final. We ALSO
        merge any explicit file paths the checkpoint does contain (by basename), so a pipeline
        that emits real paths still works. Raw checkpoint data stays under _checkpoint_artifacts.
        """
        store.ensure_job(job_id)
        proj = self._projects_dir / job_id
        out: dict[str, Any] = {}
        seen: set[str] = set()

        def _copy(p: Path) -> Optional[str]:
            try:
                if not p.is_file():
                    return None
            except OSError:
                return None
            if p.name not in seen:
                store.artifact_path(job_id, p.name).write_bytes(p.read_bytes())
                seen.add(p.name)
            return p.name

        def _scan(d: Path, exts: tuple[str, ...]) -> list[Path]:
            if not d.is_dir():
                return []
            return sorted(p for p in d.iterdir()
                          if p.is_file() and p.suffix.lower() in exts
                          and not p.name.startswith("_"))

        # explicit file paths embedded in the checkpoint (guarded against free text)
        def _paths_in(v: Any) -> list[Path]:
            found: list[Path] = []
            if isinstance(v, str):
                if v and "\n" not in v and len(v) <= 400:
                    p = Path(v) if Path(v).is_absolute() else proj / v
                    try:
                        if p.is_file():
                            found.append(p)
                    except OSError:
                        pass
            elif isinstance(v, dict):
                for vv in v.values():
                    found += _paths_in(vv)
            elif isinstance(v, list):
                for vv in v:
                    found += _paths_in(vv)
            return found

        # a storyboard contact sheet is a review aid, not a scene still — keep it out of stills.
        # Archived revise leftovers (history/superseded-stills, *.pre-*) must not surface as
        # live stills or get /brand stamped.
        imgs = [p for p in _scan(proj / "assets" / "images", (".png", ".jpg", ".jpeg"))
                if "contact" not in p.name.lower() and "sheet" not in p.name.lower()
                and not is_storyboard_name(p.name)
                and not is_superseded_still(p)]
        vids = _scan(proj / "assets" / "video", (".mp4", ".mov", ".webm"))
        renders = _scan(proj / "renders", (".mp4", ".mov"))

        try:
            from lib.screen_layout import is_launcher_owned
        except Exception:  # noqa: BLE001 — never let an import break mirroring
            def is_launcher_owned(path: Path, project_dir: Path) -> bool:
                try:
                    rel = Path(path).resolve().relative_to(Path(project_dir).resolve())
                except (ValueError, OSError):
                    return False
                return bool(rel.parts) and rel.parts[0] in ("inputs", "overlay")
        for p in _paths_in(artifacts):
            if is_launcher_owned(p, proj):
                continue        # user screenshots / overlay renders are never stills or clips
            ext = p.suffix.lower()
            if (ext in (".png", ".jpg", ".jpeg") and p not in imgs and not is_superseded_still(p)
                    and not is_storyboard_name(p.name)):
                imgs.append(p)
            elif ext in (".mp4", ".mov", ".webm"):
                if p.parent.name == "renders" or "final" in p.name.lower():
                    if p not in renders:
                        renders.append(p)
                elif p not in vids:
                    vids.append(p)

        stills = [n for n in (_copy(p) for p in imgs) if n and not is_storyboard_name(n)]
        clips = [n for n in (_copy(p) for p in vids) if n]
        final = None
        if renders:
            pref = [p for p in renders if "final" in p.name.lower()] or renders
            final = _copy(pref[-1])

        # Surface structured TEXT artifacts INLINE for review at their gates so the human in Dify
        # actually sees the content: script (dialogue/sections at GATE 1 approve_script),
        # scene_plan (text plan at GATE 2), asset_manifest (media inventory at GATE 3/4). Prefer
        # the checkpoint's inline dict; fall back to the on-disk artifacts/<name>.json the skills
        # write. Without this, the script gate would surface only a gate label and no script body,
        # so the reviewer has nothing to approve and the flow appears to skip the human.
        for aname in ("script", "scene_plan", "asset_manifest"):
            val = artifacts.get(aname)
            if not isinstance(val, dict):
                ap = proj / "artifacts" / f"{aname}.json"
                try:
                    val = json.loads(ap.read_text(encoding="utf-8")) if ap.is_file() else None
                except (OSError, ValueError):
                    val = None
            if isinstance(val, dict):
                out[aname] = val
        # stills checkpoint often omits scene_plan; keep it for the storyboard join
        if "scene_plan" not in out:
            sp_cp = proj / "checkpoint_scene_plan.json"
            try:
                data = json.loads(sp_cp.read_text(encoding="utf-8")) if sp_cp.is_file() else {}
                val = (data.get("artifacts") or {}).get("scene_plan")
                if isinstance(val, dict) and val.get("scenes"):
                    out["scene_plan"] = val
            except (OSError, ValueError):
                pass
        # Fallback ONLY when there is no structured script (e.g. a script written as a markdown)
        # file): surface it as a downloadable link so it's still reviewable. Match ONLY a file
        # literally named script.* — never another stray .md (e.g. cost_report.md), which would
        # otherwise be mislabeled as the script at gates whose checkpoint carries no script
        # artifact (regression seen at the scene_plan gate: artifacts.script -> cost_report.md).
        if "script" not in out:
            script_md = [p for p in _scan(proj / "artifacts", (".md",))
                         if p.stem.lower() == "script"]
            if script_md:
                n = _copy(script_md[-1])
                if n:
                    out["script"] = n
        if stills:
            tmp = {**out, "stills": stills}
            out["stills"] = ordered_still_basenames(tmp) or stills
        if clips:
            out["clips"] = clips
        if final:
            out["final"] = final
            out["branded"] = False
        out["_checkpoint_artifacts"] = artifacts  # raw non-file data for Dify context
        # Dual-surface: keep inline JSON, also write .md copies (preview key is set in _sync
        # once the gate is known so Dify's file slot shows the current text gate only).
        _write_text_previews(job_id, out, gate=None)
        return out

    def _write_cost_report(self, job_id: str) -> None:
        """Build the per-project cost/time report (Higgsfield credits, ElevenLabs usage,
        generation time) and mirror it into the job store so it's available ON DEMAND via
        `GET /jobs/{id}/cost` and the downloadable `cost_report.md` artifact. It is deliberately
        NOT attached to the polled job state — cost is API-only, not injected into every gate
        response. Non-fatal: a report failure must never break a run."""
        try:
            from lib import cost_report as cr
            cr.write_report(job_id)
        except Exception:  # noqa: BLE001 — the report must never break a run
            return
        try:
            proj_art = self._projects_dir / job_id / "artifacts"
            for name in ("cost_report.md", "cost_report.json"):
                src = proj_art / name
                if src.is_file():
                    store.artifact_path(job_id, name).write_bytes(src.read_bytes())
        except OSError:
            pass

    # -- approvals + prompts ------------------------------------------------
    def _gate_stage(self, gate: Optional[str]) -> Optional[str]:
        if gate in ("approve_hero_still", "approve_stills", "approve_motion_sample",
                    "budget_exceeded", "approve_assets"):
            return "assets"                 # all are pauses of the single assets stage
        if gate == "approve_brand":
            return "brand"
        for s, g in _STAGE_GATE.items():
            if g == gate:
                return s
        return None

    def _approve_stage(self, job_id: str, stage: Optional[str],
                       pipeline_type: Optional[str] = None) -> None:
        if not stage:
            return
        from lib import checkpoint as cp
        pipe = pipeline_type or _DEFAULT_PIPELINE
        # Never complete assets on stills-only video media — that skips the stills/storyboard
        # gate and falsely unlocks edit. Carousel/image are stills-terminal and may complete.
        if stage == "assets" and pipe not in _STILLS_TERMINAL:
            existing = cp.read_checkpoint(self._projects_dir, job_id, stage) or {}
            arts = self._mirror_artifacts(job_id, existing.get("artifacts") or {})
            if _stills_only_media(arts):
                sys.stderr.write(
                    f"[dify_launcher] refusing to complete assets for {job_id}: stills-only "
                    "(no clips/final) — keep awaiting stills/storyboard gate\n")
                return
        existing = cp.read_checkpoint(self._projects_dir, job_id, stage) or {}
        cp.write_checkpoint(
            self._projects_dir, job_id, stage, "completed",
            existing.get("artifacts", {}), pipeline_type=pipe,
            human_approval_required=True, human_approved=True,
        )

    def _start_prompt(self, job_id: str, brief: str, options: Optional[dict[str, Any]] = None,
                      pipeline: str = "panda-video", *, language_coerced: bool = False) -> str:
        options = options or {}
        if pipeline == "panda-carousel":
            return self._carousel_start_prompt(job_id, brief, options,
                                               language_coerced=language_coerced)
        if pipeline == "panda-image":
            return self._image_start_prompt(job_id, brief, options,
                                            language_coerced=language_coerced)
        lang = str(options.get("language", "en")).lower()
        narrator = str(options.get("narrator", "panda")).lower()
        music = options.get("music", True)            # BGM: mood string, True (default bed), or False
        runtime = str(options.get("render_runtime", "auto")).lower()  # auto|ffmpeg|remotion|hyperframes
        motion_sample = str(options.get("motion_sample", False)).lower() \
            not in ("false", "0", "no", "off", "")    # one-clip motion cost gate (default off)
        hero_still = str(options.get("hero_still", True)).lower() \
            not in ("false", "0", "no", "off", "")
        cap = _budget_cap({"options": options})       # max_higgsfield_credits, or None (no cap)

        if cap is not None:
            budget_line = (
                f"BUDGET — HARD CAP of {cap} Higgsfield credits for this project. Before ANY "
                "Higgsfield generation (stills, motion sample, or clips), follow the BUDGET HARD RULE "
                "in skills/meta/higgsfield-mcp-bridge.md: sum the credits already recorded in "
                "asset_manifest PLUS the get_cost of the batch you are about to generate; if that "
                f"total would exceed {cap}, DO NOT call the generation tool — write the assets "
                "checkpoint status='awaiting_human' with partial_progress={\"phase\":\"budget_hold\"} "
                "(include the cap, spent, requested, projected credits) and STOP for a human decision.")
        else:
            budget_line = ("BUDGET — no credit cap set for this job (max_higgsfield_credits unset). "
                           "Still record each asset's get_cost credits in asset_manifest.")

        if runtime in ("ffmpeg", "remotion", "hyperframes"):
            runtime_line = (
                f"RENDER RUNTIME — the job requests render_runtime='{runtime}'. Set "
                f"edit_decisions.render_runtime='{runtime}' and route compose accordingly "
                "(ffmpeg->panda_render, remotion/hyperframes->video_compose). If that runtime "
                "is unavailable on this machine, STOP and escalate — do NOT silently fall back.")
        else:
            runtime_line = (
                "RENDER RUNTIME — 'auto': choose render_runtime per the decision matrix in "
                "skills/pipelines/panda-video/compose-director.md AND the actual on-box "
                "availability (check via video_compose). Default to 'ffmpeg' (panda_render) for "
                "character-mascot clips; pick 'remotion'/'hyperframes' only when the brief needs "
                "React/HTML motion graphics. Record the choice in edit_decisions.render_runtime "
                "and log a render_runtime_selection decision.")

        voice_line = _voice_line(options)
        scale_line = _pair_scale_lock_line()
        lipsync_line = _audio_lipsync_line(options)
        lang_note = _language_lock_note(options, coerced=language_coerced)

        if music is False or str(music).lower() in ("false", "none", "no", "off"):
            music_line = "MUSIC — do NOT add a background music bed for this job."
        else:
            mood = "" if music is True else f" Mood/brief: {music}."
            music_line = ("MUSIC — add a background music bed via the `music_gen` tool "
                          f"(ElevenLabs Music, same ELEVENLABS_API_KEY).{mood} Keep it under the VO.")

        return (
            f"Run the `{pipeline}` pipeline to produce a video.\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    narrator: {narrator}\n\n"
            "BRAND — MANDATORY, do NOT improvise: read config/panda-elements.json and USE its "
            "Higgsfield reference Element IDs — customer "
            "`089ddcec-c375-4299-8a65-6d8b757dd81a`, panda "
            "`4c01c8f9-6cfb-4d8c-9eb9-74cb61462103`. Any human/traveller/customer/person OR "
            "panda/mascot mention maps to those IDs (see phrase_aliases). ATTACH Elements as "
            "MCP media / image_references — never put UUIDs in the prompt sentence, never invent "
            "a new character. Look: styles/panda.yaml — default medium is 2D flat illustration "
            "(same medium for people, mascot, and set). Max 2 paid generate_image calls per "
            "scene; take 2 = i2i of take 1; then STOP and gate. Full rules: "
            "skills/meta/higgsfield-mcp-bridge.md (CHARACTER LOCK, STILLS 2-TAKE HARD RULE, "
            "2D MEDIUM LOCK).\n"
            f"{scale_line}\n{voice_line}\n{lipsync_line}{lang_note}{music_line}\n"
            f"{runtime_line}\n{budget_line}\n\n"
            "Follow pipeline_defs/panda-video.yaml (it names each stage's director skill) "
            "and skills/meta/checkpoint-protocol.md. Execute stages in "
            "order. At every stage whose manifest sets human_approval_default: true, write the "
            "checkpoint with status='awaiting_human' and STOP (end your turn) — do NOT "
            "self-approve.\n"
            "PIPELINE SHAPE: the `scene_plan` stage produces ONLY a structured TEXT plan — NO "
            "media, NO generation tools. The `assets` stage then runs in human-reviewed phases "
            "(cost gates):\n"
            + self._assets_phases_text(motion_sample, hero_still=hero_still,
                                       audio_lipsync=_audio_lipsync_enabled(options)) +
            "Finally compose the approved assets with the `panda_render` tool. Stop at the first gate."
        )

    def _carousel_start_prompt(self, job_id: str, brief: str, options: dict[str, Any],
                               *, language_coerced: bool = False) -> str:
        lang = str(options.get("language", "en")).lower()
        ratio = _carousel_aspect(options)
        cap = _budget_cap({"options": options})
        auto_script = not _script_gate_enabled({"options": options})
        hero_still = str(options.get("hero_still", True)).lower() \
            not in ("false", "0", "no", "off", "")
        lang_note = _language_lock_note(options, coerced=language_coerced)
        if cap is not None:
            budget_line = (
                f"BUDGET — HARD CAP of {cap} Higgsfield credits. Before ANY Higgsfield still "
                "generation, follow the BUDGET HARD RULE in skills/meta/higgsfield-mcp-bridge.md. "
                f"If spent + get_cost would exceed {cap}, write the assets checkpoint "
                "status='awaiting_human' with partial_progress={{\"phase\":\"budget_hold\"}} and STOP.")
        else:
            budget_line = ("BUDGET — no credit cap set. Still record each still's get_cost credits "
                           "in asset_manifest.")
        script_line = (
            "SCRIPT GATE — this job auto-approves script: write the script checkpoint "
            "status='completed' with human_approved=True, log decision_log category="
            "'approval_policy' (auto-approved by job option), and continue to scene_plan in "
            "the SAME turn."
            if auto_script else
            "SCRIPT GATE — write script status='awaiting_human' and STOP. Do NOT self-approve."
        )
        if hero_still:
            assets_shape = (
                "  - assets PHASE 0 (hero look-lock): generate ONLY ONE hero still "
                "(hero_moment scene, else scene 1). Checkpoint status='awaiting_human' AND "
                "top-level partial_progress={\"phase\":\"hero_still\",\"hero_scene_id\":\"…\","
                "\"look_notes\":[]} and STOP. Do NOT generate other stills yet.\n"
                "  - assets PHASE 1 (stills): after hero is approved, KEEP the hero PNG; generate "
                "REMAINING stills under LOOK LOCK (import hero as style ref once + look_notes). "
                "Preflight all remaining take-1 stills and enforce the complete-batch budget, "
                "then submit with max 4 Higgsfield jobs in flight (2 after a 429), poll the set "
                "together, then run take 2 only for unusable take-1 results. Write "
                "asset_manifest with credits. Checkpoint status='awaiting_human' AND top-level "
                "partial_progress={\"phase\":\"stills\"} and STOP.\n"
            )
            shape_line = (
                "PIPELINE SHAPE: idea (internal, no gate) → script (GATE 1) → scene_plan TEXT "
                "(GATE 2) → hero still (GATE 2.5) → assets STILLS (GATE 3) → DONE.\n"
            )
        else:
            assets_shape = (
                "  - assets: generate ONLY stills via Higgsfield generate_image at that aspect ratio "
                "(max 2 paid calls per slide). Preflight all take-1 stills and enforce the "
                "complete-batch budget, then submit with max 4 jobs in flight (2 after a 429), "
                "poll together, then take 2 = i2i only for unusable take 1. Bake primary-language copy into each "
                "still. Write asset_manifest (images only) with per-still credits and Element IDs. "
                "Checkpoint status='awaiting_human' AND "
                "partial_progress={\"phase\":\"stills\"} and STOP.\n"
            )
            shape_line = (
                "PIPELINE SHAPE: idea (internal, no gate) → script (GATE 1) → scene_plan TEXT "
                "(GATE 2) → assets STILLS ONLY (GATE 3) → DONE.\n"
            )
        return (
            f"Run the `panda-carousel` pipeline to produce a STILLS-ONLY social carousel "
            f"(NOT a video).\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    aspect_ratio: {ratio}\n\n"
            "BRAND — MANDATORY: read config/panda-elements.json and USE its Higgsfield reference "
            "Element IDs — customer `089ddcec-c375-4299-8a65-6d8b757dd81a`, panda "
            "`4c01c8f9-6cfb-4d8c-9eb9-74cb61462103`. Any human/traveller/customer OR panda/mascot "
            "mention maps to those IDs (phrase_aliases). ATTACH as MCP media — never UUID in "
            "prompt, never invent a new character. Look: styles/panda.yaml — default 2D flat. "
            "Max 2 paid generate_image per slide; take 2 = i2i of take 1; then STOP. See "
            "skills/meta/higgsfield-mcp-bridge.md (CHARACTER LOCK, STILLS 2-TAKE, 2D MEDIUM).\n"
            f"{lang_note}{budget_line}\n{script_line}\n\n"
            "Follow pipeline_defs/panda-carousel.yaml and "
            "skills/pipelines/panda-carousel/*-director.md. Execute stages in order.\n"
            f"{shape_line}"
            "  - scene_plan: one scene per slide, bilingual captions.zh/en, required_assets are "
            "images only (exactly one image per slide). Set metadata.aspect_ratio to the job option "
            f"'{ratio}' (caller-set; default 4:5). Pass that same ratio to generate_image. "
            "Name locked Element IDs; plan 2D flat.\n"
            f"{assets_shape}"
            "Do NOT generate motion clips, TTS, music, edit_decisions, or a compose/render. "
            "Do NOT brand the stills (no wordmark overlay) — branding is a later POST /brand. "
            "Stop at the first human_approval gate."
        )

    def _image_start_prompt(self, job_id: str, brief: str, options: dict[str, Any],
                            *, language_coerced: bool = False) -> str:
        lang = str(options.get("language", "en")).lower()
        ratio = _stills_aspect(options, pipeline="panda-image")
        cap = _budget_cap({"options": options})
        lang_note = _language_lock_note(options, coerced=language_coerced)
        if cap is not None:
            budget_line = (
                f"BUDGET — HARD CAP of {cap} Higgsfield credits. Before ANY Higgsfield still "
                "generation, follow the BUDGET HARD RULE in skills/meta/higgsfield-mcp-bridge.md. "
                f"If spent + get_cost would exceed {cap}, write the assets checkpoint "
                "status='awaiting_human' with partial_progress={{\"phase\":\"budget_hold\"}} and STOP.")
        else:
            budget_line = ("BUDGET — no credit cap set. Still record the still's get_cost credits "
                           "in asset_manifest.")
        return (
            f"Run the `panda-image` pipeline to produce ONE STILLS-ONLY social image "
            f"(NOT a video, NOT a carousel).\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    aspect_ratio: {ratio}\n\n"
            "BRAND — MANDATORY: read config/panda-elements.json and USE its Higgsfield reference "
            "Element IDs — customer `089ddcec-c375-4299-8a65-6d8b757dd81a`, panda "
            "`4c01c8f9-6cfb-4d8c-9eb9-74cb61462103`. Any human/traveller/customer OR panda/mascot "
            "mention maps to those IDs (phrase_aliases). ATTACH as MCP media — never UUID in "
            "prompt, never invent a new character. Look: styles/panda.yaml — default 2D flat. "
            "Max 2 paid generate_image; take 2 = i2i of take 1; then STOP. See "
            "skills/meta/higgsfield-mcp-bridge.md (CHARACTER LOCK, STILLS 2-TAKE, 2D MEDIUM).\n"
            f"{lang_note}{budget_line}\n\n"
            "Follow pipeline_defs/panda-image.yaml and "
            "skills/pipelines/panda-image/*-director.md. Execute stages in order.\n"
            "PIPELINE SHAPE: idea (internal, no gate) → scene_plan TEXT (GATE 1) → "
            "assets ONE STILL (GATE 2) → DONE. There is NO script stage.\n"
            "  - scene_plan: exactly ONE scene, bilingual captions.zh/en, required_assets is "
            "one image only. Set metadata.aspect_ratio to the job option "
            f"'{ratio}' (caller-set; default 1:1). Pass that same ratio to generate_image. "
            "Name locked Element IDs; plan 2D flat.\n"
            "  - assets: generate ONE still via Higgsfield generate_image at that aspect ratio "
            "(max 2 paid calls; take 2 = i2i). Bake primary-language copy into the still. Write "
            "asset_manifest (images only) with credits and Element IDs. Checkpoint "
            "status='awaiting_human' AND "
            "partial_progress={\"phase\":\"stills\"} and STOP.\n"
            "Do NOT generate motion clips, TTS, music, edit_decisions, or a compose/render. "
            "Do NOT brand the still (no wordmark overlay) — branding is a later POST /brand. "
            "Stop at the first human_approval gate."
        )

    def _assets_phases_text(self, motion_sample: bool, *, hero_still: bool = True,
                            audio_lipsync: bool = True) -> str:
        scale_lock = _pair_scale_lock_line()
        if hero_still:
            hero = (
                "  PHASE 0 (hero still look-lock): generate ONLY ONE hero still "
                "(scene with hero_moment, else scene 1) via the Higgsfield MCP bridge. "
                f"CHARACTER LOCK + 2D MEDIUM + STILLS 2-TAKE on that one still. {scale_lock} "
                "Write the assets "
                "checkpoint status='awaiting_human' AND top-level "
                "partial_progress={\"phase\":\"hero_still\",\"hero_scene_id\":\"…\","
                "\"look_notes\":[]} (NOT nested under asset_manifest.metadata) and STOP. "
                "Do NOT generate other stills or any video yet.\n"
            )
            stills = (
                "  PHASE 1 (stills): only after the hero is approved, KEEP the approved hero PNG. "
                "Generate REMAINING scene stills under LOOK LOCK (media_import hero as style/look "
                "reference ONCE — not a start-frame that copies composition; reuse that media id; "
                f"bake look_notes into every prompt). {scale_lock} "
                "Preflight all remaining take-1 stills and "
                "enforce the complete-batch budget, then submit (max 4 Higgsfield jobs in flight; "
                "reduce to 2 after a 429), poll the set together; do not serialize. Run take 2 "
                "only for unusable take-1 results. Same CHARACTER LOCK + 2D MEDIUM + STILLS "
                "2-TAKE per remaining scene. "
                "Record all stills (incl. hero) in asset_manifest with credits. Write the assets "
                "checkpoint status='awaiting_human' AND top-level "
                "partial_progress={\"phase\":\"stills\"} and STOP. Do NOT generate any video yet. "
                "Do NOT mark assets completed yet.\n"
            )
        else:
            hero = ""
            stills = (
                "  PHASE 1 (stills): generate ONLY the stills — one per scene — via the Higgsfield "
                "MCP bridge (skills/meta/higgsfield-mcp-bridge.md). CHARACTER LOCK: attach "
                "customer/panda Element IDs as media (never invent, never UUID-in-prompt). "
                f"2D MEDIUM LOCK: styles/panda.yaml flat illustration. {scale_lock} "
                "STILLS 2-TAKE HARD RULE: "
                "preflight all take-1 stills and enforce the complete-batch budget, then submit "
                "(max 4 Higgsfield jobs in flight; 2 after a 429), poll the set together, then "
                "take 2 = i2i only for unusable take 1; max 2 paid generate_image per scene. "
                "Then write the assets "
                "checkpoint with status='awaiting_human' AND partial_progress={\"phase\":\"stills\"} "
                "and STOP. Do NOT generate any video yet.\n")
        if audio_lipsync:
            motion_how = (
                "For customer/panda speaking clips: seedance_2_0 with start_image + "
                "audio_references=VO, generate_audio:false, lip-sync mouth to audio (no mouth "
                "HOLD). Narrator/text_card: HOLD or static. Fallback HOLD on failure."
            )
            hold_note = "AUDIO LIPSYNC path (see AUDIO LIPSYNC line)"
            qa_how = (
                "Then run mandatory lipsync_qa with the exact VO, allow only one local timing "
                "correction or one paid failed-scene regeneration, retain both takes, and persist "
                "unresolved warnings; never attempt a third take. "
            )
        else:
            motion_how = (
                "image_to_video with HOLD LOCK (mouth frozen); duration from measured VO."
            )
            hold_note = "HOLD LOCK 2D + locked Elements"
            qa_how = ""
        if motion_sample:
            return hero + stills + (
                "  PHASE 2 (motion sample): only after the stills are approved, for the HERO "
                "scene: if it is narrated, TTS-FIRST (ElevenLabs per section + audio_probe) then "
                "snap i2v duration via lib/i2v_duration.snap_i2v_duration (models_explore allowed "
                f"list), THEN animate that ONE still ({motion_how}) so the motion/animation can be "
                "approved before the full batch. "
                f"{hold_note}. Write the assets checkpoint status='awaiting_human' AND "
                "partial_progress={\"phase\":\"motion_sample\"} and STOP — no other clips yet "
                "(sample-scene VO may already exist).\n"
                "  PHASE 3 (media): only after the motion sample is approved, TTS-FIRST for all "
                "remaining speaking sections, probe ALL VO, then call "
                "lib/i2v_duration.allocate_scene_durations once for the full timeline "
                "(requested total ±5%; unequal scene lengths; approved sample duration fixed), "
                "THEN animate the "
                f"REMAINING stills ({motion_how}). Preflight all pending clips and enforce the "
                "complete-batch budget, then submit as waves (max 4 jobs in flight; 2 after a 429), "
                "checkpoint scene_id→job_id immediately, poll the set together, and start music "
                "(ElevenLabs) while i2v jobs are in flight. Record everything "
                "(incl. the sample) in asset_manifest with per-asset Higgsfield credits "
                f"and metadata.timeline_contract (keep vo_duration_map for compatibility). {qa_how}Then write the assets checkpoint "
                "status='awaiting_human' (no phase marker) and STOP.\n")
        return hero + stills + (
            "  PHASE 3 (media): only after the stills are approved, TTS-FIRST (ElevenLabs per "
            "speaking section + audio_probe for ALL VO), then call "
            "lib/i2v_duration.allocate_scene_durations once for the full timeline (requested "
            "total ±5%; unequal audio-driven scene lengths), THEN animate approved stills "
            f"({motion_how}; duration from the allocation). Preflight all pending clips and enforce "
            "the complete-batch budget, then submit as waves (max 4 jobs in flight; 2 after "
            "a 429), checkpoint scene_id→job_id immediately, poll the set together, and start "
            "music while i2v is in flight. Record everything in "
            f"asset_manifest (incl. metadata.timeline_contract; keep vo_duration_map for compatibility). {qa_how}Then write the assets checkpoint "
            "status='awaiting_human' (no 'stills' phase marker) and STOP.\n")

    def _hero_approved_prompt(self, job_id: str, state: Optional[dict[str, Any]] = None) -> str:
        look_notes = list((state or {}).get("look_notes") or [])
        notes_txt = ""
        if look_notes:
            notes_txt = " Accumulated look_notes from hero revises: " + json.dumps(look_notes) + "."
        hero_sid = ((state or {}).get("artifacts") or {}).get("hero_scene_id") or ""
        sid_txt = f" hero_scene_id={hero_sid}." if hero_sid else ""
        return (
            f"For project_id: {job_id}, the HERO STILL look-lock phase of the `assets` stage is "
            "APPROVED. Do NOT mark the assets stage completed yet, and do NOT generate video. "
            "KEEP the approved hero PNG on disk."
            f"{sid_txt}{notes_txt} "
            "Do NOT stop to ask clarifying questions — apply pipeline defaults and generate. "
            "For panda-carousel, bake scene_plan captions into stills (asset-director); if "
            "look_notes conflict with the brief on baked text, follow the brief/scene_plan. "
            "Generate the REMAINING scene stills under LOOK LOCK "
            "(skills/meta/higgsfield-mcp-bridge.md): media_import the hero PNG as a style/look "
            "reference (confirm the live media role with models_explore — do NOT pass it as a "
            "start-frame that copies composition onto every scene). Match palette, character "
            "rendering, lighting, medium, and wardrobe from the hero; use each remaining scene's "
            "action/framing from scene_plan; bake look_notes into every remaining prompt. Reuse "
            "the one imported hero style id; preflight all remaining take-1 stills and enforce "
            "the complete-batch budget, then submit (max 4 Higgsfield jobs in flight; reduce to "
            "2 after a 429), poll together; do not serialize. Take 2 only for unusable take-1 "
            "results. Same CHARACTER LOCK + 2D MEDIUM + STILLS 2-TAKE per remaining scene. Record all stills "
            "(incl. the approved hero) in asset_manifest with credits. Then rewrite the assets "
            "checkpoint with status='awaiting_human' AND top-level "
            "partial_progress={\"phase\":\"stills\"} (NOT nested under asset_manifest.metadata) "
            "and STOP for full storyboard approval. Do NOT mark assets completed yet. "
            f"Do NOT leave phase=hero_still — that would wrongly re-open the hero gate. "
            f"{_pair_scale_lock_line()}"
        )

    def _continue_prompt(self, job_id: str, pipeline: Optional[str] = None) -> str:
        p = pipeline or _DEFAULT_PIPELINE
        extra = ""
        if p == "panda-carousel":
            extra = (" This is a stills-only carousel — do NOT generate video, TTS, music, or "
                     "compose. After stills the pipeline is complete.")
        elif p == "panda-image":
            extra = (" This is a SINGLE still — do NOT generate video, TTS, music, or compose. "
                     "There is no script stage. After the one still the pipeline is complete.")
        return (
            f"Continue the `{p}` pipeline for project_id: {job_id}. Read the latest "
            "checkpoint, proceed from the next stage, and STOP at the next human_approval gate "
            f"(status='awaiting_human', end your turn). If the pipeline is complete, finish.{extra}"
        )

    def _assets_approved_prompt(self, job_id: str, pipeline: Optional[str] = None) -> str:
        """Prompt after GATE 4 (approve_assets): run ungated edit then compose → approve_final."""
        p = pipeline or _DEFAULT_PIPELINE
        return (
            f"For project_id: {job_id}, the FULL MEDIA phase of the `assets` stage "
            f"(GATE 4 / approve_assets) is APPROVED on the `{p}` pipeline. "
            "Do NOT stop to ask the human a question. Do NOT invent a gate. "
            "`edit` is ungated (human_approval_default:false) — the next human pause is "
            "compose's approve_final only.\n\n"
            "1. Read skills/pipelines/panda-video/edit-director.md and "
            "skills/meta/checkpoint-protocol.md. Build edit_decisions from scene_plan + "
            "asset_manifest (cut points, caption bands, render_runtime carried UNCHANGED). "
            "Mute/discard the native AAC track baked into Higgsfield/Kling i2v clips before "
            "mixing narration+music. Pre-conform off-spec 1076x1928 clips to 1080x1920 via "
            "an all-top crop (not a centred cover-crop).\n"
            "2. Use asset_manifest.metadata.timeline_contract as the effective timeline. Scene "
            "lengths may be unequal, but the final must remain within ±5% of the requested total. "
            "Record each cut's source_duration_seconds, effective_duration_seconds, and bounded "
            "post-speech tail_hold_seconds. Reject pacing_revision_required unless the human "
            "explicitly approved a logged duration exception. Do NOT shorten locked copy, retime "
            "lip-synced motion, or let a hold cover active speech.\n"
            "3. Read asset_manifest.metadata.lip_sync_qa. Apply only offsets whose local re-check "
            "passed: derive the signed delta from attempt-1 expected offset and recompute affected "
            "VO start_seconds from effective_scene_start + immutable original scene-local offset. "
            "Never shift picture and VO independently or apply unresolved offsets.\n"
            "4. Write the edit checkpoint status='completed' (ungated), then immediately "
            "run compose per skills/pipelines/panda-video/compose-director.md "
            "(panda_render for ffmpeg / render_runtime already locked). Verify final.mp4, "
            "write render_report + final_review. Carry unresolved lip-sync scene ids into "
            "final_review.checks.lip_sync_check and set both its and final_review's top-level "
            "recommended_action='present_to_user'; do not retry again or block. Checkpoint "
            "compose status='awaiting_human' for "
            "approve_final, and END YOUR TURN.\n"
            "Stdout questions are invisible to Dify — ending without an awaiting_human "
            "checkpoint leaves the job stuck."
        )

    def _edit_compose_continue_prompt(self, job_id: str,
                                      pipeline: Optional[str] = None) -> str:
        """Re-nudge after a continue leg that asked/exited without reaching approve_final."""
        p = pipeline or _DEFAULT_PIPELINE
        return (
            f"For project_id: {job_id} (`{p}`), clip/media approval ALREADY happened. You "
            "previously stopped without reaching compose's approve_final gate. Do NOT ask "
            "the human a question — decide and proceed. "
            "If edit_decisions is missing, write it now from metadata.timeline_contract "
            "(unequal audio-driven scene lengths; requested total ±5%; bounded post-speech holds; "
            "apply only locally validated lip-sync offsets from immutable scene-local timestamps; "
            "mute native clip audio; all-top crop). Then compose "
            "to final.mp4, carry unresolved lip-sync scene warnings into final_review with "
            "recommended_action='present_to_user', and checkpoint compose status='awaiting_human'. "
            "Do NOT retry lip-sync again and do NOT leave status running with no gate."
        )

    def _stills_approved_prompt(self, job_id: str,
                                options: Optional[dict[str, Any]] = None) -> str:
        return (
            f"For project_id: {job_id}, the STILLS phase of the `assets` stage is APPROVED. Do NOT "
            "mark the assets stage completed yet. TTS-FIRST for every speaking script section "
            "(ElevenLabs + VOICE CAST), probe ALL durations (audio_probe), then call "
            "lib/i2v_duration.allocate_scene_durations once for the full timeline using the "
            "requested total, tolerance_fraction=0.05, scene-plan weights, scene-local audio "
            "bounds, and models_explore allowed durations. THEN animate the approved stills with "
            "each allocated i2v_duration per the AUDIO LIPSYNC line below "
            "(seedance_2_0 + audio_references for customer/panda when on; else HOLD LOCK). "
            "Preflight all pending clips and enforce the complete-batch budget before any submit; "
            "submit max 4 Higgsfield jobs in flight (2 after a 429), checkpoint every "
            "scene_id→job_id immediately, and poll the set together instead of serializing. "
            "Start music while i2v jobs are in flight. Record every file in asset_manifest including "
            "metadata.timeline_contract and legacy vo_duration_map (plus audio_lipsync on eligible "
            "clips), then rewrite the "
            "assets checkpoint with status='awaiting_human' (WITHOUT the 'stills' phase marker) "
            "and STOP for the full media approval.\n\n"
            + _pair_scale_lock_line() + "\n"
            + _voice_line(options or {}) + "\n" + _audio_lipsync_line(options)
            + _lip_sync_qa_line(options)
        )

    def _budget_raised_prompt(self, job_id: str, new_cap: Any) -> str:
        cap_txt = f" The approved cap is now {new_cap} Higgsfield credits." if new_cap is not None else ""
        return (
            f"For project_id: {job_id}, the BUDGET HOLD is cleared — the human authorized continuing."
            f"{cap_txt} Resume the Higgsfield generation that was blocked, RE-CHECKING the budget "
            "hard-rule (spent + get_cost vs the cap) before generating. If it now fits, generate the "
            "batch, record each asset's credits in asset_manifest, and stop at the next assets gate. "
            "If it STILL exceeds the cap, do NOT generate — write the budget_hold checkpoint again."
        )

    def _motion_sample_prompt(self, job_id: str,
                              options: Optional[dict[str, Any]] = None) -> str:
        return (
            f"For project_id: {job_id}, the STILLS phase of the `assets` stage is APPROVED. Do NOT "
            "mark the assets stage completed yet, and do NOT batch-generate all clips. For the "
            "HERO scene: if narrated, TTS-FIRST (ElevenLabs + audio_probe + snap_i2v_duration), "
            "THEN animate that ONE still into a single MOTION SAMPLE clip per the AUDIO LIPSYNC "
            "line below (snapped duration). Record the sample's Higgsfield credits on that asset "
            "(credits + credits_source='actual'). Then rewrite the assets checkpoint with "
            "status='awaiting_human' AND partial_progress={\"phase\":\"motion_sample\"} and STOP. "
            "Generate NO other clips yet (sample-scene VO may remain on disk for PHASE 3).\n\n"
            + _pair_scale_lock_line() + "\n" + _audio_lipsync_line(options)
        )

    def _motion_approved_prompt(self, job_id: str,
                                options: Optional[dict[str, Any]] = None) -> str:
        return (
            f"For project_id: {job_id}, the MOTION SAMPLE is APPROVED. Do NOT mark the assets stage "
            "completed yet. TTS-FIRST for remaining speaking sections (reuse sample-scene VO), "
            "probe ALL VO, then call allocate_scene_durations once for the full requested timeline "
            "(±5%; unequal scene lengths; fixed_i2v_duration for the approved sample), THEN animate "
            "the REMAINING approved stills with their allocated durations per "
            "the AUDIO LIPSYNC line below (reuse the approved sample's approach when it matches; "
            "lipsync shots stay on seedance_2_0 + audio_references). Preflight all pending clips "
            "and enforce the complete-batch budget before any submit; submit max 4 Higgsfield "
            "jobs in flight (2 after a 429), checkpoint every scene_id→job_id immediately, and "
            "poll the set together instead of serializing; start music while i2v is in flight. "
            "Record every file (incl. the already-approved sample) in asset_manifest with "
            "per-asset Higgsfield credits, metadata.timeline_contract, and legacy vo_duration_map, "
            "then rewrite the assets "
            "checkpoint with status='awaiting_human' (WITHOUT any phase marker) and STOP for the "
            "full media approval.\n\n"
            + _pair_scale_lock_line() + "\n"
            + _voice_line(options or {}) + "\n" + _audio_lipsync_line(options)
            + _lip_sync_qa_line(options)
        )

    def _revise_prompt(self, job_id: str, stage: Optional[str], response: dict[str, Any],
                       state: Optional[dict[str, Any]] = None) -> str:
        note = response.get("answer", "(no note)")
        shots = response.get("shots") or []
        shot_txt = f" Regenerate only shots {shots}." if shots else ""
        extra = ""
        gate = (state or {}).get("gate")
        is_hero = (gate == "approve_hero_still"
                   or "HERO STILL" in str(stage or "").upper())
        is_stills = (gate == "approve_stills"
                     or ("STILLS" in str(stage or "").upper() and not is_hero))
        if is_hero:
            mode = _stills_revise_mode(response)
            mode_label = "EDIT" if mode == "edit" else "FRESH"
            paths = _still_abs_paths(
                job_id, state, shots or [1], getattr(self, "_projects_dir", None))
            path_txt = (f" Current hero still absolute path(s): {paths}." if paths else
                        f" Current hero still lives under projects/{job_id}/assets/images/ "
                        "and the launcher artifacts dir.")
            look_notes = list((state or {}).get("look_notes") or [])
            if note and note != "(no note)":
                look_notes = look_notes + [note]
            notes_txt = f" Append to look_notes (full list after this revise): {json.dumps(look_notes)}."
            if mode == "edit":
                extra = (
                    f" MODE={mode_label}.{path_txt} EDIT the ONE hero look-lock still: load it "
                    "from disk, media_import locally, generate_image with that media_id plus a "
                    "preservation prompt (keep composition/character/layout; apply only the "
                    "feedback). Keep Element IDs. Same aspect ratio."
                    f"{notes_txt}"
                )
            else:
                extra = (
                    f" MODE={mode_label}.{path_txt} FRESH: generate_image the ONE hero still from "
                    "text + panda/customer Element IDs only. Do NOT pass the old PNG."
                    f"{notes_txt}"
                )
            extra += (
                " Rewrite the assets checkpoint with status='awaiting_human' AND top-level "
                "partial_progress={\"phase\":\"hero_still\",\"hero_scene_id\":"
                "<same id>,\"look_notes\":<updated list>} (not nested under metadata) and STOP. "
                "Do NOT generate remaining storyboard stills or video."
            )
        elif is_stills:
            mode = _stills_revise_mode(response)
            mode_label = "EDIT" if mode == "edit" else "FRESH"
            paths = _still_abs_paths(
                job_id, state, shots, getattr(self, "_projects_dir", None))
            path_txt = (f" Current still absolute paths: {paths}." if paths else
                        f" Current stills live under projects/{job_id}/assets/images/ "
                        "and the launcher artifacts dir.")
            shot_txt = (f" Flagged shots (1-based): {shots}." if shots else
                        " All current stills.")
            if mode == "edit":
                extra = (
                    f" MODE={mode_label}.{path_txt} EDIT: load each flagged still from disk, "
                    "register it with Higgsfield MCP via local upload / media_import "
                    "(media_import_url cannot fetch localhost artifact URLs), confirm the image "
                    "model's start/reference media role with models_explore, then generate_image "
                    "with that media_id plus a preservation prompt: keep composition, character, "
                    "layout, and typography; apply only the feedback. Keep Element IDs. Same "
                    "aspect ratio. Replace only those files and their asset_manifest rows "
                    "(new credits / job_id). Leave other slides untouched. If the image model "
                    "rejects a source still, surface a blocker — do NOT silently switch to FRESH."
                )
            else:
                extra = (
                    f" MODE={mode_label}.{path_txt} FRESH: generate_image from text + "
                    "panda/customer Element IDs only. Do NOT pass the old PNG. Replace only "
                    "the flagged files and their asset_manifest rows. Leave other slides untouched."
                )
            extra += (
                " Rewrite the assets checkpoint with status='awaiting_human' AND top-level "
                "partial_progress={\"phase\":\"stills\"} (not nested under metadata) and STOP. "
                "Do NOT generate video."
            )
        if is_hero or is_stills:
            extra += " " + _pair_scale_lock_line()
        return (
            f"Revise stage '{stage}' for project_id: {job_id} per this feedback: {note}.{shot_txt}"
            f"{extra} "
            "Rewrite that stage's checkpoint with status='awaiting_human' and STOP for approval."
        )


def get_runner(name: str) -> Runner:
    return {"mock": MockRunner, "claude": ClaudeCodeRunner}.get(name, MockRunner)()
