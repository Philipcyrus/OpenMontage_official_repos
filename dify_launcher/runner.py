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

Gate sequence (matches pipeline_defs/panda-video.yaml — upstream shape + a Panda cost gate):
    start ─▶ GATE 1 approve_script ─▶ GATE 2 approve_scene_plan (TEXT)
          ─▶ GATE 3 approve_stills ─▶ GATE 4 approve_assets ─▶ GATE 5 approve_final ─▶ done
scene_plan produces a TEXT plan only (no media). The assets stage runs in TWO human-reviewed
phases (both stage="assets"): first STILLS ONLY (cheap — approve the look before any expensive
video), then the full media (clips + voice + music) recorded in asset_manifest. The two pauses
are distinguished by the checkpoint's partial_progress.phase ("stills" vs full).
Branding is NOT a gate — it's a separate on-demand step after approve_final.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dify_launcher import store

_ENGINE_ROOT = Path(__file__).resolve().parents[1]

# ordered human-approval gates. Note: approve_stills + approve_assets are TWO pauses of the
# single `assets` stage (stills-first cost gate, then full media) — see _sync/_do_stills.
GATES = ["approve_script", "approve_scene_plan", "approve_stills", "approve_assets", "approve_final"]
# gates from the previous storyboard-stills flow — resuming one is refused with a migration note
_LEGACY_GATES = {"approve_storyboard", "approve_clips"}

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
        return self._do_script(state, {})

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        decision = (response or {}).get("decision", "approve")
        gate = state.get("gate")

        if gate in _LEGACY_GATES:
            return _legacy_migration(state)

        # revise: regenerate the CURRENT gate's artifact, stay at the same gate
        if decision == "revise":
            regen = {
                "approve_script": self._do_script,
                "approve_scene_plan": self._do_scene_plan,
                "approve_stills": self._do_stills,
                "approve_assets": self._do_assets,
                "approve_final": self._do_production,
            }.get(gate)
            if not regen:
                raise ValueError(f"cannot revise from gate {gate!r}")
            return regen(state, response)

        # approve: advance to the next stage/gate
        if gate == "approve_script":
            return self._do_scene_plan(state, response)
        if gate == "approve_scene_plan":
            return self._do_stills(state, response)      # assets phase 1: stills only
        if gate == "approve_stills":
            return self._do_assets(state, response)       # assets phase 2: clips + audio + manifest
        if gate == "approve_assets":
            return self._do_production(state, response)
        if gate == "approve_final":
            state.update(status="done", gate=None, question=None)
            return state
        raise ValueError(f"cannot resume from gate {gate!r}")

    # --- GATE 1: script ----------------------------------------------------
    def _do_script(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        brief = state.get("brief", "")
        note = (response or {}).get("answer")
        script = (
            f"# Script (mock)\n\n**Brief:** {brief}\n\n"
            + (f"_Revision note: {note}_\n\n" if note else "")
            + "1. Open on the Panda mascot.\n2. Explain the tip.\n3. CTA.\n"
        )
        store.artifact_path(job_id, "script.md").write_text(script, encoding="utf-8")
        state.update(
            stage="script", status="awaiting_human", gate="approve_script",
            question="Approve the script, or request a revision.",
            artifacts={**state.get("artifacts", {}), "script": "script.md"},
        )
        return state

    # --- GATE 2: TEXT scene plan (no media generated here) -----------------
    def _do_scene_plan(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Produce ONLY a schema-valid, TEXT scene plan (upstream contract). No stills, no
        media — each scene declares required_assets the assets stage fulfils later."""
        job_id = state["job_id"]
        brief = state.get("brief", "")
        note = (response or {}).get("answer")
        n = 3
        scenes = []
        for i in range(n):
            scenes.append({
                "id": f"scene-{i+1}",
                "type": "generated",
                "description": (f"Scene {i+1} for: {brief}"
                                + (f" (revised: {note})" if note else "")),
                "start_seconds": float(i * 3),
                "end_seconds": float((i + 1) * 3),
                "framing": "medium",
                "movement": "static",
                "required_assets": [
                    {"type": "image", "description": f"Panda keyframe for scene {i+1}",
                     "source": "generate"},
                    {"type": "video", "description": f"Motion clip for scene {i+1}",
                     "source": "generate"},
                ],
            })
        scene_plan = {"version": "1.0", "scenes": scenes}
        store.artifact_path(job_id, "scene_plan.json").write_text(
            json.dumps(scene_plan, indent=2), encoding="utf-8")
        # Surface the plan inline (dict) so Dify can review it as TEXT — no stills here.
        arts = {k: v for k, v in state.get("artifacts", {}).items() if k != "stills"}
        state.update(
            stage="scene_plan", status="awaiting_human", gate="approve_scene_plan",
            question="Approve the scene plan (text), or request a revision.",
            artifacts={**arts, "scene_plan": scene_plan},
        )
        return state

    # --- GATE 3: assets PHASE 1 — stills only (cheap, pre-video cost gate) --
    def _do_stills(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Generate STILLS ONLY from the approved scene plan — one per scene, NO video yet.
        This is the cost/visual checkpoint: approve the look (on-model panda, composition)
        before any expensive image->video spend. Real gen is Higgsfield image generation; the
        mock makes placeholder stills. On the box this checkpoint carries
        partial_progress.phase='stills' so the launcher surfaces it as approve_stills."""
        job_id = state["job_id"]
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        n = len(scenes) or 3
        stills = self._placeholder_stills(job_id, n)              # stills are made HERE (no video)
        # entering the stills phase drops any clips/manifest from a prior pass
        arts = {k: v for k, v in state.get("artifacts", {}).items()
                if k not in ("clips", "asset_manifest")}
        state.update(
            stage="assets", status="awaiting_human", gate="approve_stills",
            question="Approve the stills (one per scene) — on-model and well-composed? — or "
                     "request a revision. No video is generated until the stills are approved.",
            artifacts={**arts, "stills": stills},
        )
        return state

    # --- GATE 4: assets PHASE 2 — animate approved stills + audio + manifest
    def _do_assets(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Animate the APPROVED stills into motion clips (and, on the box, voice/music) and
        record everything in asset_manifest. Real gen is the Higgsfield MCP (image_to_video) +
        ElevenLabs; the mock renders a short clip per still. Reviewer approves the set or revises
        specific shots (response.shots) — only those regenerate."""
        job_id = state["job_id"]
        scene_plan = state.get("artifacts", {}).get("scene_plan") or {}
        scenes = scene_plan.get("scenes") or []
        stills = list(state.get("artifacts", {}).get("stills", []))
        n = len(stills) or (len(scenes) or 3)
        if len(stills) != n:                                     # safety: stills come from GATE 3
            stills = self._placeholder_stills(job_id, n)
        only = set((response or {}).get("shots", []))  # optional: regenerate specific shots

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
                           "source_tool": "mock_still", "scene_id": sid})
            assets.append({"id": f"vid-{i:02d}", "type": "video", "path": clips[i],
                           "source_tool": "mock_clip", "scene_id": sid})
        asset_manifest = {"version": "1.0", "assets": assets, "total_cost_usd": 0.0}
        store.artifact_path(job_id, "asset_manifest.json").write_text(
            json.dumps(asset_manifest, indent=2), encoding="utf-8")

        state.update(
            stage="assets", status="awaiting_human", gate="approve_assets",
            question="Approve the generated media (clips + audio), or request revision of "
                     "specific shots (send {\"decision\":\"revise\",\"shots\":[i,...]}).",
            artifacts={**state.get("artifacts", {}), "stills": stills, "clips": clips,
                       "asset_manifest": asset_manifest},
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

        state.update(
            stage="compose", status="awaiting_human", gate="approve_final",
            question="Approve the finished (unbranded) video, or request a revision. "
                     "Branding can be added on request after approval.",
            artifacts={**state.get("artifacts", {}), "final": "final.mp4", "branded": False},
        )
        return state

    # --- helpers -----------------------------------------------------------
    def _placeholder_stills(self, job_id: str, n: int) -> list[str]:
        from PIL import Image, ImageDraw
        names = []
        colors = [(11, 11, 11), (253, 197, 13), (30, 30, 30)]
        for i in range(n):
            img = Image.new("RGB", (1080, 1920), colors[i % len(colors)])
            d = ImageDraw.Draw(img)
            d.text((60, 900), f"Scene {i+1}", fill=(255, 255, 255))
            p = store.artifact_path(job_id, f"still_{i:02d}.png")
            img.save(p)
            names.append(p.name)
        return names

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
_PIPELINE_TYPE = os.environ.get("PANDA_PIPELINE_TYPE", "panda-video")


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
        from lib import checkpoint as cp
        cp.init_project(job_id, title=(state.get("brief") or "Panda video")[:80],
                        pipeline_type=_PIPELINE_TYPE)
        self._run_agent(self._start_prompt(job_id, state.get("brief", ""), state.get("options") or {}),
                        job_id, "script")
        return self._sync(state)

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        if state.get("gate") in _LEGACY_GATES:
            return _legacy_migration(state)
        from lib import checkpoint as cp
        gate = state.get("gate")
        decision = (response or {}).get("decision", "approve")
        stage = self._gate_stage(gate)

        # Approving the STILLS phase must NOT complete the assets stage — it only unlocks the
        # video/audio phase WITHIN the same stage. Continue the agent from the stills checkpoint
        # so it animates the approved stills and stops again at the full assets gate.
        if gate == "approve_stills":
            if decision == "approve":
                self._run_agent(self._stills_approved_prompt(job_id), job_id, "assets_media")
            else:
                self._run_agent(self._revise_prompt(
                    job_id, "assets (STILLS phase — regenerate the flagged stills and stop again "
                            "at the stills gate; do NOT generate video yet)", response or {}),
                    job_id, "stills_revise")
            return self._sync(state)

        if decision == "approve":
            self._approve_stage(job_id, stage)
            # Approving the LAST gate finishes the job — there is no next stage to run, so do
            # NOT spin up a pointless agent turn; just report done.
            nxt = cp.get_next_stage(self._projects_dir, job_id, _PIPELINE_TYPE)
            if nxt is None:
                return self._sync(state)
            prompt = self._continue_prompt(job_id)
            label = nxt
        else:
            prompt = self._revise_prompt(job_id, stage, response or {})
            label = f"{stage}_revise"
        self._run_agent(prompt, job_id, label)
        return self._sync(state)

    # -- agent invocation ---------------------------------------------------
    _TRANSIENT = ("connection closed", "api error", "overloaded", "rate limit",
                  "timeout", "timed out", " 500", " 502", " 503", " 529")

    def _run_agent(self, prompt: str, job_id: str = "", label: str = "") -> None:
        """Run `claude -p` once per leg. Retries on TRANSIENT API/network errors (a dropped
        connection shouldn't kill a long leg); the agent resumes from the latest checkpoint,
        so re-running the same prompt is safe. Records the leg's wall-time (all attempts) to
        the project's timing.jsonl for the per-project generation-time report."""
        import subprocess
        import time
        attempts = int(os.environ.get("CLAUDE_MAX_ATTEMPTS", "3"))
        last = ""
        started = time.monotonic()
        try:
            for i in range(attempts):
                proc = subprocess.run(
                    [self._bin, "-p", prompt, *self._extra],
                    cwd=str(_ENGINE_ROOT), capture_output=True, text=True, timeout=self._timeout,
                )
                if proc.returncode == 0:
                    return
                last = (proc.stderr or proc.stdout or "").strip()
                low = last.lower()
                transient = any(s in low for s in self._TRANSIENT)
                if not transient or i == attempts - 1:
                    break
                time.sleep(3 * (i + 1))  # brief backoff, then let the agent resume from checkpoint
            tail = last.splitlines()[-8:]
            raise RuntimeError("claude failed: " + " | ".join(tail))
        finally:
            self._record_timing(job_id, label, round(time.monotonic() - started, 2))

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
    def _sync(self, state: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        from lib import checkpoint as cp
        latest = cp.get_latest_checkpoint(self._projects_dir, job_id)
        if not latest:
            state.update(status="failed", question="agent produced no checkpoint")
            return state
        stage, status = latest.get("stage"), latest.get("status")
        arts = self._mirror_artifacts(job_id, latest.get("artifacts", {}))
        self._add_cost_report(job_id, arts)
        if status == "failed":
            state.update(status="failed", stage=stage, gate=None,
                         question=latest.get("error", "stage failed"), artifacts=arts)
        elif status == "awaiting_human":
            if stage == "assets":
                # the assets stage pauses twice: stills-first (cheap, pre-video), then full media.
                phase = (latest.get("partial_progress") or {}).get("phase")
                gate = "approve_stills" if phase == "stills" else "approve_assets"
            else:
                gate = _STAGE_GATE.get(stage, f"approve_{stage}")
            state.update(status="awaiting_human", stage=stage, gate=gate,
                         question=f"Approve {stage}, or request a revision.", artifacts=arts)
        else:  # completed
            nxt = cp.get_next_stage(self._projects_dir, job_id, _PIPELINE_TYPE)
            if nxt is None:
                state.update(status="done", stage=stage, gate=None, question=None, artifacts=arts)
            else:
                state.update(status="running", stage=stage, gate=None,
                             question=f"stage {stage} completed; next: {nxt}", artifacts=arts)
        return state

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

        # a storyboard contact sheet is a review aid, not a scene still — keep it out of stills
        imgs = [p for p in _scan(proj / "assets" / "images", (".png", ".jpg", ".jpeg"))
                if "contact" not in p.name.lower() and "sheet" not in p.name.lower()]
        vids = _scan(proj / "assets" / "video", (".mp4", ".mov", ".webm"))
        renders = _scan(proj / "renders", (".mp4", ".mov"))

        for p in _paths_in(artifacts):
            ext = p.suffix.lower()
            if ext in (".png", ".jpg", ".jpeg") and p not in imgs:
                imgs.append(p)
            elif ext in (".mp4", ".mov", ".webm"):
                if p.parent.name == "renders" or "final" in p.name.lower():
                    if p not in renders:
                        renders.append(p)
                elif p not in vids:
                    vids.append(p)

        stills = [n for n in (_copy(p) for p in imgs) if n]
        clips = [n for n in (_copy(p) for p in vids) if n]
        final = None
        if renders:
            pref = [p for p in renders if "final" in p.name.lower()] or renders
            final = _copy(pref[-1])

        md = _scan(proj / "artifacts", (".md",))
        if md:
            n = _copy(md[-1])
            if n:
                out["script"] = n
        # Surface structured TEXT artifacts inline for review at their gates: scene_plan (text
        # plan at GATE 2) and asset_manifest (media inventory at GATE 3). Prefer the checkpoint's
        # inline dict; fall back to the on-disk artifacts/<name>.json the skills write.
        for aname in ("scene_plan", "asset_manifest"):
            val = artifacts.get(aname)
            if not isinstance(val, dict):
                ap = proj / "artifacts" / f"{aname}.json"
                try:
                    val = json.loads(ap.read_text(encoding="utf-8")) if ap.is_file() else None
                except (OSError, ValueError):
                    val = None
            if isinstance(val, dict):
                out[aname] = val
        if stills:
            out["stills"] = stills
        if clips:
            out["clips"] = clips
        if final:
            out["final"] = final
            out["branded"] = False
        out["_checkpoint_artifacts"] = artifacts  # raw non-file data for Dify context
        return out

    def _add_cost_report(self, job_id: str, arts: dict[str, Any]) -> None:
        """Build the per-project cost/time report (Higgsfield credits, ElevenLabs usage,
        generation time), mirror it into the job store, and surface it in the Dify view:
        `cost_report` as a downloadable .md link, `cost_report_summary` inline. Non-fatal."""
        try:
            from lib import cost_report as cr
            summary = cr.write_report(job_id)
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
        arts["cost_report"] = "cost_report.md"     # ends .md -> surfaced as a download link
        arts["cost_report_summary"] = summary       # inline dict for Dify context

    # -- approvals + prompts ------------------------------------------------
    def _gate_stage(self, gate: Optional[str]) -> Optional[str]:
        if gate in ("approve_stills", "approve_assets"):
            return "assets"                 # both are pauses of the single assets stage
        for s, g in _STAGE_GATE.items():
            if g == gate:
                return s
        return None

    def _approve_stage(self, job_id: str, stage: Optional[str]) -> None:
        if not stage:
            return
        from lib import checkpoint as cp
        existing = cp.read_checkpoint(self._projects_dir, job_id, stage) or {}
        cp.write_checkpoint(
            self._projects_dir, job_id, stage, "completed",
            existing.get("artifacts", {}), pipeline_type=_PIPELINE_TYPE,
            human_approval_required=True, human_approved=True,
        )

    def _start_prompt(self, job_id: str, brief: str, options: Optional[dict[str, Any]] = None) -> str:
        options = options or {}
        lang = str(options.get("language", "en")).lower()
        narrator = str(options.get("narrator", "panda")).lower()
        voice_id = options.get("voice_id")            # explicit override from Dify
        music = options.get("music", True)            # BGM: mood string, True (default bed), or False
        runtime = str(options.get("render_runtime", "auto")).lower()  # auto|ffmpeg|remotion|hyperframes

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

        if voice_id:
            voice_line = (f"VOICE — use ElevenLabs voice_id='{voice_id}' (explicit override from "
                          "the job). ")
        else:
            voice_line = ("VOICE — use ElevenLabs with the voice_id from config/panda-elements.json "
                          f"`voices` matching narrator='{narrator}' and language='{lang}'. ")
        voice_line += ("Only if ElevenLabs is truly unavailable, fall back to Higgsfield audio and "
                       "record that decision.")

        if music is False or str(music).lower() in ("false", "none", "no", "off"):
            music_line = "MUSIC — do NOT add a background music bed for this job."
        else:
            mood = "" if music is True else f" Mood/brief: {music}."
            music_line = ("MUSIC — add a background music bed via the `music_gen` tool "
                          f"(ElevenLabs Music, same ELEVENLABS_API_KEY).{mood} Keep it under the VO.")

        return (
            f"Run the `{_PIPELINE_TYPE}` pipeline to produce a video.\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    narrator: {narrator}\n\n"
            "BRAND — MANDATORY, do NOT improvise: read config/panda-elements.json and USE its "
            "Higgsfield reference Element IDs for character consistency — the panda Element for "
            "every panda shot, the customer Element for the customer. Never invent a new panda.\n"
            f"{voice_line}\n{music_line}\n{runtime_line}\n\n"
            "Follow AGENT_GUIDE.md and skills/meta/checkpoint-protocol.md. Execute stages in "
            "order. At every stage whose manifest sets human_approval_default: true, write the "
            "checkpoint with status='awaiting_human' and STOP (end your turn) — do NOT "
            "self-approve.\n"
            "PIPELINE SHAPE: the `scene_plan` stage produces ONLY a structured TEXT plan — NO "
            "media, NO generation tools. The `assets` stage then runs in TWO human-reviewed "
            "phases (a cost gate):\n"
            "  PHASE 1 (stills): generate ONLY the stills — one per scene — via the Higgsfield "
            "MCP bridge (skills/meta/higgsfield-mcp-bridge.md), then write the assets checkpoint "
            "with status='awaiting_human' AND partial_progress={\"phase\":\"stills\"} and STOP. "
            "Do NOT generate any video yet.\n"
            "  PHASE 2 (media): only after the stills are approved, animate the approved stills "
            "into motion clips (image_to_video) + generate narration/music (ElevenLabs), record "
            "everything in asset_manifest, then write the assets checkpoint status='awaiting_human' "
            "(no 'stills' phase marker) and STOP.\n"
            "Finally compose the approved assets with the `panda_render` tool. Stop at the first gate."
        )

    def _continue_prompt(self, job_id: str) -> str:
        return (
            f"Continue the `{_PIPELINE_TYPE}` pipeline for project_id: {job_id}. Read the latest "
            "checkpoint, proceed from the next stage, and STOP at the next human_approval gate "
            "(status='awaiting_human', end your turn). If the pipeline is complete, finish."
        )

    def _stills_approved_prompt(self, job_id: str) -> str:
        return (
            f"For project_id: {job_id}, the STILLS phase of the `assets` stage is APPROVED. Do NOT "
            "mark the assets stage completed yet. Animate the approved stills into motion clips "
            "(image_to_video via the Higgsfield MCP bridge) and generate narration/music "
            "(ElevenLabs), record every file in asset_manifest, then rewrite the assets checkpoint "
            "with status='awaiting_human' (WITHOUT the 'stills' phase marker) and STOP for the "
            "full media approval."
        )

    def _revise_prompt(self, job_id: str, stage: Optional[str], response: dict[str, Any]) -> str:
        note = response.get("answer", "(no note)")
        shots = response.get("shots") or []
        shot_txt = f" Regenerate only shots {shots}." if shots else ""
        return (
            f"Revise stage '{stage}' for project_id: {job_id} per this feedback: {note}.{shot_txt} "
            "Rewrite that stage's checkpoint with status='awaiting_human' and STOP for approval."
        )


def get_runner(name: str) -> Runner:
    return {"mock": MockRunner, "claude": ClaudeCodeRunner}.get(name, MockRunner)()
