# Higgsfield MCP Bridge — Agent Protocol

This project generates video through the **agent's Higgsfield MCP connection**,
not the Higgsfield Cloud REST API. The registry provider is
`higgsfield_mcp_video` (provider id `higgsfield_mcp`), backed by
`tools/video/higgsfield_mcp_video.py`. Read this before any asset stage that
produces motion video.

## Why a bridge

OpenMontage tools are Python `BaseTool` subclasses that call provider APIs with
keys. The Higgsfield connection on this machine is an **MCP integration** —
callable only by the agent, not from inside the Python process. So generation is
split: the **agent** calls Higgsfield MCP to make the clip; the
**`higgsfield_mcp_video` tool** ingests the result (download + ffprobe +
provenance) into the project so the `asset_manifest` stays consistent.

`higgsfield_mcp_video` reports `AVAILABLE` whenever `HIGGSFIELD_MCP_BRIDGE=1` is
set in `.env`. That flag means "the agent driving this repo has a working
Higgsfield MCP connection." It is NOT a guarantee the MCP server is reachable —
if an MCP call fails, surface a structured blocker per the AGENT_GUIDE
"Escalate Blockers Explicitly" rule. Do not silently fall back to another
provider.

## MCP tool names

The Higgsfield MCP tools are namespaced (e.g. `mcp__claude_ai_Higgsfield__*`).
The relevant ones:

- `balance` — check available credits before a batch.
- `models_explore` (action `recommend`/`get`) — pick a model, confirm exact
  model id, durations, aspect ratios, and which media roles it accepts.
- `generate_video` — submit a generation. Pass `get_cost:true` first to
  preflight the credit cost without spending.
- `job_status` / `show_generations` — poll until the job is `completed`.
- `reveal_generation` / `job_display` — obtain the finished clip's CDN URL.
- `generate_image` — make a still first when doing image-to-video.
- `media_import_url` — register a web image URL, returns a `media_id` to pass as
  a `medias[]` value (never pass raw https URLs into `medias`).

Always confirm the live model catalog with `models_explore` rather than trusting
hardcoded ids. Model guidance from the server: `seedance_2_0` for identity /
cinematic; `kling3_0` for multi-shot, native audio, or motion transfer;
`kling3_0_turbo` for fast text-to-video or single start-frame animation.

## Per-clip generation loop

For each scene/clip the `scene_plan` requires:

1. **Preflight cost** — call `generate_video` with
   `{model, prompt, duration, aspect_ratio, count:1, get_cost:true}`. Sum the
   credits across all clips and check against `balance`. Report the total to the
   user against the budget before committing to a batch. **Retain the per-clip
   credit number** — it must be written into that asset's `asset_manifest` entry
   (`credits`, `credits_source: "actual"`) for the per-project cost report.
2. **Submit** — call `generate_video` (omit `get_cost`) with the final params.
   Capture the returned `job_id`. For image-to-video, first either
   `generate_image` or `media_import_url` to get a `media_id`, then pass it via
   the model's declared start-image media role.
3. **Poll** — `job_status` (or `show_generations`) until `completed`. Handle
   `failed`/`nsfw`/`cancelled` as a blocker, not a silent skip.
4. **Reveal** — `reveal_generation` / `job_display` to get the clip CDN URL.
5. **Ingest** — call the registry tool `higgsfield_mcp_video.execute({...})`
   with the same creative params **plus**:
   - `video_url`: the CDN URL from step 4 (or `source_path` if you downloaded it
     yourself),
   - `job_id`: from step 2,
   - `output_path`: the project asset path, e.g.
     `projects/<name>/assets/video/<scene-id>.mp4`.
   The tool downloads, runs ffprobe, and returns a standard `ToolResult` with
   width/height/duration/codec for the `asset_manifest`.

If you invoke `higgsfield_mcp_video` with no `video_url`/`source_path`, it
returns `success=False` with an `agent_action_required` payload restating these
steps — a safety net, not the intended path.

## Provenance

Record per clip in the `asset_manifest`: `provider: higgsfield_mcp`, the `model`,
the `job_id`, the prompt, the probed dimensions, and the **`credits`** consumed
(from the step-1 `get_cost` preflight) with `credits_source: "actual"`. Higgsfield clips may include
native synced audio — note in `edit_decisions` whether a clip's audio is kept or
replaced by the narration/music mix so the compose stage doesn't double up.

## Cost note

Higgsfield bills in **credits**, not USD. `higgsfield_mcp_video.estimate_cost`
returns a rough USD figure for budget governance only; the authoritative number
is the `get_cost:true` preflight from MCP. Check `balance` before large batches.

## BUDGET HARD RULE (run-level credit ceiling)

When the job sets **`max_higgsfield_credits`** (a per-project credit cap), enforce a **hard
pre-generation block** — credits are the authoritative unit:

1. **Before calling ANY Higgsfield generation** (`generate_image`, `generate_video`, image→video —
   stills, motion sample, OR the full batch), compute:
   - `spent` = sum of `credits` already recorded in `asset_manifest` (this project so far), and
   - `requested` = sum of the `get_cost:true` preflight credits for the batch you are about to submit.
2. If `spent + requested > max_higgsfield_credits`: **DO NOT call the generation tool.** Write the
   `assets` checkpoint `status='awaiting_human'` **AND `partial_progress={"phase":"budget_hold"}`**,
   include the numbers (`cap`, `spent`, `requested`, `projected = spent+requested`) in the checkpoint,
   and **STOP**. The launcher surfaces this as the **`budget_exceeded`** gate.
3. The human then either **raises the cap** (resume carries a new `max_higgsfield_credits`), **revises**
   (regenerate a cheaper/smaller plan), or **cancels**. On resume, **re-run this check** before
   generating — never assume the raise is enough without re-summing.

No cap set (`max_higgsfield_credits` unset) → no ceiling; still record each asset's `get_cost`
credits in `asset_manifest`. This is stop-and-ask, not silent truncation: never quietly drop scenes
to fit a budget — always pause for the human decision.
