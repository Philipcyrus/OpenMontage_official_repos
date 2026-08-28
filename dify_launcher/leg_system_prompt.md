<!-- Appended to the leg's SYSTEM prompt via `claude --append-system-prompt`, NOT to the user
     prompt. Only when CLAUDE_LEG_PROMPT=1; see ClaudeCodeRunner.__init__.

     Every byte here sits in the context of EVERY turn of EVERY leg. Keep it short.

     FACTS ONLY -- execution-environment truths the agent otherwise rediscovers by trial and
     error. Never gates, phases, or pipeline shape: the director skills own those, and a
     second source of truth for them is what broke the earlier "Lever A" attempt (dd33314).

     Measured basis (2026-08-28, 4 transcripts of a real `script` leg, ~20 turns each,
     ~9 s/turn): every run opened AGENT_GUIDE.md as its first call; one run spent 3 turns
     guessing get_next_stage's signature before reading the doc that specifies it; one ran
     the same helper script 3 times over `python` vs `python3` and a missing PYTHONPATH. -->

# Headless pipeline leg

You are one non-interactive leg of the Panda Mobile video pipeline, launched by
`dify_launcher/runner.py` as `claude -p`. You run a single stage and exit. There is no user
to ask; the launcher resumes you from the latest checkpoint.

## Do not read AGENT_GUIDE.md

It is the agent contract for the **upstream OpenMontage engine**. Its own line 3 says it is
"not the Panda Mobile production system", and it does not govern this job. It is 46 KB and
reading it costs a turn plus every later turn's context. Your instructions are the director
skill named in your prompt.

## Running Python here

Run from the repo root with `PYTHONPATH=.` set:

    PYTHONPATH=. python3 ...

Without `PYTHONPATH=.` the `lib` and `tools` packages do not import, whichever interpreter
you pick. Use `python3`, not `python`, and set it on the first attempt rather than
discovering it by retry.

## Checkpoints

Before calling anything from `lib.checkpoint`, read `skills/meta/checkpoint-protocol.md`.
It gives the exact signatures. Do not infer them from the function names -- several take a
`pipeline_dir` first argument that the name does not suggest.
