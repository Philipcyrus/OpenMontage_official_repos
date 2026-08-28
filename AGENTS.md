# OpenMontage

## Headless pipeline leg? Do NOT read AGENT_GUIDE.md.

**If your prompt names a `projects/job_*` directory, you ARE a headless leg.** That single
fact decides it — no further judgement needed. `dify_launcher/runner.py` starts one per pipeline
stage; you run that one stage and exit. There is no interactive user to ask.

- **Do not read `AGENT_GUIDE.md`.** It is the agent contract for the **upstream OpenMontage
  engine** — its own line 3 says "not the Panda Mobile production system". It is 46 KB, reading
  it costs a turn, and it then sits in your context for every remaining turn of the leg. Your
  instructions are the director skill named in your prompt.
  The only sections of it that apply to you are **Decision Communication Contract**, **Project
  Directory Convention** and **Human Checkpoint Protocol**; open those individually if the
  director skill leaves you short.
- **Running Python:** from the repo root, with `PYTHONPATH=.` set, using `python3`. Without
  `PYTHONPATH=.` the `lib` and `tools` packages do not import, whichever interpreter you pick.
  Set it on the first attempt rather than discovering it by retry.
- **Checkpoints:** read `skills/meta/checkpoint-protocol.md` **before** calling anything in
  `lib.checkpoint`, not after. It gives the exact signatures — several take a `pipeline_dir`
  first argument that the function name does not suggest. Do not infer them.

## Interactive session

**MANDATORY — interactive sessions ONLY, never a headless leg: Read
`AGENT_GUIDE.md` before responding to a user message.**

If the section above applies to you, this one does not. Stop here.

Do not act on the user's request until you have read AGENT_GUIDE.md.
It contains routing rules that determine your first action based on what the user asked.
Skipping it WILL cause you to take the wrong action.
