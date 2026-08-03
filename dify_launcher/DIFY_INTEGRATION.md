# Wiring Dify → the Launcher

How to connect a Dify workflow to the Panda launcher. Dify makes **outbound HTTP calls** to
the launcher; the launcher drives the agent and pauses at each approval gate. Dify shows the
artifact, collects the user's decision, and calls back to resume.

```
Dify (dify.ai)  ──HTTP──▶  Launcher {BASE_URL}
  brief ─────────────────▶ POST /jobs                     → gate: approve_script
  show script, ask user
  decision ──────────────▶ POST /jobs/{id}/respond        → gate: approve_storyboard
  show stills, ask user
  decision ──────────────▶ POST /jobs/{id}/respond        → gate: approve_clips
  show clips, ask user
  decision ──────────────▶ POST /jobs/{id}/respond        → gate: approve_final
  show final video, ask
  decision ──────────────▶ POST /jobs/{id}/respond        → status: done
```

## Prerequisites
- Launcher deployed at a URL Dify can reach — call it `{BASE_URL}` (e.g. `https://panda.example.com`).
- Shared secret `DIFY_TOKEN` set on the launcher; Dify sends it as header `X-Dify-Token`.

## Auth (every request)
```
Header:  X-Dify-Token: <the DIFY_TOKEN value>
Header:  Content-Type: application/json
```

## Gate loop (the whole flow)
Dify repeats the same pattern for each gate: **read state → show artifact → ask user → respond**.
The launcher's response always contains `status`, `gate`, `question`, and `artifacts` — use those
to drive the next node.

### 1. Start the job  (HTTP node)
```
POST {BASE_URL}/jobs
{
  "brief": "{{ user_brief }}",
  "profile": "ugc"
}
```
Response:
```json
{ "job_id": "job_ab12…", "status": "awaiting_human", "gate": "approve_script",
  "question": "Approve the script, or request a revision.",
  "artifacts": { "script": "/jobs/job_ab12…/artifacts/script.md" } }
```
Save `job_id`. Show the script by fetching `{BASE_URL}{{ artifacts.script }}`.

### 2. Respond at each gate  (HTTP node, repeated)
```
POST {BASE_URL}/jobs/{{ job_id }}/respond
{
  "decision": "approve"          // or "revise"
  // optional: "answer": "make it punchier"
  // storyboard gate: "stills": ["/path/a.png","/path/b.png"]   (user-supplied stills)
  // clips gate:      "shots": [1,4]  with decision:"revise"     (regenerate only those)
}
```
Response is the same shape, advanced to the next `gate` (or `status:"done"`).

### 3. Show the artifact for the current gate
| gate | show the user | field |
|---|---|---|
| approve_script | the script text | `artifacts.script` (a .md URL) |
| approve_storyboard | the stills | `artifacts.stills` (list of image URLs) |
| approve_clips | the generated clips | `artifacts.clips` (list of mp4 URLs) |
| approve_final | the finished video | `artifacts.final` (mp4 URL) |

All artifact values are **paths relative to `{BASE_URL}`** — prepend the base to fetch/embed.

### 4. Loop until done
Keep alternating (show artifact → user decision → `/respond`) until the response has
`"status": "done"`. Then the final video is at `{BASE_URL}{{ artifacts.final }}`.

## Polling note (matters for the real agent)
- **Mock runner:** `/respond` returns the next gate immediately (work is instant).
- **Real agent (ClaudeCodeRunner on EC2):** generation takes minutes. `/respond` kicks off the
  next leg and returns quickly, but the job stays `running` until the agent hits the next gate.
  So after each `/respond`, Dify should **poll** `GET {BASE_URL}/jobs/{{ job_id }}` on an interval
  until `status` becomes `awaiting_human` (next gate ready) or `done`. Build this as a Dify
  loop/iteration node with a delay.

## Human-in-the-loop in Dify
Between a gate's artifact display and the `/respond` call, use Dify's user-input/conversation
step to collect **approve / request revision** (and any note). Map that to the `decision`
(+ optional `answer`, `stills`, `shots`) in the next `/respond` body.

## Quick curl smoke test (before wiring Dify)
```bash
BASE=https://dev.om.mvnoc.ai ; TOK=your-token
# start
curl -s -X POST $BASE/jobs -H "X-Dify-Token: $TOK" -H 'Content-Type: application/json' \
     -d '{"brief":"35s eSIM tip, panda mascot"}'
# then, using the job_id returned:
curl -s -X POST $BASE/jobs/<job_id>/respond -H "X-Dify-Token: $TOK" \
     -H 'Content-Type: application/json' -d '{"decision":"approve"}'
```

## Deploying the launcher (to get {BASE_URL})
On EC2 the launcher **replaces montage-svc on port 8501**, so `dev.om.mvnoc.ai` reaches it
with **no reverse-proxy change**. See `deploy/README.md` for the full steps; in short:
```bash
export DIFY_RUNNER=mock            # or 'claude' once the real agent is wired
export DIFY_TOKEN='<a long random secret>'
export DIFY_DATA_DIR=/opt/panda/data
sudo systemctl disable --now montage-svc     # free port 8501
uvicorn dify_launcher.app:app --host 127.0.0.1 --port 8501   # (systemd unit does this)
```
→ **`BASE_URL = https://dev.om.mvnoc.ai`** (proxy already forwards root → 8501).
