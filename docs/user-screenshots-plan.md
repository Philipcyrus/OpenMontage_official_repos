# User screenshots in Panda videos, carousels and images — build plan

**Scope:** `panda-video`, `panda-carousel` and `panda-image` jobs started from Dify (carousel and image: §5 "Carousel and image"). **Status:** built on branch `feat/user-screenshots` (not merged, not deployed); tested locally — see §8. **Date:** 2026-09-17.

---

## 1. What we are building

A user attaches several screenshots to the brief in Dify and says which one goes in which scene and
how to use it ("1 in scene 1, zoom on the Pay button", "3 and 4 in scene 2", "blur the card number").
The pipeline then:

- puts each screenshot in exactly the scene(s) the user chose — never one they didn't assign, never
  leaving one out;
- decides at the **scene plan** where each screenshot sits, how it moves, and where the Panda stands;
- generates the Panda stills and clips **with that area left empty**;
- after the edit, lays each screenshot onto its clip with **Remotion**, exactly as the layout says;
- assembles the video with `panda_render` exactly as today.

Screenshots are never sent to Higgsfield (a video model redraws an image, so UI text would break).
Placement costs 0 credits and gives the same result on every run.

## 2. Decisions already made

| Decision | Choice |
|---|---|
| Which screenshot goes in which scene | The user — in the brief, or at the scene plan gate once the scenes are visible. Claude follows it exactly. |
| Screenshots the user gave no scene for | Not used. Listed at the script gate as "not placed" so the user can say where. |
| Who decides the layout | Claude, per scene, at the scene plan, following the user's "how to use it". No fixed template. |
| How the generated shot relates to the screenshot | The still and clip are generated with space left for it; the screenshot is laid over that space. |
| When the layout is fixed | At the scene plan gate — the last gate before credits are spent. |
| Where user files live | `projects/{job}/inputs/` — never under `assets/`. |
| What assembles the video | `panda_render` (ffmpeg lane), unchanged. Remotion only renders the screenshot scenes' clips. |
| When screenshots are laid in | At compose, after the edit fixes each scene's length, so overlay timing matches the voice. |

## 3. What must not change

- Jobs **without** screenshots: same prompts, same gates, same render. Enforced by a snapshot test of
  every prompt builder (§8).
- `panda_render`, the shared hybrid script/edit directors, the Higgsfield bridge, branding.
- Nothing from `inputs/` or `overlay/` may ever appear in `stills` or `clips`. The launcher treats every
  image/video under `assets/`, and any file path in a checkpoint, as generated media
  (`_mirror_artifacts` in `dify_launcher/runner.py`); user files there would be animated, regenerated on revise,
  and would upset gate routing.

## 4. The layout — one source of truth

Written by Claude into the approved scene plan, on the scene's `required_assets` item
(items are already open, and `source: "provided"` already exists — `scene_plan.schema.json:113`).
Validated by a new `schemas/artifacts/screen_layout.schema.json`.

```json
{
  "type": "image",
  "source": "provided",
  "input_id": "in_01",
  "description": "Checkout page — Pay button highlighted",
  "layout": {
    "zone":         {"x": 0.40, "y": 0.14, "w": 0.56, "h": 0.56},
    "subject_zone": {"x": 0.03, "y": 0.28, "w": 0.34, "h": 0.55},
    "frame": "phone",
    "show":  {"from_s": 0.0, "to_s": 5.0},
    "crop":  {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
    "enter": {"type": "slide_left", "at_s": 0.3, "duration_s": 0.5},
    "camera": "locked",
    "steps": [
      {"kind": "blur_region",   "region": {"x": 0.08, "y": 0.52, "w": 0.60, "h": 0.05}},
      {"kind": "cursor_move",   "to": [0.62, 0.81], "at_s": 1.6, "duration_s": 0.6},
      {"kind": "click_pulse",   "at": [0.62, 0.81], "at_s": 2.2},
      {"kind": "zoom_to",       "region": {"x": 0.30, "y": 0.70, "w": 0.60, "h": 0.22}, "at_s": 2.4, "duration_s": 0.8},
      {"kind": "highlight_box", "region": {"x": 0.40, "y": 0.76, "w": 0.44, "h": 0.09}, "at_s": 3.2, "duration_s": 1.6},
      {"kind": "card", "text": {"zh": "点这里付款", "en": "Tap Pay"},
       "zone": {"x": 0.42, "y": 0.64, "w": 0.52, "h": 0.05}, "at_s": 3.4, "duration_s": 1.4}
    ]
  }
}
```

- `zone`, `subject_zone`, card `zone`: fractions of the video frame.
- Step `region` / points: fractions of the **whole** screenshot (as in `ScreenshotScene`), so changing
  `crop` or `zoom_to` never invalidates them.
- `at_s`: seconds from the start of the scene. `blur_region` has no timing — it is on from the first frame.
- `frame`: `phone | browser | card | none`. Flat Panda look: white body, black keyline, no drop shadow.
- `show`: when the screenshot is on screen within the scene (optional; default the whole scene).
- Several screenshots in one scene (one item each): **side by side** (zones don't overlap) or **one after
  another in the same frame**, like a phone screen changing (zones may overlap only when `show` windows
  don't). Claude picks unless the user said.
- One screenshot in several scenes: one item in each scene.

The same layout drives four things, so they cannot disagree: the still/clip prompts (where the Panda
stands, what stays empty), the gate previews, the checks, and the final Remotion render.

## 5. Stage by stage

### Intake — `POST /jobs`
- **Dify** sends `options.media: [{"url": "...", "name": "checkout.png"}, ...]` in attachment order.
- **Launcher** (`dify_launcher/app.py` `create_job`), all **before** a job id exists:
  1. Count ≤ `SCREENSHOT_MAX_FILES` (env; Dify's own upload limit must allow the same number);
     Remotion available on this server (Node ≥ 22 + `remotion-composer/node_modules`) —
     otherwise 400 "screenshots need Remotion on this server".
  2. Download the links in parallel: only from hosts in `DIFY_FILES_HOSTS`; a relative `/files/...` link
     is prefixed with `DIFY_FILES_BASE`; no redirects; ≤ 10 MB each and a total cap; whole batch inside
     ~40 s (Dify HTTP node timeouts are 30–60 s — `DIFY_INTEGRATION.md:162`).
  3. Real image check (Pillow open + verify; png / jpeg / webp; pixel cap); apply EXIF rotation; strip
     metadata (location); convert to sRGB PNG.
  4. Write to a temp dir, then move to `projects/{job}/inputs/in_01.png …` plus `inputs.json`
     `[{n, input_id, name, width, height, sha256}]`. Any failure → 400 with the reason, nothing created.
     (`init_project` is idempotent, so creating `inputs/` early is safe.)
- Public job view gains `inputs: [{n, name}]` so Dify can echo the numbering.

### Script — start leg (gate: `approve_script`)
- **Prompt facts** (only when `inputs/` exists): one line per screenshot — number, name, size, absolute path.
- **idea-director** (Panda-owned): view the screenshots (one parallel turn) and turn the user's guidance
  into `inputs/requests.json`:
  `[{n, input_id, scenes: [N, ...], moment: "<user's words when they named a moment, not a number>",
  instruction: "<how to use it, user's words>", shows: "<one line>"}]`. Screenshots are referred to by
  attachment number or file name. No guidance → `scenes: []`, `moment: ""` (not placed). Scene numbers
  the user gave are binding, so the brief is structured around them and narration talks about what is
  on screen.
- **Launcher**: validate `requests.json`; append a "Your screenshots" list to `script.md`
  ("1 · checkout.png → scene 1 · zoom on the Pay button", "6 · receipt.png → not placed");
  `screens_board` (`screens_uploads.png`) shows every upload as a numbered thumbnail with its scene, so "screenshot 7" is
  unambiguous before anything is planned. Problems go into the gate question.

### Scene plan (gate: `approve_scene_plan`)
- **Prompt facts**: screenshots, requests, frame size, and the areas to keep clear as fractions —
  caption strip (`draw_caption`, overlays.py:104) and logo area (`draw_logo`, overlays.py:227),
  stored per canvas in `KEEP_OUT` (`lib/screen_layout.py`) and re-measured from the vendored drawing
  code by a contract test. For 1080×1920: captions x 0.03–0.97, y 0.76–0.85; logo x 0.64–0.97, y 0.03–0.13.
- **scene-plan-director** (Panda-owned): scene N carries exactly the screenshots assigned to N; a
  `moment` assignment goes in the scene covering that moment (shown for the user to confirm); unplaced
  screenshots appear nowhere. Write the layout (§4) on each; the scene keeps its normal
  `source: "generate"` still, composed to leave the zones empty.
- **Re-assigning**: at this gate the user sees the real scene numbers and can revise ("move 4 to scene 6",
  "put 6 in scene 3"). The revise leg updates `requests.json` and the layouts; checks and board re-run.
- **Launcher checks** (§6) and **preview**: `screens_board` (`screens_layouts.png`) — one Remotion still showing every
  screenshot scene with the screenshot placed, the Panda area marked, and the final state of boxes,
  blur and cards. New artifact key `screens_board`. `scene_plan.md` lists each layout in words.
- Approving this gate fixes the layouts. After the stills are approved, moving `zone` or
  `subject_zone` needs a new still for that scene (paid); everything inside the zone stays free to change.

### Stills (gates: `approve_hero_still`, `approve_stills`)
- **Prompt facts** per screenshot scene (hero included), e.g. "s03: keep x 0.40–0.95, y 0.10–0.70
  plain white — no character, props or text; character inside x 0.03–0.37".
- **asset-director** (Panda-owned): honour those areas in every still prompt.
- **Launcher**: clear-area check on each screenshot scene's still (cover-cropped to the frame the way
  `panda_render` does); `screens_board` (`screens_stills.png`) shows each screenshot scene over its
  still, with any problem noted under the scene. The existing storyboard preview is unchanged.

### Clips (gates: `approve_motion_sample` if on, `approve_assets`)
- **Prompt facts**: screenshot scenes use a locked camera; the subject stays in its area; background stays plain.
- **Launcher**: clear-area check on 5 sampled frames of each screenshot scene's clip; `screens_board`
  (`screens_clips.png`) shows one combined frame per screenshot scene (clip frame + screenshot).

### Edit
- Unchanged. Screenshot scenes' clips are ordinary clips.

### Compose (gate: `approve_final`)
- **compose-director** (Panda-owned), new step before `panda_render`: if the approved scene plan has
  layouts, call `screen_overlay` (mode `compose`) with the exact scene list it is about to pass to
  `panda_render`. The tool renders `overlay/<scene_id>.mp4` for each screenshot scene — the scene's clip
  from 0 s for exactly `duration_s` (what `normalize_scene` does), with the screenshot layers on top — and
  returns the same list with those `media_path`s swapped. `panda_render` is then called as today.
- How: the clip is normalised with ffmpeg (same cover-crop, held to `duration_s`), Remotion renders the
  screenshot layers as a transparent PNG sequence (`PandaScreenOverlay`), and ffmpeg composites them at
  the exact frame size, clip fps, no audio, CRF 12 (`panda_render` re-encodes). On a local test white
  stayed 255 and the Panda yellow moved by at most 1 level.
- **Revise at the final gate** ("box later", "bigger", "no blur"): update the layout, re-run
  `screen_overlay` + `panda_render`. 0 credits.

### Carousel and image (no clips, no compose)
The same intake, `requests.json`, layout and checks, with three differences:
- **Units.** A carousel places screenshots by slide number; an image job has one scene, so a screenshot
  is either on the image (`"scenes": [1]`) or not used. Notes and boards say "slide 2" / "the image".
- **No timing, no caption strip.** A still shows every part of a layout at once (the Remotion `still`
  prop = the boards' settled state), so `show` / `enter` / `exit` / `at_s` are ignored and any two
  screenshots on one slide must not overlap. Slide copy is baked into the generated still, so the
  director keeps it out of the screenshot zones and the clear-area check counts it. The logo keep-out
  runs to the top-right corner, because the brand stamp sits closer to the corner on larger stills.
  Canvas defaults: carousel 4:5, image 1:1 (3:4 and 4:3 also measured).
- **Placement by the launcher, as soon as a still exists.** At the end of every `_mirror_artifacts`
  (right after the clean stills are copied into the job store), `screens.place_on_stills` renders each
  screenshot scene's still with its screenshots (`screen_overlay` mode `still`: one transparent Remotion
  PNG at the still's own size, alpha-composited, so every other pixel of a PNG still stays as
  generated; a JPEG still is re-encoded once at q95 from the clean original) and writes it
  over the store copy under the same name. The hero, the stills, the storyboard and the brand pass
  (`branded_stills`) therefore all carry the screenshots. The clean still under `assets/images` is
  never touched; `_still_abs_paths` already prefers it, so `fresh` / `edit` revisions work from it.
  Renders are cached per (still, layout, screenshots, language) under `overlay/stills/`; a failing
  render is tried at most twice per version. `SCREENSHOT_STILLS_BUDGET_S` (300 s) bounds the
  renders one pass may START, so a pass can also run the one render already under way; stills left
  over are placed by the next pass and are flagged at the gate, and the pass that runs when
  `approve_stills` is approved is not time-boxed, so nothing reaches the brand stamp unplaced
  without a note. The agent never calls the tool for stills.

## 6. Checks (launcher code, not prompts)

| When | Check | If it fails |
|---|---|---|
| `POST /jobs` | count, allowed host, size, real image, Remotion available | 400, no job |
| `approve_script` | `requests.json` valid; every upload listed once; scene numbers ≥ 1; unplaced uploads named | noted in the question + board |
| `approve_scene_plan` | layout schema; each scene shows exactly the screenshots assigned to it — none missing, none extra; unplaced screenshots nowhere; enough scenes for the highest scene number; zones inside the frame and clear of the logo, `subject_zone` and **the scene's caption as it really wraps** (measured with panda_render's own renderer, profile and fonts from the scene's `captions`; without them, or without the renderer, the one-line strip grows to `CAPTION_FALLBACK_LINES` and the note says it is an estimate); same-frame screenshots don't overlap in time; legible (shown scale not < 0.35× or > 2×); a zoom that barely zooms; step times fit the scene | noted + board |
| stills gates | screenshot area of each still is clear (busy pixels ≤ 4%, `SCREENSHOT_CLEAR_MAX_BUSY`) | noted + note under the scene on the board |
| carousel / image stills gates | the same clear-area check on the clean still (slide copy counts), and the still in the job store is the placed version (sha256 against `overlay/stills/status.json`); no caption-strip or timing rules at the scene plan | noted (no board — the stills show the screenshots) |
| clips gates | same check on 5 frames per clip, plus the caption clearance again (captions and the output size can change after the plan) | noted + board; a clip whose frames will not decode is reported as **could not be checked**, never as a pass |
| `approve_final` | each screenshot scene has its overlay clip, rendered from the approved layout (layout hash in a sidecar); then **each screenshot separately**, in its own scene and its own show window: `overlay/timeline.json` (written by compose, panda_render's xfade offsets) says where that window falls in the finished video, 3 settled moments inside it are sampled clear of the transitions, and each is compared with the composed clip against the clip without the screenshot | noted. No timeline, a final whose length no longer matches it, frames that will not decode, a missing reference clip, a screenshot the timeline does not place, one hidden under the caption, or one that looks too much like the clip behind it to compare are all **could not be checked** — every screenshot the plan places ends with an outcome — stated in the question, never a pass, never blocking |
| every gate | `inputs/` files unchanged (sha256); nothing from `inputs/` or `overlay/` in stills/clips | noted |

Rules for all checks: they never raise into state handling (render or check before mutating state, or
guard fully), and they explain the problem in the gate question instead of blocking the human.

## 7. What was built

| Area | File | What | Launcher restart |
|---|---|---|---|
| Intake | `dify_launcher/app.py` | `options.media` checked, downloaded and normalised before the job id exists, for panda-video / panda-carousel / panda-image; pipeline + language recorded in `inputs/job.json`; `inputs` in the job view; `media` dropped from stored options | **yes** |
| Launcher helpers | `dify_launcher/screens.py` (new) | intake, prompt facts (scene / slide / image wording), gate checks, boards, markdown sections, `place_on_stills` + placement check for carousel / image | **yes** |
| Launcher | `dify_launcher/runner.py` | facts appended in `_run_agent` (nothing for jobs without uploads); `inputs/` + `overlay/` skipped in `_mirror_artifacts`, which ends by placing screenshots on carousel / image stills; checks + board at every `awaiting_human` gate | **yes** |
| Shared rules | `lib/screen_layout.py` (new) | geometry (mirrors the TS), assignment + layout rules (video, and stills without timing or caption strip), keep-out areas for 6 canvases, the caption measured as rendered, timing against the composed duration, display windows, file lookups | **yes** (imported by the launcher) |
| Schemas | `schemas/artifacts/screen_layout.schema.json`, `screen_requests.schema.json` (new) | §4 layout, `requests.json` | no |
| Tool | `tools/video/screen_overlay.py` (new, auto-discovered) | modes `board` (uploads / layouts / stills / clips; one cell per display window, so a screenshot shown after another is still reviewable), `compose` (video: refuses a cut that would hide a placement, and records `overlay/timeline.json`) and `still` (carousel / image, called by the launcher) | no |
| Remotion | `remotion-composer/src/panda/{screenGeometry.ts, ScreenLayer.tsx, PandaScreenOverlay.tsx, PandaScreenBoard.tsx, PandaCompositions.tsx, entry.tsx}` (new) + one element in `Root.tsx` | frames (phone / browser / card / none), crop, `zoom_to`, highlight, cursor, click, blur, cards with the CJK font `panda_render` uses; `still` prop = settled state. `entry.tsx` bundles only the Panda compositions, so renders do not depend on the Google Fonts other compositions download | no |
| Directors | `skills/pipelines/panda-video/{idea,scene-plan,asset,compose}-director.md`, `panda-carousel/{idea,script,scene-plan,asset}-director.md`, `panda-image/{idea,scene-plan,asset}-director.md` | "User screenshots" sections, all starting "only when the prompt has a USER SCREENSHOTS block" | no |
| Pipeline | `pipeline_defs/panda-video.yaml` | compose `tools_available` += `screen_overlay` (carousel / image pipelines unchanged — the launcher places) | no |
| Docs | `dify_launcher/DIFY_INTEGRATION.md`, `dify_launcher/README.md`, `dify_launcher/CAROUSEL.md`, `dify_launcher/IMAGE.md`, `deploy/README.md`, `.env.example` | `options.media`, `inputs`, `screens_board`, `DIFY_FILES_HOSTS` / `DIFY_FILES_BASE`, limits, Node 22 for screenshot jobs | no |

The upstream `ScreenshotScene.tsx` is untouched; the Panda composition adds blur, zoom, `at_s` timing
and the CJK font alongside it.

## 8. Tests (all run locally on 2026-09-19, on `main` after PR #14)

| Test | Result |
|---|---|
| `python dify_launcher/test_screens.py` (new) — intake (allow-list; redirect / 404 / oversize / fake image / GIF / pixel cap refused; EXIF rotation applied; metadata stripped; nothing created on a bad upload; media only for panda-video / panda-carousel / panda-image; Remotion required); assignment and layout rules for all three pipelines; TS↔Python geometry; still clear-area; **prompts byte-identical for jobs without uploads**; facts appended with them; `inputs/` and `overlay/` never mirrored; gate notes + board; checks never raise | pass |
| the same file, §8 (rewritten) — the final check per screenshot: found in its own scene and window, a screenshot that appears only in the NEXT scene flagged, two sequential screenshots checked independently (only the missing one flagged), and no timeline / a cut changed after compose / an undecodable final / a missing reference clip / a screenshot the timeline does not place / one indistinguishable from its background each reported as **could not be checked** rather than a pass | pass |
| the same file, §9 — carousel / image placement: the store still carries the screenshot and the clean one never changes, renders cached per still+layout+language, a regenerated still is re-placed, a cached composite is published before any render starts, one slide's failure does not skip the others, the 2-failure cap, scene ids that share a prefix keep their caches, an archived rejected take does not hide the shipped still, a manifest gap and a pending placement are both flagged, the brand gate repeats the notes, a still outside `assets/images` still gives a revise leg the clean file | pass |
| the same file, §10 — `WIDTHxHEIGHT` and unlisted ratios measured as produced (one parser shared with the launcher), the stills logo keep-out covers the fixed-pixel brand stamp on small stills, slide / image wording, the newest scene plan wins, `inputs/job.json` carries the job's language (a Mandarin brief sent with `language:en` becomes zh) | pass |
| the same file, §11 — a placement at 4–5 s in a scene cut to 3 s is named and compose refuses (no silent move), an implicit whole-scene window follows the cut, steps and entrances measured against the cut, a still pipeline has no timing; the caption area measured from the scene's own wrapped caption (short caption clear, long one flagged, no captions = the one-line strip, fallback declared as an estimate, re-checked at the later gates, never for carousel / image); sequential screenshots give two display windows and simultaneous ones give one | pass |
| `python -m pytest tests/contracts/test_screen_layout.py` (36) — keep-out areas re-measured from `overlays.py` for 6 canvases **and the brand stamp at 7 real still sizes**, a wrapped bilingual caption re-measured above the one-line strip on all 6, schemas valid, this doc's §4 example is a clean layout, geometry, tool discovered without Node, `compose` swaps only screenshot scenes and keeps durations, **records panda_render's own xfade offsets** (under the scene id even when compose inferred it from the media path) and **refuses a cut that would hide a placement**, the board splits sequential screenshots into separate cells, directors + pipeline wired for all three pipelines | pass |
| `python -m pytest tests/tools/test_screen_overlay_render.py` (4) — real Remotion renders: the screenshot lands on the pixels the Python geometry predicts, a short clip is held to the scene length, the board renders, the still mode keeps every generated pixel outside the screenshot, JPEG / EXIF-rotated / 16-bit stills come out right, and a board of two sequential screenshots really shows each one in its own cell | pass (33 s) |
| `python dify_launcher/test_claude_adapter.py`, `python dify_launcher/test_dify_flow.py` (existing) | pass |
| `python -m pytest tests -q` | 17 failed / 1088 passed — the same 17 failures as `main` (Veo / Google Music / runtime-presentation, plus the `test_voice_cast` premix stub that PR #14 left stale) |
| Whole-job simulation through the real launcher, panda-video (scratch script; agent legs simulated, everything else real: intake from an HTTP server, gates, Remotion boards, `screen_overlay`, `panda_render`) | pass (222 s) — uploads board at the script gate; layout board + logo-corner notes at the scene plan; a still with the character's arm in the screenshot area flagged, cleared after a revise; clips board; both screenshots found **in their own scenes**, with the recorded 13.0 s timeline matching the 12.83 s render |
| Whole-job simulation, panda-carousel + panda-image (same harness, real still placement, storyboard and brand pass) | pass (34 s) — hero and stills gates show the placed screenshots, slide copy in the screenshot area flagged, an edit revise works from the clean still and is re-placed, branded copies keep the screenshots |

Not covered by tests: whether a real agent leg follows the new carousel / image director text, and
Linux-only behaviour (a hung Remotion render, `EXDEV`, systemd `PATH`). Both need the box.

## 9. Rollout

Local branch → PR on GitHub → merge → the box pulls. Nothing is edited on the box. Everything is inert
for jobs that do not send `options.media`, and `options.media` is refused until `DIFY_FILES_HOSTS` is set.

**Before merge.** The registry import test passes (a tool that fails to import would break tool lookup
for every job from the next agent leg — `ToolRegistry.discover()` has no error handling).

**Box readiness (read-only).** In the environment that starts the launcher: `node -v` ≥ 22 ahead of
the system Node 18 (`deploy/README.md` "Node runtime"); `remotion-composer/node_modules` installed;
`python -m pytest tests/tools/test_screen_overlay_render.py` passes on the box (the first run downloads
Remotion's headless Chrome). In Dify: attach one image and note the link the flow receives (absolute or
`/files/...`) and confirm the box can download it before it expires.

**Deploy.** Pull, wait until no job is mid-leg, **restart the launcher**. Director text applies from the
next leg after the pull; every new rule starts with "only when the prompt has a USER SCREENSHOTS
block", so it is inert for normal jobs before the restart. Then one normal job to the scene plan gate
(unchanged), and one API job with 3 screenshots through every gate to the final video.

**Dify flow.** Set `DIFY_FILES_HOSTS` (+ `DIFY_FILES_BASE` if links are relative) and restart; turn on
image upload; send `options.media`; show `screens_board` full width. One end-to-end job from the chat.

## 10. Not in v1

- Screenshot inside a generated phone the Panda holds (blank screen + frame-by-frame tracked composite).
- Screenshot scenes with no Panda shot behind them (needs a no-generation scene and gate-count changes).
- Video uploads, adding screenshots at a later gate, photos as generation references.

## 11. Decisions taken in the build

1. Personal data (card numbers, ICCID / IMEI, phone numbers, emails, names, addresses, account QR codes)
   is blurred by default — the scene-plan director adds `blur_region` even when the user did not ask;
   it shows on the layouts board and the user can remove it with a revise.
2. Failed checks warn in the gate question; they never block the human.
