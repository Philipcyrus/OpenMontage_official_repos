#!/usr/bin/env python
"""Dev / ops CLI to drive the panda-video pipeline via the REAL ClaudeCodeRunner directly.

This bypasses the HTTP launcher on purpose: each leg runs `claude -p` synchronously and can
take minutes (real Higgsfield generation), which is awkward over HTTP. Use this to run and
field-verify the real pipeline one gate at a time, watching the output. The HTTP launcher
(dify_launcher/app.py) is the production path Dify calls; this is the operator's console.

Auth: uses the box's Claude Code subscription login (~/.claude). It force-clears the
ANTHROPIC_* env so a stray key/OpenRouter value can't hijack the subprocess, and forces a
clean CLAUDE_EXTRA_ARGS so an inline-comment in .env can't leak into the claude argv.

Usage:
  python scripts/run_job.py start  "<brief>"                 # new job -> runs to first gate
  python scripts/run_job.py resume <job_id> <gate> [approve|revise] ["note"]
  python scripts/run_job.py status <job_id>                  # read current checkpoint state

Gates: approve_script -> approve_storyboard -> approve_clips -> approve_final
Examples:
  python scripts/run_job.py start "35s vertical eSIM tip with a panda mascot"
  python scripts/run_job.py resume job_ae0a331161fe approve_script approve
  python scripts/run_job.py resume job_ae0a331161fe approve_clips revise "more motion on shot 2"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Repo root = this file's parent's parent. Portable — no hard-coded home path.
ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# Force a clean claude invocation regardless of .env quirks.
os.environ["CLAUDE_EXTRA_ARGS"] = os.environ.get("CLAUDE_EXTRA_ARGS_OVERRIDE", "--dangerously-skip-permissions")
os.environ.setdefault("PANDA_PIPELINE_TYPE", "panda-video")
for _k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

from dify_launcher import runner as R  # noqa: E402
from dify_launcher import store  # noqa: E402


def _show(out: dict) -> None:
    arts = out.get("artifacts") or {}
    print("\n=== RESULT ===")
    print(f"job: {out.get('job_id')} | status: {out.get('status')} "
          f"| stage: {out.get('stage')} | gate: {out.get('gate')}")
    print("question:", out.get("question"))
    print("artifact keys:", list(arts.keys()))
    for k in ("script", "stills", "clips", "final", "branded"):
        if k in arts:
            print(f"  {k}: {arts[k]}")
    print("--- full artifacts (truncated) ---")
    print(json.dumps(arts, indent=2, default=str)[:3000])


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    run = R.ClaudeCodeRunner()

    if cmd == "start":
        # positional brief = first non-flag arg after "start"; --lang/--narrator -> options
        rest = sys.argv[2:]
        opts = {"language": "en", "narrator": "panda"}
        brief_parts = []
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--lang" and i + 1 < len(rest):
                opts["language"] = rest[i + 1].lower(); i += 2
            elif a == "--narrator" and i + 1 < len(rest):
                opts["narrator"] = rest[i + 1].lower(); i += 2
            else:
                brief_parts.append(a); i += 1
        brief = " ".join(brief_parts) or "Panda video"
        job = store.new_job_id()
        store.ensure_job(job)
        state = {"job_id": job, "brief": brief, "profile": "ugc", "options": opts,
                 "status": "running", "stage": None, "gate": None, "artifacts": {}}
        print(f">>> START {job}  (lang={opts['language']} narrator={opts['narrator']}; real generation — may take minutes)")
        out = run.start(state)
        store.save_state(out)
        _show(out)

    elif cmd == "resume":
        if len(sys.argv) < 4:
            print("resume needs: <job_id> <gate> [approve|revise] [note]")
            sys.exit(1)
        job, gate = sys.argv[2], sys.argv[3]
        decision = sys.argv[4] if len(sys.argv) > 4 else "approve"
        resp: dict = {"decision": decision}
        if len(sys.argv) > 5:
            resp["answer"] = sys.argv[5]
        state = {"job_id": job, "gate": gate, "status": "awaiting_human", "artifacts": {}}
        print(f">>> RESUME {job}: {decision} from {gate} (real generation — may take minutes)")
        out = run.resume(state, resp)
        store.save_state(out)
        _show(out)

    elif cmd == "status":
        job = sys.argv[2]
        _show(run._sync({"job_id": job}))

    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
