"""Per-project cost & time report.

Aggregates, for a single job/project, the consumption that actually costs money or
time, in each platform's OWN native units (no cross-platform USD roll-up):

  - Higgsfield  -> credits   (real, from the agent's get_cost preflight, recorded per
                              asset in asset_manifest.json as `credits`/`credits_source`)
  - ElevenLabs  -> characters (TTS) / seconds (music) — real, from the tool `usage`
                              events in the project's events.jsonl
  - Generation  -> seconds   per stage + total, from timing.jsonl (written by the
    time                      launcher runner around each `claude -p` leg)

Writes two artifacts into the project's `artifacts/` dir:
  - cost_report.json  (machine-readable summary)
  - cost_report.md    (human-readable table)

Design: never raises for missing/partial data — a fresh job with no generation yet
produces a valid report with zeros. Reads are tolerant of malformed lines.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.events import read_events
from lib.paths import PROJECTS_DIR


def _hms(seconds: float) -> str:
    """Human duration, e.g. 1716 -> '28m 36s', 42 -> '42s', 3720 -> '1h 2m 0s'."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, ValueError):
        return None


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not p.is_file():
        return out
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _higgsfield_from_manifest(proj: Path) -> dict[str, Any]:
    """Native Higgsfield credits, summed from asset_manifest.json per-asset `credits`."""
    manifest = _read_json(proj / "artifacts" / "asset_manifest.json")
    items: list[dict[str, Any]] = []
    total = actual = estimated = 0.0
    for a in (manifest or {}).get("assets", []) if isinstance(manifest, dict) else []:
        if not isinstance(a, dict):
            continue
        credits = a.get("credits")
        if not isinstance(credits, (int, float)):
            continue
        src = a.get("credits_source") or "estimated"
        items.append({
            "id": a.get("id"), "scene_id": a.get("scene_id"), "type": a.get("type"),
            "model": a.get("model") or a.get("source_tool"),
            "credits": credits, "source": src,
        })
        total += credits
        if src == "actual":
            actual += credits
        else:
            estimated += credits
    return {
        "unit": "credits", "total": round(total, 4),
        "actual": round(actual, 4), "estimated": round(estimated, 4),
        "count": len(items), "items": items,
    }


def _elevenlabs_from_events(proj: Path) -> dict[str, dict[str, Any]]:
    """Native ElevenLabs usage (characters / seconds), summed from events.jsonl `usage`."""
    buckets: dict[str, dict[str, Any]] = {}
    for ev in read_events(proj):
        if ev.get("event") != "finish":
            continue
        usage = ev.get("usage")
        if not isinstance(usage, dict):
            continue
        platform = usage.get("platform")
        amount = usage.get("amount")
        if not platform or not isinstance(amount, (int, float)):
            continue
        b = buckets.setdefault(platform, {
            "unit": usage.get("unit", ""), "total": 0.0, "calls": 0,
            "source": usage.get("source", "actual"),
        })
        b["total"] = round(b["total"] + amount, 4)
        b["calls"] += 1
    return buckets


def _timing(proj: Path) -> dict[str, Any]:
    """Per-stage active generation seconds + total, from timing.jsonl."""
    rows = _read_jsonl(proj / "artifacts" / "timing.jsonl")
    order: list[str] = []
    by_stage: dict[str, float] = {}
    for r in rows:
        stage = r.get("stage") or "unknown"
        secs = r.get("seconds")
        if not isinstance(secs, (int, float)):
            continue
        if stage not in by_stage:
            order.append(stage)
        by_stage[stage] = round(by_stage.get(stage, 0.0) + secs, 2)
    total = round(sum(by_stage.values()), 2)
    return {
        "stages": [{"stage": s, "seconds": by_stage[s], "human": _hms(by_stage[s])} for s in order],
        "total_active_seconds": total,
        "total_active_human": _hms(total),
    }


def build_summary(job_id: str) -> dict[str, Any]:
    """Assemble the per-project cost/time summary (native units, never raises)."""
    proj = PROJECTS_DIR / job_id
    platforms: dict[str, Any] = {"higgsfield": _higgsfield_from_manifest(proj)}
    platforms.update(_elevenlabs_from_events(proj))
    return {
        "job_id": job_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "units": "native (per-platform); no cross-platform USD total by design",
        "time": _timing(proj),
        "platforms": platforms,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    """Human-readable report. Native units per platform, time broken down per stage."""
    L: list[str] = []
    L.append(f"# Cost & Time Report — {summary.get('job_id', '')}")
    L.append("")
    L.append(f"_Generated {summary.get('generated_at', '')}. "
             f"Native units per platform — no cross-platform total by design._")
    L.append("")

    # --- generation time ---
    t = summary.get("time", {})
    L.append("## Generation time (active, excludes human review waits)")
    L.append("")
    L.append("| Stage | Time |")
    L.append("|---|---|")
    for s in t.get("stages", []):
        L.append(f"| {s['stage']} | {s['human']} ({s['seconds']}s) |")
    total_h = t.get("total_active_human", "0s")
    total_s = t.get("total_active_seconds", 0)
    L.append(f"| **Total active** | **{total_h} ({total_s}s)** |")
    L.append("")

    # --- Higgsfield credits ---
    hf = summary.get("platforms", {}).get("higgsfield", {})
    L.append("## Higgsfield — credits")
    L.append("")
    if hf.get("count"):
        L.append("| Asset | Scene | Type | Model | Credits | Source |")
        L.append("|---|---|---|---|---|---|")
        for it in hf.get("items", []):
            L.append(f"| {it.get('id','')} | {it.get('scene_id','') or ''} | {it.get('type','')} "
                     f"| {it.get('model','') or ''} | {it.get('credits')} | {it.get('source')} |")
        L.append(f"| **Total** | | | | **{hf.get('total')}** | "
                 f"{hf.get('actual')} actual · {hf.get('estimated')} estimated |")
    else:
        L.append("_No Higgsfield credits recorded yet._")
    L.append("")

    # --- ElevenLabs (TTS characters + music seconds) ---
    L.append("## ElevenLabs")
    L.append("")
    plats = summary.get("platforms", {})
    tts = plats.get("elevenlabs")
    music = plats.get("elevenlabs_music")
    if tts or music:
        L.append("| Product | Consumed | Calls | Source |")
        L.append("|---|---|---|---|")
        if tts:
            L.append(f"| Voice (TTS) | {tts.get('total')} {tts.get('unit')} "
                     f"| {tts.get('calls')} | {tts.get('source')} |")
        if music:
            L.append(f"| Music | {music.get('total')} {music.get('unit')} "
                     f"| {music.get('calls')} | {music.get('source')} |")
    else:
        L.append("_No ElevenLabs usage recorded yet._")
    L.append("")
    return "\n".join(L)


def write_report(job_id: str) -> dict[str, Any]:
    """Build + persist cost_report.json and cost_report.md into the project's artifacts dir.

    Returns the summary dict. Never raises — a write failure returns the summary anyway.
    """
    summary = build_summary(job_id)
    try:
        adir = PROJECTS_DIR / job_id / "artifacts"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "cost_report.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        (adir / "cost_report.md").write_text(render_markdown(summary), encoding="utf-8")
    except OSError:
        pass
    return summary
