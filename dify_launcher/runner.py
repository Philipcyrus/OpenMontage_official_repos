"""Runners drive a job through the pipeline gates.

A runner advances a job to its NEXT human-approval gate, then stops. Each HTTP call
(start / respond) moves the job forward one leg. This mirrors how the real agent works:
it runs until a checkpoint writes `awaiting_human`, then pauses for Dify.

Two runners:
  - MockRunner        : no LLM, no Higgsfield. Fakes script + storyboard, and REALLY renders
                        a clean master from the approved stills via panda_render. Lets us test
                        the whole Dify handshake + local storage + gates here, with no EC2.
  - ClaudeCodeRunner  : the EC2 path — invokes Claude Code headless against the engine repo.
                        Skeleton only; swap it in where the box has `claude` + OpenRouter + MCP.

Gate sequence (matches pipeline_defs/panda-video.yaml):
    start ─▶ GATE 1 approve_script ─▶ GATE 2 approve_storyboard ─▶ GATE 3 approve_clips
          ─▶ GATE 4 approve_final ─▶ done
Branding is NOT a gate — it's a separate on-demand step after approve_final.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from dify_launcher import store

_ENGINE_ROOT = Path(__file__).resolve().parents[1]

# gate -> the stage the agent pauses AFTER producing that stage's artifact
GATES = ["approve_script", "approve_storyboard", "approve_clips", "approve_final"]


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

        # revise: regenerate the CURRENT gate's artifact, stay at the same gate
        if decision == "revise":
            regen = {
                "approve_script": self._do_script,
                "approve_storyboard": self._do_storyboard,
                "approve_clips": self._do_clips,
                "approve_final": self._do_production,
            }.get(gate)
            if not regen:
                raise ValueError(f"cannot revise from gate {gate!r}")
            return regen(state, response)

        # approve: advance to the next stage/gate
        if gate == "approve_script":
            return self._do_storyboard(state, response)
        if gate == "approve_storyboard":
            return self._do_clips(state, response)
        if gate == "approve_clips":
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

    # --- GATE 2: storyboard stills -----------------------------------------
    def _do_storyboard(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        provided = (response or {}).get("stills") or []
        stills: list[str] = []
        if provided:
            # Dify may hand us the stills directly (user-supplied storyboard).
            for i, src in enumerate(provided):
                p = Path(src)
                if p.is_file():
                    dst = store.artifact_path(job_id, f"still_{i:02d}{p.suffix or '.png'}")
                    dst.write_bytes(p.read_bytes())
                    stills.append(dst.name)
        if not stills:
            # else generate placeholder stills so the flow is self-contained
            stills = self._placeholder_stills(job_id, n=3)
        state.update(
            stage="scene_plan", status="awaiting_human", gate="approve_storyboard",
            question="Approve the storyboard stills, or request a revision.",
            artifacts={**state.get("artifacts", {}), "stills": stills},
        )
        return state

    # --- GATE 3: generate one motion clip per approved still ---------------
    def _do_clips(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """Animate each APPROVED still into a clip. Real gen is the Higgsfield MCP
        (image_to_video) on the box; the mock renders each still to a short clip so the
        per-shot clip gate is exercised locally. Reviewer approves the set or asks to
        revise specific shots (response.shots) — only those regenerate."""
        job_id = state["job_id"]
        stills = state.get("artifacts", {}).get("stills", [])
        only = set((response or {}).get("shots", []))  # optional: regenerate specific shots
        clips = list(state.get("artifacts", {}).get("clips", []))
        if len(clips) != len(stills):
            clips = [None] * len(stills)
        for i, still in enumerate(stills):
            if only and i not in only and clips[i]:
                continue  # keep already-approved shot
            clip_name = f"clip_{i:02d}.mp4"
            self._render_clean(
                [str(store.artifact_path(job_id, still))],
                str(store.artifact_path(job_id, clip_name)),
            )
            clips[i] = clip_name
        state.update(
            stage="assets", status="awaiting_human", gate="approve_clips",
            question="Approve the generated clips, or request revision of specific shots "
                     "(send {\"decision\":\"revise\",\"shots\":[i,...]}).",
            artifacts={**state.get("artifacts", {}), "clips": clips},
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

# OpenMontage stage  ->  launcher gate name (matches pipeline_defs/panda-video.yaml)
_STAGE_GATE = {
    "script": "approve_script",
    "scene_plan": "approve_storyboard",
    "assets": "approve_clips",
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
        self._run_agent(self._start_prompt(job_id, state.get("brief", ""), state.get("options") or {}))
        return self._sync(state)

    def resume(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        job_id = state["job_id"]
        from lib import checkpoint as cp
        decision = (response or {}).get("decision", "approve")
        stage = self._gate_stage(state.get("gate"))
        if decision == "approve":
            self._approve_stage(job_id, stage)
            # Approving the LAST gate finishes the job — there is no next stage to run, so do
            # NOT spin up a pointless agent turn; just report done.
            if cp.get_next_stage(self._projects_dir, job_id, _PIPELINE_TYPE) is None:
                return self._sync(state)
            prompt = self._continue_prompt(job_id)
        else:
            prompt = self._revise_prompt(job_id, stage, response or {})
        self._run_agent(prompt)
        return self._sync(state)

    # -- agent invocation ---------------------------------------------------
    _TRANSIENT = ("connection closed", "api error", "overloaded", "rate limit",
                  "timeout", "timed out", " 500", " 502", " 503", " 529")

    def _run_agent(self, prompt: str) -> None:
        """Run `claude -p` once per leg. Retries on TRANSIENT API/network errors (a dropped
        connection shouldn't kill a long leg); the agent resumes from the latest checkpoint,
        so re-running the same prompt is safe."""
        import subprocess
        import time
        attempts = int(os.environ.get("CLAUDE_MAX_ATTEMPTS", "3"))
        last = ""
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
        if status == "failed":
            state.update(status="failed", stage=stage, gate=None,
                         question=latest.get("error", "stage failed"), artifacts=arts)
        elif status == "awaiting_human":
            state.update(status="awaiting_human", stage=stage,
                         gate=_STAGE_GATE.get(stage, f"approve_{stage}"),
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
        if stills:
            out["stills"] = stills
        if clips:
            out["clips"] = clips
        if final:
            out["final"] = final
            out["branded"] = False
        out["_checkpoint_artifacts"] = artifacts  # raw non-file data for Dify context
        return out

    # -- approvals + prompts ------------------------------------------------
    def _gate_stage(self, gate: Optional[str]) -> Optional[str]:
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
        return (
            f"Run the `{_PIPELINE_TYPE}` pipeline to produce a video.\n"
            f"project_id: {job_id}\nBrief: {brief}\n"
            f"language: {lang}    narrator: {narrator}\n\n"
            "BRAND — MANDATORY, do NOT improvise: read config/panda-elements.json and USE its "
            "Higgsfield reference Element IDs for character consistency — the panda Element for "
            "every panda shot, the customer Element for the customer. Never invent a new panda. "
            "VOICE — use ElevenLabs with the voice_id from config/panda-elements.json `voices` "
            f"matching narrator='{narrator}' and language='{lang}'. Only if ElevenLabs is truly "
            "unavailable, fall back to Higgsfield audio and record that decision.\n\n"
            "Follow AGENT_GUIDE.md and skills/meta/checkpoint-protocol.md. Execute stages in "
            "order. At every stage whose manifest sets human_approval_default: true, write the "
            "checkpoint with status='awaiting_human' and STOP (end your turn) — do NOT "
            "self-approve. Generate imagery/video via the Higgsfield MCP bridge "
            "(skills/meta/higgsfield-mcp-bridge.md) and compose with the `panda_render` tool. "
            "Stop at the first gate."
        )

    def _continue_prompt(self, job_id: str) -> str:
        return (
            f"Continue the `{_PIPELINE_TYPE}` pipeline for project_id: {job_id}. Read the latest "
            "checkpoint, proceed from the next stage, and STOP at the next human_approval gate "
            "(status='awaiting_human', end your turn). If the pipeline is complete, finish."
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
