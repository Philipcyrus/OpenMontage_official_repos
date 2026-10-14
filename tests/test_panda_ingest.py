"""Tests for the job-history exporter and loader (deploy/panda_ingest*.py).

The unit tests build a synthetic job that mirrors the real on-box formats (launcher state,
checkpoints with history, timing.jsonl, agent logs, events.jsonl, the asset manifest, Claude
transcripts with streamed records, subagents and MCP logs) and plant secrets in it. No network,
no real Claude, Higgsfield or ElevenLabs calls. The PostgreSQL test runs only when
PANDA_INGEST_TEST_DSN points at a server where it may create and drop a scratch database.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ingest = _load("panda_ingest", ROOT / "deploy" / "panda_ingest.py")
loader = _load("panda_ingest_loader", ROOT / "deploy" / "panda_ingest_loader.py")

JOB = "job_0123456789ab"
OTHER_JOB = "job_ffffffffffff"
RUNNING_JOB = "job_aaaaaaaaaaaa"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
HF = "mcp__claude_ai_Higgsfield__"
PROVIDER_JOB = "5f0c2b8e-1d2a-4c3b-9e8f-7a6b5c4d3e2f"

# Secrets and private text planted in the sources; none may reach a batch.
FAKE_EL_KEY = "sk_" + "0123456789abcdef" * 3
FAKE_ANT_KEY = "sk-ant-api03-" + "Zz9" * 20
SIGNATURE = "SECRETSIGNATURE123"
THINKING = "PRIVATE-REASONING-MARKER"
EMAIL = "person.private@example.com"
AI_TITLE = "AI-TITLE-ONLY-MARKER"
READ_BODY = "READ-FILE-BODY-MARKER"
WRITE_BODY = "WRITE-FILE-BODY-MARKER"
CAT_BODY = "CAT-FILE-BODY-MARKER"
GREP_BODY = "GREP-MATCH-BODY-MARKER"
HEREDOC_BODY = "HEREDOC-FILE-BODY-MARKER"
FAL_KEY = "3f2a9c1e-4b5d-4e6f-8a7b-9c0d1e2f3a4b:0123456789abcdef0123456789abcdef"
BASE64_IMAGE = "iVBORw0KGgoAAAANSUhEUgAA" * 60
FORBIDDEN = (FAKE_EL_KEY, FAKE_ANT_KEY, SIGNATURE, THINKING, EMAIL, AI_TITLE, READ_BODY,
             WRITE_BODY, CAT_BODY, GREP_BODY, HEREDOC_BODY, FAL_KEY, BASE64_IMAGE[:200],
             "Key-Pair-Id=", "hunter2-db-password")


def ts(delta_s: float) -> str:
    return (T0 + timedelta(seconds=delta_s)).isoformat().replace("+00:00", "Z")


class Transcript:
    """Writes Claude Code JSONL records in the on-disk shape (one block per record)."""

    def __init__(self, path: Path, session_id: str, *, sidechain: bool = False) -> None:
        self.path = path
        self.session = session_id
        self.sidechain = sidechain
        self.lines: list = []
        self.parent = None
        self.last_at = 0.0

    def _base(self, rtype: str, at: float, **extra) -> dict:
        self.last_at = max(self.last_at, at)
        uid = str(uuid.uuid4())
        record = {"type": rtype, "uuid": uid, "parentUuid": self.parent,
                  "isSidechain": self.sidechain, "timestamp": ts(at),
                  "sessionId": self.session, "cwd": "/home/ec2-user/repo",
                  "version": "2.1.250", "gitBranch": "main", "userType": "external"}
        record.update(extra)
        self.parent = uid
        self.lines.append(record)
        return record

    def queue(self, at: float, prompt: str) -> None:
        self.lines.append({"type": "queue-operation", "operation": "enqueue",
                           "timestamp": ts(at), "sessionId": self.session, "content": prompt})

    def prompt(self, at: float, text: str, *, leg: bool = True) -> None:
        extra = {"permissionMode": "bypassPermissions", "promptSource": "sdk"} if leg else {}
        self._base("user", at, message={"role": "user", "content": text}, **extra)

    def assistant(self, at: float, message_id: str, blocks: list, usage: dict,
                  stop: str = "tool_use") -> list:
        records = []
        for block in blocks:
            records.append(self._base(
                "assistant", at, requestId=f"req_{message_id}",
                message={"id": message_id, "type": "message", "role": "assistant",
                         "model": "claude-sonnet-5", "content": [block],
                         "stop_reason": stop, "usage": usage}))
        return records

    def result(self, at: float, tool_use_id: str, content, *, is_error=None,
               tool_use_result=None, structured=None) -> None:
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error is not None:
            block["is_error"] = is_error
        extra = {}
        if tool_use_result is not None:
            extra["toolUseResult"] = tool_use_result
        if structured is not None:
            extra["mcpMeta"] = {"structuredContent": structured, "_meta": {}}
        self._base("user", at, message={"role": "user", "content": [block]}, **extra)

    def raw(self, record: dict) -> None:
        self.lines.append(record)

    def write(self, torn_tail: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(r) + "\n" for r in self.lines)
        if torn_tail:
            text += '{"type": "assistant", "uuid": "torn'          # still being written
        self.path.write_text(text, encoding="utf-8")
        at_file(self.path, self.last_at)                    # the file was last written then


def at_file(path: Path, delta_s: float) -> None:
    """Set a file's mtime to T0 + delta_s (files of the synthetic box are 'old')."""
    stamp = (T0 + timedelta(seconds=delta_s)).timestamp()
    os.utime(path, (stamp, stamp))


def age_tree(root: Path, delta_s: float) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            at_file(path, delta_s)


USAGE = {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 0, "service_tier": "standard",
         "output_tokens_details": {"thinking_tokens": 20}}


def tool(tool_id: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp,
            "caller": {"type": "direct"}}


def make_box(tmp_path: Path) -> "ingest.Settings":
    """A box with one finished-leg job, one job still running, and unrelated sessions."""
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    projects = tmp_path / "projects"
    claude = tmp_path / "claude" / "projects" / "-home-ec2-user-repo"
    mcp_dir = tmp_path / "cache" / "claude-cli-nodejs" / "-home-ec2-user-repo"
    job_dir = projects / JOB
    for sub in ("artifacts", "history", "assets/video", "assets/images", "renders"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)
    (data / "jobs" / JOB).mkdir(parents=True)
    repo.mkdir()

    # Launcher state: the transient snapshot of a /respond (revise) still being processed.
    (data / "jobs" / JOB / "state.json").write_text(json.dumps({
        "job_id": JOB, "brief": "A 30 s ad for eSIM travellers", "pipeline": "panda-video",
        "profile": "ugc", "options": {"language": "en", "max_higgsfield_credits": 200},
        "status": "running", "stage": "script", "gate": None,
        "question": "Command '['claude', '-p', 'project_id: job_0123456789ab Brief: secret "
                    "brief text']' timed out after 3600 seconds",
        "_processing_operation": "resume:approve_script", "_recovery_gate": "approve_script",
        "processing_started_at": ts(4798), "updated_at": ts(4798),
        "artifacts": {"_checkpoint_artifacts": {"huge": "x" * 100}, "final": "final.mp4"},
    }), encoding="utf-8")
    (job_dir / "project.json").write_text(json.dumps({
        "version": "1.0", "created_at": ts(-60), "project_id": JOB, "title": "A 30 s ad",
        "pipeline_type": "panda-video"}), encoding="utf-8")

    # Checkpoint history: script awaiting_human (agent, inside leg 1), then approved by the
    # launcher at 10:09:59 (outside every leg window).
    awaiting = {"version": "1.0", "project_id": JOB, "pipeline_type": "panda-video",
                "stage": "script", "status": "awaiting_human", "timestamp": ts(110),
                "checkpoint_policy": "guided", "human_approval_required": True,
                "human_approved": False, "artifacts": {"script": {"title": "Ad"}},
                "question": "Approve the script?"}
    (job_dir / "history" / "checkpoint_script_20261014T100150.json").write_text(
        json.dumps(awaiting), encoding="utf-8")
    approved = dict(awaiting, status="completed", human_approved=True, timestamp=ts(599))
    approved.pop("question")
    (job_dir / "checkpoint_script.json").write_text(json.dumps(approved), encoding="utf-8")
    (job_dir / "checkpoint_assets.json").write_text(json.dumps({
        "version": "1.0", "project_id": JOB, "pipeline_type": "panda-video", "stage": "assets",
        "status": "in_progress", "timestamp": ts(4200), "checkpoint_policy": "guided",
        "human_approval_required": True, "human_approved": False,
        "partial_progress": {"phase": "motion_and_audio_in_flight"},
        "artifacts": {"asset_manifest": {
            "version": "1.0", "total_cost_usd": 1.0,
            "assets": [{"id": "sc1-clip", "type": "video", "path": "assets/video/sc1.mp4",
                        "source_tool": "higgsfield_mcp_video", "scene_id": "sc1",
                        "provider": "higgsfield", "credits": 10.5, "credits_source": "actual",
                        "model": "seedance",
                        "original_url": f"https://cdn.example/sc1.mp4?Signature={SIGNATURE}"}],
            "metadata": {
                "lip_sync_qa": {"scenes": {"sc1": {
                    "status": "needs_visual_review", "selected_take": 1,
                    "attempts": [{"take": 1, "video_asset_id": "sc1-clip",
                                  "job_id": PROVIDER_JOB, "credits": 10.5}]}}},
                "timeline_contract": {"target_duration_seconds": 30, "status": "ok",
                                      "within_target_band": True, "scenes": [{}, {}]}}}}}),
        encoding="utf-8")
    (job_dir / "decision_log.json").write_text(json.dumps({
        "version": "1.0", "project_id": JOB, "decisions": [{
            "decision_id": "d-voice", "stage": "script", "category": "voice_selection",
            "subject": "narrator voice", "options_considered": [
                {"option_id": "a", "label": "Panda", "score": 0.9, "reason": "brand"}],
            "selected": "a", "reason": "brand voice", "confidence": 0.8}]}), encoding="utf-8")

    # Legs: script 10:00-10:02; assets_media 10:10-11:10 (attempt 0 exits rc=1, attempt 1 is
    # killed by the timeout and writes no log block); script_revise 11:20-11:25.
    (job_dir / "artifacts" / "timing.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
        {"ts": ts(120).replace("Z", "+00:00"), "stage": "script", "seconds": 120.0},
        {"ts": ts(4200).replace("Z", "+00:00"), "stage": "assets_media", "seconds": 3600.0},
        {"ts": ts(4800 + 300).replace("Z", "+00:00"), "stage": "script_revise", "seconds": 300.0},
    )), encoding="utf-8")
    (job_dir / "artifacts" / "agent_script.log").write_text(
        "\n===== attempt 0 rc=0 =====\n--- stdout ---\nScript written.\n", encoding="utf-8")
    (job_dir / "artifacts" / "agent_assets_media.log").write_bytes(
        b"\r\n===== attempt 0 rc=1 =====\r\n--- stdout ---\r\nAPI Error: 529 Overloaded\r\n")
    (job_dir / "artifacts" / "agent_script_revise.log").write_text(
        "\n===== attempt 0 rc=0 =====\n--- stdout ---\nRevised.\n", encoding="utf-8")
    (job_dir / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
        {"ts": ts(1000), "tool": "elevenlabs_tts", "event": "start"},
        {"ts": ts(1012.5), "tool": "elevenlabs_tts", "event": "finish", "success": True,
         "duration_s": 12.5, "scene_id": "sc1",
         "usage": {"platform": "elevenlabs", "unit": "characters", "amount": 640,
                   "source": "actual"}},
        {"ts": ts(4300), "tool": "video_compose", "event": "finish", "success": False,
         "duration_s": 200.0, "error": "render failed at https://x.example/a?token=abc"},
    )) + '{"ts": "torn', encoding="utf-8")
    (job_dir / "assets" / "video" / "sc1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42video")
    (job_dir / "assets" / "images" / "rejected_sc2_take1.png").write_bytes(b"\x89PNG\r\nreject")
    (job_dir / "renders" / "final.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42final")
    (job_dir / "assets" / "video" / "_lipsync_qa_pass1.json").write_text(
        json.dumps({"scenes": [{"scene": "sc1", "status": "needs_visual_review"}]}),
        encoding="utf-8")
    (job_dir / "assets" / "images" / "take#2.png").write_bytes(b"\x89PNG\r\nhash-in-name")
    age_tree(job_dir, 5100)
    age_tree(data, 5100)

    # Session A: leg 1 (script).
    a = Transcript(claude / "a1111111-0000-4000-8000-000000000001.jsonl",
                   "a1111111-0000-4000-8000-000000000001")
    prompt_a = f"project_id: {JOB}\nBrief: A 30 s ad for eSIM travellers\n"
    a.queue(1, prompt_a)
    a.raw({"type": "ai-title", "aiTitle": AI_TITLE, "sessionId": a.session})
    a.prompt(2, prompt_a)
    a.raw({"type": "attachment", "uuid": str(uuid.uuid4()), "timestamp": ts(2.5),
           "sessionId": a.session, "attachment": {"type": "session_context",
                                                  "userEmail": EMAIL}})
    a.raw({"type": "attachment", "uuid": str(uuid.uuid4()), "timestamp": ts(3),
           "sessionId": a.session, "attachment": {
               "type": "deferred_tools_delta", "addedNames": [HF + "balance"],
               "pendingMcpServers": [], "failedMcpServers": [],
               "needsAuthMcpServers": ["claude.ai Gmail"]}})
    first = a.assistant(10, "msg_a1", [
        {"type": "thinking", "thinking": THINKING, "signature": "sig"},
        {"type": "text", "text": "Reading the guide."},
        tool("toolu_read", "Read", {"file_path": "/home/ec2-user/repo/AGENT_GUIDE.md"}),
    ], USAGE)
    a.result(12, "toolu_read", f"{READ_BODY} {FAKE_ANT_KEY}",
             tool_use_result={"type": "text", "file": {"filePath": "AGENT_GUIDE.md"}})
    a.raw(dict(first[0]))                                  # resume copy of a record: skipped
    a.assistant(20, "msg_a2", [tool("toolu_write", "Write", {
        "file_path": "/home/ec2-user/repo/projects/x/script.json",
        "content": f"{WRITE_BODY} {FAKE_ANT_KEY}"})], USAGE)
    a.result(21, "toolu_write", "File created")
    a.assistant(30, "msg_a3", [tool("toolu_env", "Bash", {"command": "env"})], USAGE)
    env_dump = "\n".join([f"ELEVENLABS_API_KEY={FAKE_EL_KEY}", "HOME=/home/ec2-user",
                          "DIFY_TOKEN=abc", "PATH=/usr/bin", "DATABASE_URL=postgres://om:"
                          "hunter2-db-password@db/om", "USER=ec2-user"])
    a.result(31, "toolu_env", env_dump, tool_use_result={"stdout": env_dump, "stderr": "",
                                                         "interrupted": False})
    a.assistant(32, "msg_a6", [tool("toolu_cat", "Bash", {
        "command": "cat projects/x/inputs/requests.json"})], USAGE)
    a.result(33, "toolu_cat", json.dumps({"body": CAT_BODY, "FAL_KEY": FAL_KEY}),
             tool_use_result={"stdout": CAT_BODY, "stderr": "", "interrupted": False})
    heredoc = ("cat > projects/x/plan.json <<'EOF'\n" + json.dumps({"note": HEREDOC_BODY})
               + "\nEOF\necho done")
    a.assistant(34, "msg_a7", [tool("toolu_heredoc", "Bash", {"command": heredoc})], USAGE)
    a.result(35, "toolu_heredoc", "done")
    a.assistant(36, "msg_a8", [tool("toolu_grep", "Grep", {
        "pattern": "brief", "path": "projects", "output_mode": "content"})], USAGE)
    a.result(37, "toolu_grep", f"projects/x/brief.md:3:{GREP_BODY}")
    a.assistant(40, "msg_a4", [tool("toolu_png", "Read", {"file_path": "/x/still.png"})], USAGE)
    a.result(41, "toolu_png", [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": BASE64_IMAGE}}])
    a.raw({"type": "system", "subtype": "api_error", "level": "error", "uuid": str(uuid.uuid4()),
           "timestamp": ts(50), "sessionId": a.session,
           "error": {"message": "Overloaded", "status": 529}, "retryAttempt": 1,
           "maxRetries": 10, "retryInMs": 2000, "source": "request_retry"})
    a.raw({"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": ts(55),
           "sessionId": a.session, "isApiErrorMessage": True, "apiErrorStatus": 529,
           "message": {"id": "synthetic-1", "model": "<synthetic>", "role": "assistant",
                       "content": [{"type": "text", "text": "API Error: 529"}], "usage": None}})
    a.assistant(118, "msg_a5", [{"type": "text", "text": "Script ready for review."}], USAGE,
                stop="end_turn")
    a.write()

    # Session B: leg 2 attempt 0 (Higgsfield flow) plus a subagent; session C: attempt 1,
    # killed by the leg timeout with a call still open.
    b = Transcript(claude / "b2222222-0000-4000-8000-000000000002.jsonl",
                   "b2222222-0000-4000-8000-000000000002")
    prompt_b = f"For project_id: {JOB}, the STILLS phase was APPROVED."
    b.prompt(602, prompt_b)
    b.raw({"type": "attachment", "uuid": str(uuid.uuid4()), "timestamp": ts(603),
           "sessionId": b.session, "attachment": {
               "type": "deferred_tools_delta", "addedNames": ["Read"],
               "pendingMcpServers": ["claude.ai Higgsfield"], "failedMcpServers": [],
               "needsAuthMcpServers": []}})
    params = {"model": "seedance", "prompt": "panda waves", "duration": 5}
    b.assistant(610, "msg_b1", [tool("toolu_cost", HF + "generate_video",
                                     {"params": dict(params, get_cost=True)})], USAGE)
    cost = {"cost": {"credits": 10, "credits_exact": 10.5}}
    b.result(611, "toolu_cost", json.dumps(cost), structured=cost)
    b.assistant(620, "msg_b2", [tool("toolu_submit", HF + "generate_video",
                                     {"params": params})], USAGE)
    submitted = {"results": [{"id": PROVIDER_JOB, "type": "video", "status": "pending",
                              "model": "seedance"}], "adjustments": {"duration": {
                                  "requested": 5, "used": 6, "reason": "min"}}}
    b.result(622, "toolu_submit", json.dumps(submitted), structured=submitted)
    b.assistant(630, "msg_b3", [tool("toolu_rec", HF + "generate_video",
                                     {"params": dict(params, model="kling")})], USAGE)
    rec = {"notice": {"type": "preset_recommendation", "message": "use a preset", "data": {
        "preset": {"id": "p1", "name": "Cinematic",
                   "preview_url": "https://cdn.example/p.mp4?token=abc"}}}}
    b.result(631, "toolu_rec", json.dumps(rec), structured=rec)
    wait_in = {"jobs": [{"index": 0, "job_id": PROVIDER_JOB}], "timeout_seconds": 15}
    b.assistant(640, "msg_b4", [tool("toolu_wait1", HF + "jobs_wait", wait_in)], USAGE)
    running = {"timed_out": True, "all_terminal": False, "poll_after_seconds": 10,
               "summary": {"total": 1, "active": 1, "completed": 0, "failed": 0, "errors": 0},
               "jobs": [{"index": 0, "job_id": PROVIDER_JOB, "status": "in_progress"}]}
    b.result(656, "toolu_wait1", json.dumps(running), structured=running)
    b.assistant(700, "msg_b5", [tool("toolu_wait2", HF + "jobs_wait", wait_in)], USAGE)
    done = {"all_terminal": True,
            "summary": {"total": 1, "active": 0, "completed": 1, "failed": 0, "errors": 0},
            "jobs": [{"index": 0, "job_id": PROVIDER_JOB, "status": "completed",
                      "result_url": f"https://cdn.higgsfield.ai/r.mp4?Expires=1&Signature="
                                    f"{SIGNATURE}&Key-Pair-Id=K1"}]}
    b.result(710, "toolu_wait2", json.dumps(done), structured=done)
    b.assistant(720, "msg_b6", [tool("toolu_curl", "Bash", {
        "command": f"curl -H 'xi-api-key: {FAKE_EL_KEY}' -o sc1.mp4 "
                   f"'https://cdn.higgsfield.ai/r.mp4?Signature={SIGNATURE}'"})], USAGE)
    b.result(730, "toolu_curl", "Exit code 22\ncurl: (22) 403", is_error=True,
             tool_use_result={"stdout": "", "stderr": "403", "interrupted": False})
    b.assistant(1199, "msg_b7", [{"type": "text", "text": "Stopping; overloaded."}], USAGE,
                stop="end_turn")
    b.write(torn_tail=True)
    sub = Transcript(claude / b.session / "subagents" / "agent-1.jsonl", b.session,
                     sidechain=True)
    sub.prompt(650, "Check the clip framing.")
    sub.assistant(651, "msg_sub1", [{"type": "text", "text": "Framing is fine."}], USAGE,
                  stop="end_turn")
    sub.write()
    c = Transcript(claude / "c3333333-0000-4000-8000-000000000003.jsonl",
                   "c3333333-0000-4000-8000-000000000003")
    c.prompt(1805, prompt_b)
    c.assistant(1810, "msg_c1", [tool("toolu_hang", HF + "jobs_wait", wait_in)], USAGE)
    c.write()

    # Session D: leg 3, a revise leg whose prompt carries the reviewer's note.
    d = Transcript(claude / "d4444444-0000-4000-8000-000000000004.jsonl",
                   "d4444444-0000-4000-8000-000000000004")
    d.prompt(4802, f"Revise stage 'script' for project_id: {JOB} per this feedback: shorter "
                   f"intro please. Regenerate only shots [2].")
    d.assistant(4810, "msg_d1", [{"type": "text", "text": "Revised."}], USAGE, stop="end_turn")
    d.write()

    # Noise in the same folder: an interactive session and another job's leg.
    e = Transcript(claude / "e5555555-0000-4000-8000-000000000005.jsonl",
                   "e5555555-0000-4000-8000-000000000005")
    e.prompt(100, f"why did {JOB} fail?")
    e.write()
    f = Transcript(claude / "f6666666-0000-4000-8000-000000000006.jsonl",
                   "f6666666-0000-4000-8000-000000000006")
    f.prompt(100, f"project_id: {OTHER_JOB}\nBrief: other")
    f.write()

    # A job still running: its leg has no timing line yet.
    (projects / RUNNING_JOB / "artifacts").mkdir(parents=True)
    g = Transcript(claude / "a7777777-0000-4000-8000-000000000007.jsonl",
                   "a7777777-0000-4000-8000-000000000007")
    live = (NOW - T0).total_seconds()
    g.prompt(live - 300, f"project_id: {RUNNING_JOB}\nBrief: live")
    g.assistant(live - 290, "msg_g1", [tool("toolu_g", "Bash", {"command": "ls"})], USAGE)
    g.result(live - 289, "toolu_g", "file.txt")
    g.write()

    # MCP log for session B: one call, a connection error and a drop.
    log_dir = mcp_dir / "mcp-logs-claude-ai-Higgsfield"
    log_dir.mkdir(parents=True)
    (log_dir / "2026-10-14T10-10-00.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
        {"debug": "Calling MCP tool: generate_video", "timestamp": ts(620), "sessionId": b.session},
        {"debug": "Tool generate_video completed successfully in 2100ms", "timestamp": ts(622),
         "sessionId": b.session},
        {"error": "Connection error: getaddrinfo ENOTFOUND mcp-proxy.anthropic.com",
         "timestamp": ts(900), "sessionId": b.session},
        {"debug": "CLAUDEAI-PROXY connection dropped after 300s uptime", "timestamp": ts(901),
         "sessionId": b.session},
        {"debug": "CLAUDEAI-PROXY connection closed after 590s (cleanly)", "timestamp": ts(1199),
         "sessionId": b.session},
    )), encoding="utf-8")
    age_tree(mcp_dir, 1199)

    return ingest.Settings(
        repo=repo, data_dir=data, projects_dir=projects, transcript_dirs=[claude],
        mcp_log_dirs=[mcp_dir], spool=tmp_path / "spool", state_dir=tmp_path / "state",
        host_label="test-box", ffprobe=None)


def build(settings, job=JOB, now=NOW):
    state: dict = {}
    transcripts = ingest.discover_transcripts(settings, state)
    logs = ingest.discover_mcp_logs(settings, state)
    return ingest.build_all_rows(job, settings, transcripts, logs, state, now)


def rows_of(ctx, table):
    return [row for t, row in ctx.rows.items() if t == table]


def events(ctx, name):
    return [r for r in rows_of(ctx, "runtime_events") if r["event_name"] == name]


@pytest.fixture()
def box(tmp_path):
    return make_box(tmp_path)


@pytest.fixture()
def ctx(box):
    return build(box)


# --------------------------------------------------------------------------------- redaction

@pytest.mark.parametrize("text, leaked", [
    (f"export ANTHROPIC_API_KEY={FAKE_ANT_KEY}", FAKE_ANT_KEY),
    (f"curl -H 'xi-api-key: {FAKE_EL_KEY}' https://api.elevenlabs.io", FAKE_EL_KEY),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnopqrstuvwxyz0123"),
    ("https://d1.cloudfront.net/a.mp4?Expires=1&Signature=SIG&Key-Pair-Id=K", "Signature"),
    ("https://storage.googleapis.com/b/o?X-Goog-Signature=abc", "X-Goog"),
    ("https://acct.blob.core.windows.net/c/b?sv=1&sig=abc", "sig=abc"),
    ("https://dify.example/files/x?timestamp=1&nonce=2&sign=abc", "sign=abc"),
    ("postgres://om:hunter2-db-password@db:5432/om", "hunter2-db-password"),
    ("https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX", "XXXXXXXXXXXX"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----", "MIIEvQIBADANBg"),
    ("data:image/png;base64," + BASE64_IMAGE, BASE64_IMAGE[:100]),
    ("token value Zq8Lx2Vb9Nw4Rt6Yp1Ks3Df5Gh7Jk0Mn2Bv4Cx6Za8Qw", "Zq8Lx2Vb9Nw4"),
    ("\n".join(f"VAR{i}=value{i}secret" for i in range(6)), "value3secret"),
])
def test_redactor_removes_secrets(text, leaked):
    assert leaked not in ingest.Redactor().text(text)


@pytest.mark.parametrize("text", [
    f"projects/{JOB}/checkpoint_assets.json",
    "session d36a6209-a271-42ad-9237-efe7bb8e7a8d",
    "sha 1366cf835077ad3c014b6885c3fca4c85507bf05d61f0994072287e3d12f9fe3",
    "The token budget was exceeded; use fewer tokens next time.",
    "toolu_01LcwJzACRhSH7s5BzyYG5Fy msg_011CfZtQUHQWB77Lwg9p3Jo9",
    "https://mcp.higgsfield.ai/mcp",
    "ANTHROPIC_API_KEY=\nANTHROPIC_BASE_URL=",
])
def test_redactor_keeps_ordinary_text(text):
    assert ingest.Redactor().text(text) == text


def test_redactor_json_masks_secret_keys_but_not_token_counts():
    red = ingest.Redactor()
    out = red.json({"api_key": "abc123def456", "usage": {"output_tokens": 5},
                    "headers": {"Authorization": "Bearer xyzxyzxyzxyzxyzxyz"},
                    "source": {"data": "A" * 500}})
    assert out["api_key"] == "[redacted]"
    assert out["usage"]["output_tokens"] == 5
    assert out["headers"]["Authorization"] == "[redacted]"
    assert "AAAA" not in out["source"]["data"]


@pytest.mark.parametrize("question, expected", [
    ("Command '['claude', '-p', 'Brief: secret']' timed out after 3600 seconds",
     "Command [claude command line omitted] timed out after 3600 seconds"),
    ("Background processing stopped before the next gate (error: Command '['claude', '-p', "
     "'project_id: job_x Brief: secret']' timed out after 3600 seconds). Approve to resume.",
     "Background processing stopped before the next gate (error: Command [claude command line "
     "omitted] timed out after 3600 seconds). Approve to resume."),
    ("Command '['claude', '-p', 'Brief: secret']' returned non-zero exit status 1.",
     "Command [claude command line omitted] returned non-zero exit status 1."),
    ("Approve the script?", "Approve the script?"),
])
def test_timeout_question_keeps_no_command_line(question, expected):
    assert ingest.summarize_question(question) == expected


# ------------------------------------------------------------------------------ small parsers

@pytest.mark.parametrize("label, stage", [
    ("script", "script"), ("start_continue_2", "script"), ("scene_plan_revise", "scene_plan"),
    ("assets_media", "assets"), ("assets_media_continue_3", "assets"),
    ("stills_after_hero_2", "assets"), ("hero_still_revise", "assets"),
    ("motion_sample_revise", "assets"), ("assets_media_pacing_auto_1", "assets"),
    ("edit", "edit"), ("edit_continue_1", "edit"), ("compose_revise", "compose"),
])
def test_stage_of_label(label, stage):
    assert ingest.stage_of_label(label) == stage


def test_agent_log_groups_attempts_into_legs_and_handles_crlf():
    text = ("\r\n===== attempt 0 rc=1 =====\r\n--- stdout ---\r\n529\r\n"
            "\r\n===== attempt 1 rc=0 =====\r\n--- stdout ---\r\nok\r\n"
            "\n===== attempt 0 rc=? =====\n--- stderr ---\nboom\n")
    legs = ingest.parse_agent_log(text)
    assert [[a["rc"] for a in leg] for leg in legs] == [[1, 0], [None]]
    assert legs[1][0]["stderr"] == "boom"


def test_prompt_job_id_is_the_first_project_id():
    info_text = f"project_id: {JOB}\nBrief: compare with project_id: {OTHER_JOB}"
    assert ingest.PROMPT_JOB_RE.search(info_text).group(1) == JOB


# ------------------------------------------------------------------ Higgsfield classification

def _classify(name, inp, payload, is_error=False, text=""):
    return ingest.classify_tool(HF + name, inp, payload, text or json.dumps(payload), is_error)


def test_jobs_wait_still_running_is_not_success():
    out = _classify("jobs_wait", {"jobs": [{"job_id": PROVIDER_JOB}], "timeout_seconds": 15},
                    {"timed_out": True, "all_terminal": False, "summary": {"active": 1}})
    assert out.outcome == "timed_out_nonterminal" and out.job_ids == [PROVIDER_JOB]
    assert out.attrs["wait_budget_seconds"] == 15


def test_jobs_wait_terminal_and_malformed():
    done = _classify("jobs_wait", {"jobs": [{"job_id": PROVIDER_JOB}]},
                     {"all_terminal": True, "summary": {"completed": 1, "failed": 0}})
    assert done.outcome == "completed"
    bad = _classify("jobs_wait", {"jobIds": PROVIDER_JOB}, None, is_error=True,
                    text="Error: invalid arguments")
    assert bad.outcome == "malformed_arguments"


@pytest.mark.parametrize("payload, outcome", [
    ({"cost": {"credits": 10, "credits_exact": 10.5}}, "cost_preflight"),
    ({"notice": {"type": "preset_recommendation", "data": {"preset": {"name": "X"}}}},
     "recommendation_only"),
    ({"results": [{"id": PROVIDER_JOB, "status": "pending"}]}, "submitted"),
    ({}, "no_job_returned"),
])
def test_generate_video_outcomes(payload, outcome):
    out = _classify("generate_video", {"params": {"model": "m"}}, payload)
    assert out.outcome == outcome
    if outcome == "cost_preflight":
        assert out.credits == 10.5


def test_generate_params_sent_as_string_is_malformed():
    out = _classify("generate_image", {"params": '{"model": "x"}'}, None, is_error=True,
                    text="error: bad request")
    assert out.outcome == "malformed_arguments"


def test_batch_media_and_status_outcomes():
    batch = _classify("generate_video_batch", {"requests": [{}, {}]},
                      {"submitted_count": 2, "failed_count": 0,
                       "jobs": [{"job_id": PROVIDER_JOB}, {"job_id": "a" * 20}]})
    assert batch.outcome == "submitted" and len(batch.job_ids) == 2
    assert _classify("media_confirm", {}, {"results": [{"status": "uploaded"}]}).outcome == \
        "media_confirmed"
    status = _classify("job_status", {"jobId": PROVIDER_JOB},
                       {"generation": {"id": PROVIDER_JOB, "status": "in_progress"}})
    assert status.outcome == "nonterminal" and status.relation == "polls"


def test_bash_timeout_and_exit_code():
    timeout = ingest.classify_tool("Bash", {}, None, "", False, {"timedOutAfterMs": 120000})
    assert timeout.outcome == "timeout"
    failed = ingest.classify_tool("Bash", {}, None, "Exit code 22\n403", True, {})
    assert failed.outcome == "error" and failed.attrs["exit_code"] == 22


# ------------------------------------------------------------------------ whole-job building

def test_no_secret_or_private_text_reaches_any_row(ctx):
    blob = json.dumps(ctx.rows.items(), default=str)
    for marker in FORBIDDEN:
        assert marker not in blob, marker
    assert "secret brief text" not in json.dumps(rows_of(ctx, "jobs"))


def test_sessions_map_to_legs_with_attempts(ctx):
    runs = {r["run_key"]: r for r in rows_of(ctx, "agent_runs")}
    assert set(runs) == {"script:0:0", "assets_media:0:0", "assets_media:0:1",
                         "script_revise:0:0"}
    assert runs["script:0:0"]["status"] == "completed"
    assert runs["assets_media:0:0"]["status"] == "failed"
    assert runs["assets_media:0:0"]["exit_code"] == 1
    killed = runs["assets_media:0:1"]
    assert killed["status"] == "timed_out"                 # killed by the leg timeout
    assert killed["timeout_seconds"] == 3600 and killed["error_type"] == "leg_timeout"
    assert killed["claude_session_id"] == "c3333333-0000-4000-8000-000000000003"
    assert killed["metadata_redacted"]["unanswered_tool_calls"] == 1
    assert runs["script:0:0"]["attempt_no"] == 1
    sessions = {r["claude_session_id"] for r in rows_of(ctx, "llm_blocks")}
    assert "e5555555-0000-4000-8000-000000000005" not in sessions     # interactive session
    assert "f6666666-0000-4000-8000-000000000006" not in sessions     # another job


def test_streamed_records_make_one_inference_with_last_usage(ctx):
    infs = {r["model_message_id"]: r for r in rows_of(ctx, "llm_inferences")}
    a1 = infs["msg_a1"]
    assert a1["block_count"] == 3 and a1["output_tokens"] == 50 and a1["thinking_tokens"] == 20
    assert "synthetic-1" not in infs
    run = next(r for r in rows_of(ctx, "agent_runs") if r["run_key"] == "script:0:0")
    assert run["inference_count"] == 8 and run["output_tokens"] == 400
    assert events(ctx, "claude.api.error") and events(ctx, "claude.api.retry")


def test_duplicate_records_and_torn_lines_are_skipped(ctx):
    a_blocks = [r for r in rows_of(ctx, "llm_blocks")
                if r["claude_session_id"].startswith("a1111111")]
    assert len({r["source_uuid"] for r in a_blocks}) == len({
        (r["source_uuid"], r["block_index"]) for r in a_blocks})
    b_blocks = [r for r in rows_of(ctx, "llm_blocks")
                if r["claude_session_id"].startswith("b2222222") and not r["is_sidechain"]]
    assert all(r["source_uuid"] != "torn" for r in b_blocks)


def test_file_contents_and_reasoning_are_omitted(ctx):
    blocks = rows_of(ctx, "llm_blocks")
    read = next(r for r in blocks if r["tool_use_id"] == "toolu_read"
                and r["block_type"] == "tool_result")
    assert read["content_omitted_reason"] == "file_content" and read["content_text_redacted"] is None
    write = next(r for r in blocks if r["tool_use_id"] == "toolu_write"
                 and r["block_type"] == "tool_use")
    assert write["content_omitted_reason"] == "file_content"
    assert write["content_json_redacted"]["content_chars"] > 0
    thinking = next(r for r in blocks if r["block_type"] == "thinking")
    assert thinking["content_text_redacted"] is None and thinking["content_bytes"] > 0


def test_higgsfield_outcomes_spans_links_and_quoted_cost(ctx):
    spans = {r["tool_use_id"]: r for r in rows_of(ctx, "trace_spans")
             if r["span_kind"] == "tool_call" and r["tool_use_id"]}
    assert spans["toolu_cost"]["semantic_outcome"] == "cost_preflight"
    assert spans["toolu_submit"]["semantic_outcome"] == "submitted"
    assert spans["toolu_submit"]["attributes_redacted"]["parameters_adjusted"] == ["duration"]
    assert spans["toolu_rec"]["semantic_outcome"] == "recommendation_only"
    assert spans["toolu_wait1"]["semantic_outcome"] == "timed_out_nonterminal"
    assert spans["toolu_wait1"]["transport_status"] == "success"
    assert spans["toolu_wait2"]["semantic_outcome"] == "completed"
    assert spans["toolu_curl"]["transport_status"] == "error"
    assert spans["toolu_wait1"]["duration_ms"] == 16_000
    job_hash = ingest.hash_id(PROVIDER_JOB)
    provider = [r for r in rows_of(ctx, "trace_spans") if r["span_kind"] == "provider_job"]
    assert len(provider) == 1 and provider[0]["provider_job_id_hash"] == job_hash
    assert provider[0]["semantic_outcome"] == "completed"
    relations = sorted(r["relation"] for r in rows_of(ctx, "span_links") if r["to_span_id"])
    assert relations == ["creates", "polls", "polls"]
    produces = [r for r in rows_of(ctx, "span_links") if r["relation"] == "produces"]
    assert len(produces) == 1 and produces[0]["to_artifact_id"]
    quotes = [r for r in rows_of(ctx, "cost_observations")
              if r["usage_kind"] == "quoted_submission_credits"]
    assert len(quotes) == 1 and quotes[0]["amount"] == 10.5


def test_approvals_from_checkpoint_history_and_revise_prompt(ctx):
    approvals = {r["decision"]: r for r in rows_of(ctx, "approval_events")}
    approve = approvals["approve"]
    assert approve["provenance"] == "legacy_inferred"
    assert approve["received_at"] == "2026-10-14T10:09:59+00:00"
    assert approve["metadata_redacted"]["written_by"] == "launcher"
    revise = approvals["revise"]
    assert revise["answer_redacted"] == "shorter intro please"
    response = events(ctx, "gate.response_received")
    assert response and response[0]["attributes_redacted"]["gate"] == "approve_script"
    assert response[0]["outcome"] == "revise_or_other"


def test_legs_gaps_and_tool_runs(ctx):
    gaps = [r for r in rows_of(ctx, "trace_spans") if r["span_kind"] == "gap"]
    first = min(gaps, key=lambda r: r["started_at"])
    assert first["semantic_outcome"] == "likely_human_gate" and first["duration_ms"] == 480_000
    runs = [r for r in rows_of(ctx, "trace_spans") if r["source_service"] == "openmontage_tool"]
    assert {r["name"] for r in runs} == {"elevenlabs_tts", "video_compose"}
    usage = [r for r in rows_of(ctx, "cost_observations") if r["provider"] == "elevenlabs"]
    assert usage and usage[0]["amount"] == 640 and usage[0]["unit"] == "characters"
    assert events(ctx, "tool.outlived_leg")
    timing = rows_of(ctx, "timing_events")
    assert len(timing) == 3 and {t["stage"] for t in timing} == {"script", "assets_media",
                                                                  "script_revise"}


def test_manifest_qa_mcp_and_connector_events(ctx):
    lipsync = events(ctx, "qa.lipsync.scene")
    assert lipsync and lipsync[0]["scene_id"] == "sc1"
    assert events(ctx, "audio.timeline_contract")
    assert events(ctx, "mcp.connection.error") and events(ctx, "mcp.connection.dropped")
    summary = events(ctx, "mcp.log.summary")[0]["attributes_redacted"]
    assert summary["calls"] == 1 and summary["longest_call_ms"] == 2100
    status = {e["agent_run_id"]: e for e in events(ctx, "claude.mcp.status")}
    outcomes = sorted(e["outcome"] for e in status.values())
    assert outcomes == ["higgsfield_connected"] * 3            # sessions A, B and C
    assert len(events(ctx, "mcp.connection.dropped")) == 1     # a clean close is no drop
    assert summary["drops"] == 1
    manifest = [r for r in rows_of(ctx, "cost_observations")
                if r["usage_kind"] == "manifest_credits"]
    assert len(manifest) == 1 and manifest[0]["amount"] == 10.5


def test_artifacts_selection_and_rejected_takes(ctx):
    arts = {r["logical_id"]: r for r in rows_of(ctx, "artifacts")}
    assert arts["projects/renders/final.mp4"]["object_uri"] == f"projects/{JOB}/renders/final.mp4"
    assert arts["projects/assets/images/rejected_sc2_take1.png"]["artifact_status"] == "rejected"
    assert arts["projects/assets/video/sc1.mp4"]["selected"] is True
    assert arts["projects/assets/video/sc1.mp4"]["provider"] == "higgsfield"
    assert all("?" not in a["object_uri"] for a in arts.values())
    bookkeeping = [k for k in arts if k.endswith(("events.jsonl", "timing.jsonl", "project.json",
                                                  "decision_log.json")) or "/agent_" in k]
    assert not bookkeeping                          # read as sources, not catalogued


def test_job_projection_and_state_snapshot(ctx):
    job = rows_of(ctx, "jobs")[0]
    assert job["current_status"] == "running" and job["pipeline"] == "panda-video"
    assert job["current_question"] == ("Command [claude command line omitted] timed out after "
                                       "3600 seconds")
    snap = rows_of(ctx, "job_state_observations")[0]
    assert "_checkpoint_artifacts" not in snap["state_redacted"]["artifacts"]
    assert "brief" not in snap["state_redacted"]


def test_running_job_defers_totals_until_the_leg_ends(box):
    ctx = build(box, job=RUNNING_JOB)
    assert rows_of(ctx, "llm_blocks")                       # messages stream in at once
    assert not rows_of(ctx, "llm_inferences")
    assert not rows_of(ctx, "agent_runs")
    assert not [r for r in rows_of(ctx, "trace_spans") if r["span_kind"] == "claude_session"]
    later = build(box, job=RUNNING_JOB, now=NOW + timedelta(hours=3))
    assert rows_of(later, "llm_inferences") and rows_of(later, "agent_runs")
    assert rows_of(later, "agent_runs")[0]["status"] == "no_timing_row"


def test_every_row_has_its_table_primary_key_and_valid_keys(ctx):
    for table, row in ctx.rows.items():
        assert row[ingest.PRIMARY_KEYS[table]]
        if table == "agent_runs":
            assert row["attempt_no"] >= 1
        if table == "trace_spans":
            assert row["transport_status"] in {"started", "success", "error", "timeout",
                                               "cancelled", "unknown"}


# ----------------------------------------------------------------------- export + spool

def _run(settings, **kwargs):
    return ingest.run_export(settings, only=None, full=False, dry_run=None, **kwargs)


def _batches(settings):
    return sorted((settings.spool / "pending").rglob("*.jsonl.gz"))


def test_export_is_incremental_and_batches_validate(box, capsys):
    box.spool.mkdir()
    assert _run(box) == 0
    first = _batches(box)
    assert {p.parent.name for p in first} == {JOB, RUNNING_JOB}
    for path in first:
        header, rows = loader.read_batch(path)
        assert header["row_count"] == len(rows)
        raw = gzip.open(path).read().decode("utf-8")
        for marker in FORBIDDEN:
            assert marker not in raw
    assert _run(box) == 0
    assert _batches(box) == first                            # nothing changed, nothing new
    transcript = box.transcript_dirs[0] / "d4444444-0000-4000-8000-000000000004.jsonl"
    record = {"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": ts(4900),
              "sessionId": "d4444444-0000-4000-8000-000000000004",
              "message": {"id": "msg_d2", "role": "assistant", "model": "m",
                          "content": [{"type": "text", "text": "One more line."}],
                          "usage": USAGE}}
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    assert _run(box) == 0
    new = [p for p in _batches(box) if p not in first]
    assert len(new) == 1
    _header, rows = loader.read_batch(new[0])
    tables = {t for t, _r in rows}
    assert "llm_blocks" in tables and "llm_inferences" in tables
    assert len([r for t, r in rows if t == "llm_blocks"]) == 1
    capsys.readouterr()


def test_tampered_or_foreign_batches_are_rejected(box, tmp_path):
    box.spool.mkdir()
    _run(box)
    path = _batches(box)[0]
    data = gzip.open(path).read().split(b"\n")
    data[1] = data[1].replace(b"\"job_id\"", b"\"job_idx\"", 1)
    tampered = tmp_path / "tampered.jsonl.gz"
    tampered.write_bytes(gzip.compress(b"\n".join(data)))
    with pytest.raises(loader.BatchError, match="checksum"):
        loader.read_batch(tampered)
    header = json.loads(data[0])
    body = [json.dumps({"table": "pg_authid", "row": {"x": 1}}).encode()]
    header.update(row_count=1, rows_sha256=__import__("hashlib").sha256(b"\n".join(body)).hexdigest())
    foreign = tmp_path / "foreign.jsonl.gz"
    foreign.write_bytes(gzip.compress(json.dumps(header).encode() + b"\n" + body[0] + b"\n"))
    with pytest.raises(loader.BatchError, match="unknown table"):
        loader.read_batch(foreign)


def test_loader_rejects_unknown_or_unsafe_columns():
    columns = {"job_id": "text"}
    with pytest.raises(loader.EnvironmentProblem, match="does not exist"):
        loader._check_row("jobs", {"job_id": JOB, "nope": 1}, columns)   # a missing migration
    with pytest.raises(loader.RowValueError, match="invalid column"):
        loader._check_row("jobs", {"job_id": JOB, "x; drop": 1}, dict(columns, **{"x; drop": "t"}))


def test_db_error_text_drops_row_details():
    class Diag:
        message_primary = "new row violates check constraint"
        sqlstate = "23514"

    class Err(Exception):
        diag = Diag()

    text = loader._db_error(Err("ERROR: x\nDETAIL: Failing row contains (secret)"))
    assert "secret" not in text and "23514" in text


# ============================================================================ review fixes
# One small job per test: legs, agent logs, sessions and files are added by each test.

SMALL = "job_00000000000a"
LATER = T0 + timedelta(hours=10)


def small_box(tmp_path: Path, *, status: str = "done", options: dict = None,
              pipeline: str = "panda-video", timing=(), logs: dict = None,
              events_lines=None, extra_state: dict = None):
    data = tmp_path / "data"
    projects = tmp_path / "projects"
    claude = tmp_path / "claude" / "projects" / "-home-ec2-user-repo"
    mcp_dir = tmp_path / "cache" / "claude-cli-nodejs" / "-home-ec2-user-repo"
    job_dir = projects / SMALL
    (job_dir / "artifacts").mkdir(parents=True)
    (data / "jobs" / SMALL).mkdir(parents=True)
    claude.mkdir(parents=True)
    mcp_dir.mkdir(parents=True)
    state = {"job_id": SMALL, "brief": "b", "pipeline": pipeline, "status": status,
             "options": options or {}, "updated_at": ts(9000)}
    state.update(extra_state or {})
    (data / "jobs" / SMALL / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (job_dir / "artifacts" / "timing.jsonl").write_text("".join(
        json.dumps({"ts": ts(end), "stage": label, "seconds": seconds}) + "\n"
        for label, end, seconds in timing), encoding="utf-8")
    for label, text in (logs or {}).items():
        (job_dir / "artifacts" / f"agent_{label}.log").write_text(text, encoding="utf-8")
    if events_lines:
        (job_dir / "events.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in events_lines), encoding="utf-8")
    age_tree(job_dir, 9000)
    age_tree(data, 9000)
    settings = ingest.Settings(
        repo=tmp_path / "repo", data_dir=data, projects_dir=projects, transcript_dirs=[claude],
        mcp_log_dirs=[mcp_dir], spool=tmp_path / "spool", state_dir=tmp_path / "state",
        host_label="test-box", ffprobe=None)
    return settings, job_dir, claude


def block(attempt: int, rc, out: str = "") -> str:
    return f"\n===== attempt {attempt} rc={rc} =====\n--- stdout ---\n{out}\n"


def sid_of(char: str) -> str:
    return f"{char * 8}-{char * 4}-4{char * 3}-8{char * 3}-{char * 12}"


def leg_session(claude: Path, char: str, start: float, end: float, prompt: str = None, *,
                leg: bool = True) -> "Transcript":
    t = Transcript(claude / f"{sid_of(char)}.jsonl", sid_of(char))
    t.prompt(start, prompt or f"For project_id: {SMALL}, continue the assets stage.", leg=leg)
    t.assistant(end, f"msg_{char}", [{"type": "text", "text": "done"}], USAGE, stop="end_turn")
    t.write()
    return t


def runs_by_key(ctx) -> dict:
    return {r["run_key"]: r for r in rows_of(ctx, "agent_runs")}


# ----------------------------------------------------------------------- redaction, more shapes

@pytest.mark.parametrize("text, leaked", [
    ("{'DIFY_TOKEN': 'dfy_9f8e7d6c5b4a39281706'}", "dfy_9f8e7d6c5b4a39281706"),
    ('curl -d "{\\"api_key\\":\\"k-123456789abcdefgh\\"}"', "k-123456789abcdefgh"),
    ("headers={'X-Panda-Token': 'tok_0a1b2c3d4e5f'}", "tok_0a1b2c3d4e5f"),
    ("\n".join(["declare -x A1=1", "declare -x A2=2", "declare -x SMTP_PASS=hunter2pass",
                "declare -x A4=4", "declare -x A5=5"]), "hunter2pass"),
    ("mysqldump --password s3cretpass db", "s3cretpass"),
    ("tool --api-key=abcdefgh12345678 run", "abcdefgh12345678"),
    ("curl -u admin:s3cretpass https://x.example/a", "s3cretpass"),
    ("aws configure set aws_secret_access_key AbCdEfGhIjKlMnOp1234", "AbCdEfGhIjKlMnOp1234"),
    ("https%3A%2F%2Fcdn.example%2Fa.mp4%3FExpires%3D1%26Signature%3DSECRETSIG9", "SECRETSIG9"),
    ("postgresql://om:pa/ss-word@db:5432/om", "pa/ss-word"),
    ("https://user:pa/ss-word@host.example/x", "pa/ss-word"),
])
def test_redactor_removes_more_secret_shapes(text, leaked):
    assert leaked not in ingest.Redactor().text(text)


@pytest.mark.parametrize("value, leaked", [
    ({"FAL_KEY": FAL_KEY}, FAL_KEY),
    ({"AZURE_SPEECH_KEY": "0123456789abcdef0123456789abcdef"}, "0123456789abcdef0123"),
    ({"SMTP_PASS": "hunter2pass"}, "hunter2pass"),
    ({"secret_key": "abc123secret"}, "abc123secret"),
    ({"key": "k_9f8e7d6c5b4a3928"}, "k_9f8e7d6c5b4a3928"),
    ({"https://cdn.example/a.mp4?Expires=1&Signature=SIGKEY1": "assets/a.mp4"}, "SIGKEY1"),
    ({"PATH": "/usr/bin", "HOME": "/home/x", "USER": "x", "LANG": "C",
      "MY_VALUE": "visible-zz-value"}, "visible-zz-value"),
    (["PATH=/usr/bin", "HOME=/h", "USER=u", "LANG=C", "OTHER=plain-secret-zz"],
     "plain-secret-zz"),
])
def test_redactor_json_masks_keys_values_and_env_dumps(value, leaked):
    assert leaked not in json.dumps(ingest.Redactor().json(value))


def test_redactor_json_keeps_ordinary_keys():
    value = {"keyframes": 12, "scene_key": "sc1", "cache_key": "assets/video/sc1.mp4",
             "tokens": 5, "passes": 3, "status": "completed"}
    assert ingest.Redactor().json(value) == value


def test_nul_and_lone_surrogates_are_replaced():
    assert ingest.Redactor().text("a\x00b\ud83d") == "a�b�"
    assert ingest.clean_value({"k\x00": ["\ud800", 1, {"x": "y\x00"}]}) == \
        {"k�": ["�", 1, {"x": "y�"}]}
    row = {"text": "head -c 8 out: \x00\x00", "emoji": "cut \ud83d"}
    body = json.dumps(ingest.clean_value(row), ensure_ascii=False).encode("utf-8")
    assert b"\\u0000" not in body


def test_heredoc_bodies_are_replaced_by_size_and_hash():
    command = "cat > a.json <<'EOF'\n{\"x\": 1}\nEOF\npython3 - <<EOF2\nprint(1)\nEOF2\nls"
    stripped, count = ingest.strip_heredocs(command)
    assert count == 2 and "{\"x\": 1}" not in stripped and "print(1)" not in stripped
    assert stripped.startswith("cat > a.json <<'EOF'\n[heredoc body omitted:")
    assert stripped.endswith("\nls")


def test_bash_grep_and_heredoc_contents_are_not_stored(ctx):
    blocks = {(r["tool_use_id"], r["block_type"]): r for r in rows_of(ctx, "llm_blocks")
              if r["tool_use_id"]}
    cat = blocks[("toolu_cat", "tool_result")]
    assert cat["content_text_redacted"] is None and cat["content_json_redacted"] is None
    assert cat["content_omitted_reason"] == "command_output"
    assert len(cat["metadata_redacted"]["result_sha256"]) == 64
    heredoc = blocks[("toolu_heredoc", "tool_use")]
    assert heredoc["content_omitted_reason"] == "heredoc_body"
    assert "[heredoc body omitted:" in heredoc["content_json_redacted"]["command"]
    grep = blocks[("toolu_grep", "tool_result")]
    assert grep["content_omitted_reason"] == "file_content"
    curl = blocks[("toolu_curl", "tool_result")]                 # a failure keeps its excerpt
    assert "403" in curl["content_text_redacted"]


def test_file_names_with_query_or_hash_are_stored_safely(ctx):
    assert ingest.safe_relpath("assets/take#2.png") == "assets/take%232.png"
    signed = ingest.safe_relpath("assets/x.png?Expires=1&Signature=SIGNAME1")
    assert "?" not in signed and "SIGNAME1" not in signed and signed.startswith("assets/x.png%3F")
    arts = {r["logical_id"]: r for r in rows_of(ctx, "artifacts")}
    take = arts["projects/assets/images/take%232.png"]
    assert "#" not in take["object_uri"] and "?" not in take["object_uri"]
    for row in rows_of(ctx, "ingestion_sources"):
        assert "#" not in row["canonical_path"] and "?" not in row["canonical_path"]
    assert all(r["created_at"] for r in arts.values())


def test_read_jsonl_keeps_records_with_line_separators(tmp_path):
    path = tmp_path / "t.jsonl"
    records = [{"type": "user", "text": "line separator"}, {"type": "assistant",
                                                                "text": "para graph\x85"}]
    path.write_bytes(b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n"
                              for r in records))
    assert [r for _n, _l, r in ingest.read_jsonl(path)] == records


@pytest.mark.parametrize("label, pipeline, stage", [
    ("start_continue_1", "panda-image", "scene_plan"), ("start_continue_1", "panda-video", "script"),
    ("scene_plan", "panda-image", "scene_plan"),
])
def test_stage_of_label_knows_the_image_pipeline(label, pipeline, stage):
    assert ingest.stage_of_label(label, pipeline) == stage


# ------------------------------------------------------------------ legs, attempts, sessions

def test_timed_out_leg_does_not_shift_the_next_legs_log(tmp_path):
    settings, _job_dir, claude = small_box(
        tmp_path, timing=[("edit", 3610, 3600.0), ("edit", 7300, 300.0)],
        logs={"edit": block(0, 0, "edited")})
    first = leg_session(claude, "1", 12, 3500)
    second = leg_session(claude, "2", 7005, 7290)
    ctx = build(settings, job=SMALL, now=LATER)
    runs = runs_by_key(ctx)
    assert runs["edit:0:0"]["status"] == "timed_out"
    assert runs["edit:0:0"]["claude_session_id"] == first.session
    assert runs["edit:1:0"]["status"] == "completed" and runs["edit:1:0"]["exit_code"] == 0
    assert runs["edit:1:0"]["claude_session_id"] == second.session
    failed = [e["dedupe_key"] for e in events(ctx, "agent.attempt.failed")]
    assert not [k for k in failed if k.startswith("attempt:edit:1:0")]


def test_timed_out_leg_is_found_without_transcripts(tmp_path):
    settings, _job_dir, _claude = small_box(
        tmp_path, timing=[("edit", 3610, 3600.0), ("edit", 7300, 300.0)],
        logs={"edit": block(0, 0, "edited")})
    runs = runs_by_key(build(settings, job=SMALL, now=LATER))
    assert runs["edit:0:0"]["status"] == "timed_out"
    assert runs["edit:1:0"]["status"] == "completed"


def test_a_chained_legs_running_session_is_not_given_to_the_previous_leg(tmp_path):
    settings, _job_dir, claude = small_box(
        tmp_path, status="running", timing=[("script", 600, 590.0)], logs={"script": block(0, 0)})
    first = leg_session(claude, "1", 12, 598)
    leg_session(claude, "2", 601.5, 650)                     # start_continue_1, still running
    ctx = build(settings, job=SMALL, now=T0 + timedelta(seconds=700))
    runs = rows_of(ctx, "agent_runs")
    assert [r["run_key"] for r in runs] == ["script:0:0"]
    assert runs[0]["claude_session_id"] == first.session
    assert ctx.deferred and not events(ctx, "agent.attempt.failed")


def test_a_short_final_retry_stays_with_its_own_leg(tmp_path):
    settings, _job_dir, claude = small_box(
        tmp_path, timing=[("assets_after_plan_1", 1000, 1000.0), ("stills", 2000, 998.0)],
        logs={"assets_after_plan_1": block(0, 1, "529") + block(1, 0),
              "stills": block(0, 0)})
    leg_session(claude, "1", 2, 950)
    retry = leg_session(claude, "2", 991, 999)
    nxt = leg_session(claude, "3", 1003, 1990)
    runs = runs_by_key(build(settings, job=SMALL, now=LATER))
    assert runs["assets_after_plan_1:0:1"]["claude_session_id"] == retry.session
    assert runs["stills:0:0"]["claude_session_id"] == nxt.session
    assert runs["stills:0:0"]["status"] == "completed"


def test_an_operator_session_naming_the_job_is_not_an_attempt(tmp_path):
    settings, _job_dir, claude = small_box(
        tmp_path, timing=[("assets", 3000, 2990.0)], logs={"assets": block(0, 0)})
    leg_session(claude, "1", 12, 2900)
    asked = leg_session(claude, "2", 1200, 1300, f"look at project_id: {SMALL} why so slow?",
                        leg=False)
    ctx = build(settings, job=SMALL, now=LATER)
    runs = runs_by_key(ctx)
    assert set(runs) == {"assets:0:0", f"interactive:{asked.session}"}
    assert runs[f"interactive:{asked.session}"]["status"] == "interactive"
    assert not events(ctx, "agent.attempt.failed") and not events(ctx, "agent.session.orphaned")


def test_run_totals_include_subagents(ctx):
    run = runs_by_key(ctx)["assets_media:0:0"]
    infs = [r for r in rows_of(ctx, "llm_inferences") if r["agent_run_id"] == run["agent_run_id"]]
    assert run["output_tokens"] == sum(r["output_tokens"] for r in infs)
    assert run["inference_count"] == len(infs) and run["metadata_redacted"]["subagents"] == 1


# ------------------------------------------------------------------------ revise and approvals

REVISE_END = " Rewrite that stage's checkpoint with status='awaiting_human' and STOP for approval."


@pytest.mark.parametrize("prompt, stage, note", [
    (f"YOU are the launcher-spawned `script` leg. Revise stage 'script' for project_id: {JOB} "
     f"per this feedback: shorter intro.{REVISE_END}", "script", "shorter intro"),
    (f"Revise stage 'script' for project_id: {JOB} per this feedback: fix scene 2. Regenerate "
     f"only shots [2].{REVISE_END}", "script", "fix scene 2"),
    (f"Revise stage 'assets' for project_id: {JOB} per this feedback: warmer light. All current "
     f"stills. MODE=FRESH. Current stills live under x.{REVISE_END}", "assets", "warmer light"),
    (f"Revise stage 'assets (keep partial_progress.phase='motion_sample')' for project_id: {JOB} "
     f"per this feedback: slower pan.{REVISE_END}",
     "assets (keep partial_progress.phase='motion_sample')", "slower pan"),
    (f"Revise stage 'scene_plan' for project_id: {JOB} per this feedback: scene 2: airport\n"
     f"scene 3: taxi.{REVISE_END}\n\nUSER SCREENSHOTS — facts", "scene_plan",
     "scene 2: airport\nscene 3: taxi"),
])
def test_revise_note_is_the_reviewers_text_only(prompt, stage, note):
    assert ingest.revise_note(prompt) == (stage, JOB, note)


def test_a_retried_revise_leg_records_one_decision(tmp_path):
    settings, _job_dir, claude = small_box(
        tmp_path, timing=[("motion_sample_revise", 5000, 400.0)],
        logs={"motion_sample_revise": block(0, 1, "529") + block(1, 0)})
    prompt = (f"YOU are the launcher-spawned `assets` leg. Revise stage 'assets (phase='motion_"
              f"sample')' for project_id: {SMALL} per this feedback: slower pan.{REVISE_END}")
    first = leg_session(claude, "1", 4602, 4700, prompt)
    leg_session(claude, "2", 4710, 4990, prompt)
    revise = [r for r in rows_of(build(settings, job=SMALL, now=LATER), "approval_events")
              if r["decision"] == "revise"]
    assert len(revise) == 1
    assert revise[0]["answer_redacted"] == "slower pan" and revise[0]["gate"] == "stage:assets"
    assert revise[0]["received_at"] == ts(4602).replace("Z", "+00:00")
    assert revise[0]["ingestion_record_id"]
    assert first.session in json.dumps(rows_of(build(settings, job=SMALL, now=LATER),
                                               "agent_runs"))


def _checkpoints(job_dir: Path, versions) -> None:
    history = job_dir / "history"
    history.mkdir(exist_ok=True)
    for index, (status, at, approved) in enumerate(versions):
        data = {"version": "1.0", "project_id": SMALL, "pipeline_type": "panda-video",
                "stage": "script", "status": status, "timestamp": ts(at),
                "human_approval_required": True, "human_approved": approved,
                "artifacts": {"script": {"v": index}}}
        target = (job_dir / "checkpoint_script.json" if index == len(versions) - 1
                  else history / f"checkpoint_script_{index}.json")
        target.write_text(json.dumps(data), encoding="utf-8")
        at_file(target, at)


def test_an_approval_written_after_the_last_leg_waits_while_the_job_runs(tmp_path):
    settings, job_dir, _claude = small_box(
        tmp_path, status="running", timing=[("script", 200, 190.0)], logs={"script": block(0, 0)})
    _checkpoints(job_dir, [("awaiting_human", 100, False), ("completed", 300, True)])
    assert not rows_of(build(settings, job=SMALL, now=LATER), "approval_events")
    state = json.loads((settings.data_dir / "jobs" / SMALL / "state.json").read_text())
    state["status"] = "awaiting_human"
    (settings.data_dir / "jobs" / SMALL / "state.json").write_text(json.dumps(state))
    approvals = rows_of(build(settings, job=SMALL, now=LATER), "approval_events")
    assert len(approvals) == 1 and approvals[0]["metadata_redacted"]["written_by"] == "launcher"


def test_a_gate_the_launcher_approves_itself_is_not_a_human_decision(tmp_path):
    settings, job_dir, _claude = small_box(tmp_path, options={"gates": ["scene_plan", "assets"]})
    _checkpoints(job_dir, [("awaiting_human", 100, False), ("completed", 300, True)])
    ctx = build(settings, job=SMALL, now=LATER)
    assert not rows_of(ctx, "approval_events")
    assert events(ctx, "gate.auto_approved")[0]["stage"] == "script"


# --------------------------------------------------------------------------- Higgsfield waves

JOB_A, JOB_B, JOB_C = (str(uuid.UUID(int=n)) for n in (101, 102, 103))


def test_jobs_wait_reports_each_jobs_own_status():
    out = _classify("jobs_wait", {"jobs": [{"job_id": JOB_A}, {"job_id": JOB_B}]},
                    {"timed_out": True, "all_terminal": False,
                     "jobs": [{"job_id": JOB_A, "status": "completed"},
                              {"job_id": JOB_B, "status": "in_progress"}]})
    assert out.per_job == {JOB_A: "completed", JOB_B: "in_progress"}


def test_a_wave_poll_gives_each_provider_job_its_own_outcome(tmp_path):
    settings, _job_dir, claude = small_box(
        tmp_path, timing=[("assets_media", 1000, 990.0)], logs={"assets_media": block(0, 0)})
    t = Transcript(claude / f"{sid_of('1')}.jsonl", sid_of("1"))
    t.prompt(12, f"For project_id: {SMALL}, the FULL MEDIA phase.")
    batch = {"submitted_count": 3, "failed_count": 0,
             "jobs": [{"job_id": j} for j in (JOB_A, JOB_B, JOB_C)]}
    t.assistant(20, "m1", [tool("t_batch", HF + "generate_video_batch", {"requests": [{}] * 3})],
                USAGE)
    t.result(21, "t_batch", json.dumps(batch), structured=batch)
    poll1 = {"timed_out": True, "all_terminal": False,
             "jobs": [{"job_id": JOB_A, "status": "completed"},
                      {"job_id": JOB_B, "status": "in_progress"},
                      {"job_id": JOB_C, "status": "in_progress"}]}
    t.assistant(30, "m2", [tool("t_w1", HF + "jobs_wait", {"jobs": [
        {"job_id": j} for j in (JOB_A, JOB_B, JOB_C)]})], USAGE)
    t.result(90, "t_w1", json.dumps(poll1), structured=poll1)
    poll2 = {"all_terminal": True, "summary": {"completed": 1, "failed": 1},
             "jobs": [{"job_id": JOB_B, "status": "completed"},
                      {"job_id": JOB_C, "status": "nsfw"}]}
    t.assistant(100, "m3", [tool("t_w2", HF + "jobs_wait", {"jobs": [
        {"job_id": j} for j in (JOB_B, JOB_C)]})], USAGE)
    t.result(160, "t_w2", json.dumps(poll2), structured=poll2)
    t.assistant(170, "m4", [{"type": "text", "text": "ok"}], USAGE, stop="end_turn")
    t.write()
    ctx = build(settings, job=SMALL, now=LATER)
    outcome = {r["provider_job_id_hash"]: r["semantic_outcome"] for r in rows_of(ctx, "trace_spans")
               if r["span_kind"] == "provider_job"}
    assert outcome == {ingest.hash_id(JOB_A): "completed", ingest.hash_id(JOB_B): "completed",
                       ingest.hash_id(JOB_C): "failed"}
    links = [r for r in rows_of(ctx, "span_links") if r["relation"] == "polls"]
    assert all(r["metadata_redacted"]["provider_job_id_hash"] for r in links)


def test_a_quote_is_sent_with_its_run_once_the_leg_ends(tmp_path):
    settings, _job_dir, claude = small_box(tmp_path, status="running")
    t = Transcript(claude / f"{sid_of('1')}.jsonl", sid_of("1"))
    t.prompt(12, f"For project_id: {SMALL}, the FULL MEDIA phase.")
    params = {"model": "seedance", "prompt": "p"}
    cost = {"cost": {"credits_exact": 7.5}}
    t.assistant(20, "m1", [tool("t_cost", HF + "generate_video",
                                {"params": dict(params, get_cost=True)})], USAGE)
    t.result(21, "t_cost", json.dumps(cost), structured=cost)
    sub = {"results": [{"id": JOB_A, "status": "pending"}]}
    t.assistant(30, "m2", [tool("t_sub", HF + "generate_video", {"params": params})], USAGE)
    t.result(31, "t_sub", json.dumps(sub), structured=sub)
    t.write()
    early = build(settings, job=SMALL, now=T0 + timedelta(seconds=60))
    assert not [r for r in rows_of(early, "cost_observations")]
    late = build(settings, job=SMALL, now=LATER)                  # abandoned: final now
    quote = [r for r in rows_of(late, "cost_observations")
             if r["usage_kind"] == "quoted_submission_credits"]
    assert quote and quote[0]["agent_run_id"] == rows_of(late, "agent_runs")[0]["agent_run_id"]


# ------------------------------------------------------------------------ connectors, MCP logs

def _bare_session(settings, now=NOW):
    ctx = ingest.JobContext(SMALL, settings, now, {})
    info = ingest.TranscriptInfo(path="x.jsonl", size=0, mtime_ns=0, session_id=sid_of("9"),
                                 job_id=SMALL, first_ts=None, last_ts=None, is_subagent=False,
                                 prompt_source="sdk", permission_mode="bypassPermissions")
    return ctx, ingest.Session(info, ctx)


@pytest.mark.parametrize("setup, outcome, severity", [
    (lambda m: (m["pending"].add("claude.ai Higgsfield"), m.update(deltas=2)),
     "higgsfield_connected", "info"),                                  # pending, then cleared
    (lambda m: m.update(deltas=1, instructions=True), "higgsfield_connected", "info"),
    (lambda m: m.update(deltas=1, log_connected=True), "higgsfield_connected", "info"),
    (lambda m: (m["failed"].add("claude.ai Higgsfield"), m.update(deltas=1)),
     "higgsfield_failed", "error"),
    (lambda m: (m["pending"].add("claude.ai Higgsfield"),
                m["last_pending"].add("claude.ai Higgsfield"), m.update(deltas=1)),
     "higgsfield_still_pending_at_end", "error"),
    (lambda m: m.update(deltas=1), "higgsfield_not_listed", "warning"),
])
def test_connector_status_reads_every_signal(box, setup, outcome, severity):
    ctx, session = _bare_session(box)
    setup(session.mcp)
    ingest._mcp_summary(ctx, session)
    event = events(ctx, "claude.mcp.status")[0]
    assert (event["outcome"], event["severity"]) == (outcome, severity)


def test_mcp_log_summary_is_written_once_per_log(ctx):
    summaries = events(ctx, "mcp.log.summary")
    assert len(summaries) == 1
    assert summaries[0]["dedupe_key"] == ("mcpsummary:mcp-logs-claude-ai-Higgsfield:"
                                          "2026-10-14T10-10-00.jsonl")


# --------------------------------------------------------------------------- manifest and QA

def _manifest(job_dir: Path, where: str, assets: list, metadata: dict = None) -> None:
    manifest = {"version": "1.0", "assets": assets, "metadata": metadata or {}}
    if where == "checkpoint":
        data = {"version": "1.0", "project_id": SMALL, "pipeline_type": "panda-video",
                "stage": "assets", "status": "in_progress", "timestamp": ts(5000),
                "artifacts": {"asset_manifest": manifest}}
        path = job_dir / "checkpoint_assets.json"
    else:
        path = job_dir / "artifacts" / "asset_manifest.json"
        data = manifest
    path.write_text(json.dumps(data), encoding="utf-8")
    at_file(path, 5000)


def test_lipsync_subshot_reviews_become_events_and_links(tmp_path):
    settings, job_dir, _claude = small_box(tmp_path)
    clip = job_dir / "assets" / "video" / "sc2_customer.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"clip")
    at_file(clip, 5000)
    review = {"eligible": True, "status": "fail_generation", "retry_count": 1,
              "selected_take": "retry", "subshot_id": "sc2-s1", "speaker": "customer",
              "attempts": [{"take": "original", "video_asset_id": "clipA", "job_id": JOB_A}],
              "retry_job_id": JOB_B, "unresolved_warning": None}
    _manifest(job_dir, "checkpoint",
              [{"id": "clipA", "path": "assets/video/sc2_customer.mp4", "type": "video"}],
              {"lip_sync_qa": {"status": "warning", "scenes": {},
                               "subshots": {"sc2": {"s1": review}}}})
    ctx = build(settings, job=SMALL, now=LATER)
    qa = events(ctx, "qa.lipsync.scene")
    assert len(qa) == 1 and qa[0]["outcome"] == "fail_generation"
    assert qa[0]["severity"] == "warning" and qa[0]["scene_id"] == "sc2"
    assert qa[0]["attributes_redacted"]["section_id"] == "s1"
    produces = [r for r in rows_of(ctx, "span_links") if r["relation"] == "produces"]
    assert len(produces) == 1 and produces[0]["to_artifact_id"]


def test_the_manifest_is_one_source_whichever_copy_is_live(tmp_path):
    settings, job_dir, _claude = small_box(tmp_path)
    _manifest(job_dir, "file", [{"id": "a", "path": "assets/a.png", "credits": 10,
                                 "provider": "higgsfield"}])
    first = build(settings, job=SMALL, now=LATER)
    _manifest(job_dir, "checkpoint", [{"id": "b", "path": "assets/b.png", "credits": 4,
                                       "provider": "higgsfield"}])
    second = build(settings, job=SMALL, now=LATER)

    def manifest_source(ctx):
        return {r["ingestion_source_id"] for r in rows_of(ctx, "ingestion_sources")
                if r["canonical_path"].endswith("/asset_manifest:live")}

    assert manifest_source(first) == manifest_source(second) and len(manifest_source(first)) == 1


def test_a_report_verdict_is_kept_only_when_status_like(tmp_path):
    settings, job_dir, _claude = small_box(tmp_path)
    report = job_dir / "assets" / "_qa_report.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"result": "https://x.example/a?sig=SIGREPORT1"}))
    at_file(report, 5000)
    ctx = build(settings, job=SMALL, now=LATER)
    event = events(ctx, "report.qa_report")[0]
    assert event["outcome"] is None
    assert event["attributes_redacted"]["summary"]["value_counts"] == {"other": 1}
    assert "SIGREPORT1" not in json.dumps(ctx.rows.items(), default=str)


def test_a_tool_start_without_finish_is_reported_once_the_job_ends(tmp_path):
    settings, _job_dir, _claude = small_box(tmp_path, events_lines=[
        {"ts": ts(100), "tool": "panda_render", "event": "start"}])
    ctx = build(settings, job=SMALL, now=LATER)
    assert events(ctx, "tool.no_finish")[0]["attributes_redacted"]["tool"] == "panda_render"


# ------------------------------------------------------------------------------ artifacts

def test_artifact_lifecycle_rules(tmp_path):
    settings, job_dir, _claude = small_box(tmp_path, extra_state={"artifacts": {"still": "sc2.png"}})
    images = job_dir / "assets" / "images"
    for rel in ("sc2.png", "history/sc2.png", "superseded-stills/sc2.png",
                "rejected-sc2-take1.png", "sc2.pre-revise.png", "sc3.a1b2.tmp.png"):
        (images / rel).parent.mkdir(parents=True, exist_ok=True)
        (images / rel).write_bytes(rel.encode())
        at_file(images / rel, 5000)
    young = job_dir / "renders" / "final.mp4"
    young.parent.mkdir(parents=True)
    young.write_bytes(b"half-written")
    at_file(young, (LATER - T0).total_seconds() - 30)
    ctx = build(settings, job=SMALL, now=LATER)
    arts = {r["logical_id"]: r for r in rows_of(ctx, "artifacts")}
    base = "projects/assets/images/"
    assert arts[base + "sc2.png"]["selected"] is True
    assert arts[base + "sc2.png"]["artifact_status"] == "available"
    for rel, status in (("history/sc2.png", "superseded"), ("superseded-stills/sc2.png",
                                                             "superseded"),
                        ("rejected-sc2-take1.png", "rejected"), ("sc2.pre-revise.png",
                                                                 "superseded")):
        assert (arts[base + rel]["selected"], arts[base + rel]["artifact_status"]) == (False,
                                                                                    status)
    assert base + "sc3.a1b2.tmp.png" not in arts
    assert "projects/renders/final.mp4" not in arts and ctx.deferred


# ------------------------------------------------------------------- state.json, export runs

def test_a_torn_state_json_is_retried_not_exported(tmp_path):
    settings, _job_dir, _claude = small_box(tmp_path)
    state = settings.data_dir / "jobs" / SMALL / "state.json"
    state.write_text('{"job_id": "job_00000000000a", "status": "do', encoding="utf-8")
    at_file(state, 9000)
    with pytest.raises(ingest.DeferJob):
        build(settings, job=SMALL, now=T0 + timedelta(seconds=9030))
    ctx = build(settings, job=SMALL, now=LATER)                 # broken for hours: exported
    job = rows_of(ctx, "jobs")[0]
    assert job["current_status"] == "unknown" and job["state_version"] == 0


def test_settled_jobs_are_skipped_and_deleted_transcripts_change_nothing(box, capsys):
    box.spool.mkdir()
    assert _run(box) == 0
    first = _batches(box)
    capsys.readouterr()
    assert _run(box) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["jobs_exported"] == 0 and _batches(box) == first
    (box.transcript_dirs[0] / "a1111111-0000-4000-8000-000000000001.jsonl").unlink()
    assert _run(box) == 0
    assert _batches(box) == first                                # a deleted transcript is no change


def test_cache_pruning_keeps_recently_seen_hashes():
    state = {"file_sha": {"a|1|1": "s1", "b|1|1": "s2"}, "file_sha_seen": {"a|1|1": 100,
                                                                           "b|1|1": 10},
             "probes": {"s1": {"duration": 1}, "s2": {"duration": 2}}}
    ingest._prune_cache(state, today=101, keep_days=30)
    assert state["file_sha"] == {"a|1|1": "s1"} and set(state["probes"]) == {"s1"}


# ------------------------------------------------------------------ loader without a database

class FakeDbError(Exception):
    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(f"ERROR: {message}\nDETAIL: Failing row contains (row-secret)")
        self.diag = type("Diag", (), {"sqlstate": sqlstate, "message_primary": message})()


class FakeConn:
    """Enough of a psycopg connection for load_batch: inserts are kept per transaction."""

    def __init__(self, columns: dict, fail=None) -> None:
        self.columns = columns
        self.fail = fail                      # (table, value in the row, sqlstate)
        self.pending: list = []
        self.saved: list = []
        self.rows: list = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self) -> None:
        self.rows += self.pending
        self.pending = []

    def rollback(self) -> None:
        self.pending = []

    def close(self) -> None:
        pass


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.rowcount = 0
        self._one = None
        self._all: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        conn = self.conn
        if sql.startswith("SELECT table_name"):
            self._all = [(t, c, "text", "text") for t, cols in conn.columns.items() for c in cols]
        elif sql.startswith("SAVEPOINT"):
            conn.saved.append(len(conn.pending))
        elif sql.startswith("ROLLBACK TO"):
            del conn.pending[conn.saved.pop():]
        elif sql.startswith("RELEASE"):
            conn.saved.pop()
        elif sql.startswith("INSERT INTO"):
            table = sql.split()[2].split(".")[1]
            if conn.fail and conn.fail[0] == table and conn.fail[1] in map(str, params):
                raise FakeDbError(conn.fail[2], "refused")
            conn.pending.append((table, params))
            self.rowcount, self._one = 1, (True,)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


def _small_batch(tmp_path: Path, rows: list, job: str = SMALL) -> Path:
    spool = tmp_path / "spool"
    settings = ingest.Settings(repo=tmp_path, data_dir=tmp_path, projects_dir=tmp_path,
                               transcript_dirs=[], mcp_log_dirs=[], spool=spool,
                               state_dir=tmp_path / "state", host_label="t", ffprobe=None)
    return ingest.write_batch(spool / "pending", job, rows, False, settings, NOW, shared=False)


def _rows(job: str = SMALL, dedupe=("e1", "e2", "e3")) -> list:
    rows = [("jobs", {"job_id": job, "pipeline": "p", "brief": "", "current_status": "done",
                      "state_version": 1})]
    for key in dedupe:
        rows.append(("runtime_events", {"runtime_event_id": str(uuid.uuid5(uuid.NAMESPACE_DNS,
                                                                            key)),
                                        "job_id": job, "dedupe_key": key, "event_name": "x",
                                        "source_service": "t", "severity": "info"}))
    return rows


def _columns(rows: list) -> dict:
    columns: dict = {}
    for table, row in rows:
        columns.setdefault(table, set()).update(row)
    return columns


def test_read_batch_rejects_files_that_are_not_batches(tmp_path):
    good = _small_batch(tmp_path, _rows())
    header = gzip.open(good).read().split(b"\n")[0]
    cases = {
        "not_object": [b"[]"],
        "deep": [b"[" * 100_000 + b"]" * 100_000],
    }
    for name, body in cases.items():
        h = json.loads(header)
        h.update(row_count=len(body),
                 rows_sha256=__import__("hashlib").sha256(b"\n".join(body)).hexdigest())
        path = tmp_path / f"{name}.jsonl.gz"
        path.write_bytes(gzip.compress(json.dumps(h).encode() + b"\n" + b"\n".join(body) + b"\n"))
        with pytest.raises(loader.BatchError):
            loader.read_batch(path)
    corrupt = tmp_path / "corrupt.jsonl.gz"
    data = bytearray(good.read_bytes())
    data[30:60] = b"\x00" * 30
    corrupt.write_bytes(bytes(data))
    with pytest.raises(loader.BatchError):
        loader.read_batch(corrupt)


def test_forged_rows_are_refused(tmp_path):
    rows = _rows() + [
        ("runtime_events", {"runtime_event_id": str(uuid.uuid4()), "job_id": OTHER_JOB,
                            "dedupe_key": "x", "event_name": "x", "source_service": "t",
                            "severity": "info"}),
        ("approval_events", {"approval_event_id": str(uuid.uuid4()), "job_id": SMALL,
                             "provenance": "launcher_received"}),
        ("jobs", {"job_id": SMALL, "state_version": 9_223_372_036_854_775_000}),
    ]
    path = _small_batch(tmp_path, rows)
    header, parsed = loader.read_batch(path)
    refused = dict(loader.check_rows(header, path, parsed))
    assert sorted(refused) == [4, 5, 6]
    elsewhere = tmp_path / "pending" / OTHER_JOB / path.name
    elsewhere.parent.mkdir(parents=True)
    shutil.copy(path, elsewhere)
    with pytest.raises(loader.BatchError, match="folder"):
        loader.check_rows(header, elsewhere, parsed)


@pytest.mark.parametrize("state, environment", [
    ("42703", True), ("42P01", True), ("42501", True), ("25006", True), ("53100", True),
    ("57014", True), ("08006", True), ("40001", True), ("55P03", True),
    ("23514", False), ("23503", False), ("23502", False), ("22P05", False), ("22021", False),
    ("55000", False), ("P0001", False), ("42804", False),
])
def test_database_errors_are_classified(state, environment):
    assert loader.is_environment_error(FakeDbError(state, "x")) is environment


def _dsn_file(tmp_path: Path) -> Path:
    path = tmp_path / "dsn"
    path.write_text("postgresql://u:pa%zzss@h/db", encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)
    return path


def test_loader_keeps_batches_pending_when_the_database_is_the_problem(tmp_path, capsys):
    rows = _rows()
    path = _small_batch(tmp_path, rows)
    columns = _columns(rows)
    columns["runtime_events"].discard("severity")                    # migration missing
    code = loader.run(tmp_path / "spool", _dsn_file(tmp_path), tmp_path / "ls", 0, 30,
                      connect_fn=lambda dsn: FakeConn(columns))
    assert code == 1 and path.exists() and not (tmp_path / "spool" / "failed").exists()
    conn = FakeConn(_columns(rows), fail=("runtime_events", "e2", "25006"))   # read-only
    loader.run(tmp_path / "spool", _dsn_file(tmp_path), tmp_path / "ls", 0, 30,
               connect_fn=lambda dsn: conn)
    assert path.exists() and not conn.rows
    assert "STOPPED" in capsys.readouterr().out


def test_loader_sets_aside_only_the_rows_postgres_refuses(tmp_path, capsys):
    rows = _rows()
    path = _small_batch(tmp_path, rows)
    conn = FakeConn(_columns(rows), fail=("runtime_events", "e2", "23514"))
    loader.run(tmp_path / "spool", _dsn_file(tmp_path), tmp_path / "ls", 0, 30,
               connect_fn=lambda dsn: conn)
    assert [t for t, _p in conn.rows] == ["jobs", "runtime_events", "runtime_events"]
    kept = tmp_path / "spool" / "failed" / SMALL / path.name
    assert kept.exists()
    note = json.loads(kept.with_name(kept.name + ".rejected.json").read_text())
    assert [r["table"] for r in note["rejected"]] == ["runtime_events"]
    out = capsys.readouterr().out
    assert "row-secret" not in out and "rejected=1" in out


def test_loader_moves_invalid_files_aside_and_loads_the_rest(tmp_path):
    rows = _rows()
    good = _small_batch(tmp_path, rows)
    bad = good.with_name("00000000T000000000000Z-" + SMALL + "-000000000000.jsonl.gz")
    bad.write_bytes(b"\x1f\x8bnot really gzip")
    conn = FakeConn(_columns(rows))
    loader.run(tmp_path / "spool", _dsn_file(tmp_path), tmp_path / "ls", 0, 30,
               connect_fn=lambda dsn: conn)
    failed = tmp_path / "spool" / "failed" / SMALL
    assert (failed / bad.name).exists() and (failed / (bad.name + ".reason.json")).exists()
    assert (tmp_path / "spool" / "done" / SMALL / good.name).exists() and conn.rows


def test_a_connection_error_never_prints_the_dsn(tmp_path, capsys):
    _small_batch(tmp_path, _rows())

    def refuse(dsn):
        raise RuntimeError(f"invalid percent-encoded token: \"{dsn}\"")

    assert loader.run(tmp_path / "spool", _dsn_file(tmp_path), tmp_path / "ls", 0, 30,
                      connect_fn=refuse) == 2
    assert "pa%zzss" not in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="ownership checks are POSIX only")
def test_loader_install_refuses_a_file_the_launcher_user_could_edit(tmp_path):
    script = tmp_path / "panda_ingest_loader.py"
    script.write_text("")
    assert loader._root_owned_chain(script) == str(script)


# ------------------------------------------------------------------- PostgreSQL round trip

PG_DSN = os.environ.get("PANDA_INGEST_TEST_DSN")


@pytest.mark.skipif(not PG_DSN, reason="set PANDA_INGEST_TEST_DSN to run against PostgreSQL")
def test_postgres_round_trip(box, tmp_path):
    psycopg = pytest.importorskip("psycopg")
    name = "om_ingest_test_" + uuid.uuid4().hex[:8]
    admin = psycopg.connect(PG_DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    try:
        target = psycopg.conninfo.make_conninfo(PG_DSN, dbname=name)
        owner = psycopg.connect(target, autocommit=True)
        for migration in ("V001__phase1.sql", "V002__forensics.sql"):
            sql = (ROOT / "deploy" / "postgres" / migration).read_text(encoding="utf-8")
            owner.execute(sql)
        password = uuid.uuid4().hex
        owner.execute("DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                      "'om_ingest_pytest') THEN CREATE ROLE om_ingest_pytest LOGIN; END IF; END$$")
        owner.execute(f"ALTER ROLE om_ingest_pytest PASSWORD '{password}'")
        owner.execute("GRANT om_phase1_writer TO om_ingest_pytest")
        writer_dsn = psycopg.conninfo.make_conninfo(target, user="om_ingest_pytest",
                                                    password=password)
        dsn_file = tmp_path / "dsn"
        dsn_file.write_text(writer_dsn, encoding="utf-8")
        if os.name == "posix":
            os.chmod(dsn_file, 0o600)
        box.spool.mkdir()
        assert _run(box) == 0
        assert loader.run(box.spool, dsn_file, tmp_path / "lstate", 0, 30) == 0
        counts = dict(owner.execute(
            "SELECT 'blocks', count(*) FROM openmontage.llm_blocks UNION ALL "
            "SELECT 'spans', count(*) FROM openmontage.trace_spans UNION ALL "
            "SELECT 'approvals', count(*) FROM openmontage.approval_events").fetchall())
        assert counts["blocks"] > 20 and counts["approvals"] == 2
        for path in (box.spool / "done").rglob("*.jsonl.gz"):           # replay everything
            target_path = box.spool / "pending" / path.relative_to(box.spool / "done")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, target_path)
        assert loader.run(box.spool, dsn_file, tmp_path / "lstate", 0, 30) == 0
        again = dict(owner.execute(
            "SELECT 'blocks', count(*) FROM openmontage.llm_blocks UNION ALL "
            "SELECT 'spans', count(*) FROM openmontage.trace_spans UNION ALL "
            "SELECT 'approvals', count(*) FROM openmontage.approval_events").fetchall())
        assert again == counts
        outcomes = dict(owner.execute(
            "SELECT tool_name, semantic_outcome FROM openmontage.tool_calls_v "
            "WHERE tool_name LIKE '%jobs_wait' ORDER BY started_at LIMIT 1").fetchall())
        assert list(outcomes.values()) == ["timed_out_nonterminal"]
        totals = owner.execute(
            "SELECT usage_kind, amount FROM openmontage.job_cost_totals WHERE job_id = %s "
            "AND provider = 'higgsfield' ORDER BY usage_kind", (JOB,)).fetchall()
        assert [(k, float(a)) for k, a in totals] == [("manifest_credits", 10.5),
                                                       ("quoted_submission_credits", 10.5)]
        status = owner.execute(
            "SELECT approval_received, received_decision, approval_provenance "
            "FROM openmontage.checkpoint_approval_status WHERE job_id = %s AND stage = 'script'",
            (JOB,)).fetchone()
        assert status == (True, "approve", "legacy_inferred")
        resume = owner.execute("SELECT openmontage.get_job_resume_context(%s)", (JOB,)).fetchone()[0]
        script = [c for c in resume["checkpoints"] if c["stage"] == "script"][0]
        assert script["approval_received"] is True
        assert not list((box.spool / "failed").rglob("*")) if (box.spool / "failed").exists() \
            else True
        assert owner.execute("SELECT openmontage.purge_job(%s)", (JOB,)).fetchone()[0] > 0
        writer = psycopg.connect(writer_dsn, autocommit=True)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute("DELETE FROM openmontage.llm_blocks")
        writer.close()
        owner.close()
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


@pytest.mark.skipif(not PG_DSN, reason="set PANDA_INGEST_TEST_DSN to run against PostgreSQL")
def test_postgres_artifact_lifecycle_rejections_and_waiting(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    name = "om_ingest_life_" + uuid.uuid4().hex[:8]
    admin = psycopg.connect(PG_DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    try:
        target = psycopg.conninfo.make_conninfo(PG_DSN, dbname=name)
        owner = psycopg.connect(target, autocommit=True)
        for migration in ("V001__phase1.sql", "V002__forensics.sql"):
            owner.execute((ROOT / "deploy" / "postgres" / migration).read_text(encoding="utf-8"))
        password = uuid.uuid4().hex
        owner.execute("DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                      "'om_ingest_pytest') THEN CREATE ROLE om_ingest_pytest LOGIN; END IF; END$$")
        owner.execute(f"ALTER ROLE om_ingest_pytest PASSWORD '{password}'")
        owner.execute("GRANT om_phase1_writer TO om_ingest_pytest")
        writer_dsn = psycopg.conninfo.make_conninfo(target, user="om_ingest_pytest",
                                                    password=password)
        dsn_file = tmp_path / "dsn"
        dsn_file.write_text(writer_dsn, encoding="utf-8")
        if os.name == "posix":
            os.chmod(dsn_file, 0o600)
        settings, job_dir, claude = small_box(
            tmp_path, timing=[("assets_media", 1000, 990.0)],
            logs={"assets_media": block(0, 0)},
            extra_state={"artifacts": {"clip": "sc3.mp4"}})
        settings.spool.mkdir()
        lstate = tmp_path / "lstate"

        def export_and_load():
            assert _run(settings) == 0
            return loader.run(settings.spool, dsn_file, lstate, 0, 30)

        def artifact_rows():
            return owner.execute(
                "SELECT artifact_version, provider, model, selected, artifact_status "
                "FROM openmontage.artifacts WHERE job_id = %s AND logical_id = %s "
                "ORDER BY created_at", (SMALL, "projects/assets/video/sc3.mp4")).fetchall()

        # 1. The clip is on disk before the manifest names it; a leg session ran.
        clip = job_dir / "assets" / "video" / "sc3.mp4"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"take-one")
        at_file(clip, 900)
        session = leg_session(claude, "1", 12, 990)
        assert export_and_load() == 0
        assert artifact_rows() == [(1, None, None, True, "available")]

        # 2. The manifest names it later: provider and model are filled in.
        _manifest(job_dir, "checkpoint", [{"id": "c3", "path": "assets/video/sc3.mp4",
                                           "provider": "higgsfield", "model": "seedance",
                                           "credits": 12, "scene_id": "sc3"}])
        assert export_and_load() == 0
        assert artifact_rows() == [(1, "higgsfield", "seedance", True, "available")]

        # 3. A re-render replaces the file: the older version is retired.
        clip.write_bytes(b"take-two")
        at_file(clip, 1500)
        assert export_and_load() == 0
        rows = artifact_rows()
        assert [(r[3], r[4]) for r in rows] == [(False, "superseded"), (True, "available")]

        # 4. Claude Code deletes the transcript after 30 days and the job changes again: the
        #    rebuilt run (no session) is skipped, nothing fails.
        Path(session.path).unlink()
        (job_dir / "events.jsonl").write_text(json.dumps(
            {"ts": ts(2000), "tool": "panda_render", "event": "finish", "success": True,
             "duration_s": 5}) + "\n", encoding="utf-8")
        at_file(job_dir / "events.jsonl", 2000)
        assert export_and_load() == 0
        assert not (settings.spool / "failed").exists()
        runs = owner.execute("SELECT run_key, claude_session_id IS NOT NULL FROM "
                             "openmontage.agent_runs WHERE job_id = %s", (SMALL,)).fetchall()
        assert runs == [("assets_media:0:0", True)]

        # 5. One row PostgreSQL refuses: the rest of its batch loads, the batch is kept with
        #    the list of refusals.
        bad = [(t, dict(r, severity="bogus") if r.get("dedupe_key") == "bad-1" else r)
               for t, r in _rows(SMALL, dedupe=("ok-1", "bad-1", "ok-2"))
               if t == "runtime_events"]
        batch = ingest.write_batch(settings.spool / "pending", SMALL, bad, False, settings,
                                   datetime.now(timezone.utc), shared=False)
        assert loader.run(settings.spool, dsn_file, lstate, 0, 30) == 0
        loaded = {k for (k,) in owner.execute(
            "SELECT dedupe_key FROM openmontage.runtime_events WHERE job_id = %s "
            "AND (dedupe_key LIKE 'ok-%%' OR dedupe_key LIKE 'bad-%%')", (SMALL,)).fetchall()}
        assert loaded == {"ok-1", "ok-2"}
        kept = settings.spool / "failed" / SMALL / batch.name
        assert json.loads(kept.with_name(kept.name + ".rejected.json").read_text())[
            "rejected"][0]["table"] == "runtime_events"

        # 6. A batch with a column the database does not have yet waits in pending/.
        newer = [(t, dict(r, future_column=1)) for t, r in _rows(SMALL, dedupe=("w-1",))
                 if t == "runtime_events"]
        waiting = ingest.write_batch(settings.spool / "pending", SMALL, newer, False, settings,
                                     datetime.now(timezone.utc), shared=False)
        assert loader.run(settings.spool, dsn_file, lstate, 0, 30) == 1
        assert waiting.exists()
        owner.close()
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()
