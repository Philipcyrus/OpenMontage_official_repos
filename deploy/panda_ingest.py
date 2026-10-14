#!/usr/bin/env python3
"""Export every job's forensic trail as sanitized batches for PostgreSQL (job history).

Runs as the launcher's own user (cron, every few minutes) and holds NO database credential.
For each job whose files changed since the last run it reads:

  * the launcher state      DIFY_DATA_DIR/jobs/<job>/state.json (rewritten in place)
  * the project folder      projects/<job>/: project.json, checkpoint_*.json and history/,
                            decision_log.json, events.jsonl, artifacts/timing.jsonl,
                            artifacts/agent_*.log, the asset manifest (credits, lip-sync QA,
                            timeline contract), review/report files, inputs, every media file
  * the Claude transcripts  ~/.claude/projects/<repo-slug>/<session>.jsonl whose leg prompt
                            names the job ("project_id: job_..."), with their subagent files
  * the MCP logs            ~/.cache/claude-cli-nodejs/<repo-slug>/mcp-logs-*/ (connector
                            errors and drops, joined to a leg by its session id)

and turns them into rows for the V001 history tables and the V002 forensic tables: Claude
model responses, every visible message and tool call with sanitized arguments and results,
timed spans (legs, attempts, inferences, tool calls, provider jobs, gaps between legs),
provider outcomes (submitted, cost preflight, preset recommendation, still running, done),
quoted credits per submission, tool runs with ElevenLabs usage, QA results and approvals.

Everything is redacted here, before it leaves this user: credentials, every URL query
string, environment dumps, base64/media bytes and hidden reasoning never reach the spool.
File contents (Read/Write/Edit bodies, the output of successful Bash and Grep calls, heredoc
bodies) are kept as size and hash only. Rows get deterministic ids (uuid5 of their domain
key), so the loader can replay any batch without duplicates. Only rows not exported before
are written; a running job is exported incrementally (immutable facts at once; totals,
approvals and quotes once each leg has ended). A job is re-read only when one of its files
changed or something still waits on time. The batches go to the spool, where
deploy/panda_ingest_loader.py (a separate OS user holding the DSN) loads them into
PostgreSQL.

USAGE (on the box, as the launcher's user; no launcher restart):
  python3 deploy/panda_ingest.py --install           # cron: export every 5 minutes
  python3 deploy/panda_ingest.py                     # export changed jobs now
  python3 deploy/panda_ingest.py --job job_123abc --dry-run /tmp/batches
  python3 deploy/panda_ingest.py --status

Standard library only (system python3 3.9+). Paths default from the launcher's .env
(DIFY_DATA_DIR, OPENMONTAGE_PROJECTS_DIR, CLAUDE_CONFIG_DIR) and can be overridden with
PANDA_INGEST_* variables or flags (see --help).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

EXPORTER_VERSION = "1.1.0"
PARSER_VERSION = "panda-ingest-v2"
BATCH_FORMAT = "panda-ingest-batch"
BATCH_VERSION = 1
CRON_MARKER = "deploy/panda_ingest.py"
INDEX_VERSION = 3

ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL,
                          "https://github.com/Philipcyrus/OpenMontage_official_repos/panda-ingest")
# Every leg prompt builder interpolates "project_id: {job_id}" before any user text, so the
# FIRST match identifies the job; later matches could come from the brief or feedback.
PROMPT_JOB_RE = re.compile(r"project_id:\s*`?(job_[0-9a-f]{12})\b")
# The runner's leg prompts (dify_launcher/runner.py) open with one of these. A session whose
# prompt merely names a job (an operator asking about it) is not a leg attempt.
LEG_PROMPT_RE = re.compile(r"^\s*(?:YOU are the launcher-spawned `|For project_id: job_|"
                           r"Continue the `[^`]+` pipeline for project_id: job_|Revise stage '|"
                           r"Run the `[^`]+` pipeline)")
# The stage text can itself contain quotes (motion sample: "...phase='motion_sample'").
REVISE_RE = re.compile(r"Revise stage '(.*?)' for project_id: (job_[0-9a-f]{12}) per this "
                       r"feedback: (.*)", re.S)
# What the runner appends after the reviewer's note (runner._revise_prompt).
REVISE_TEMPLATE_END = (" Rewrite that stage's checkpoint with status='awaiting_human' and STOP "
                       "for approval.")
REVISE_NOTE_SUFFIXES = (". Regenerate only shots ", ". Flagged shots (1-based): ",
                        ". All current stills.", ". MODE=")
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
STALE_SESSION = timedelta(hours=2)
# A finished job's session that has been quiet this long is over (an orphaned claude child
# that outlived a launcher restart can still be writing for a while).
IDLE_AFTER_TERMINAL = timedelta(minutes=10)
# Media still being written (ffmpeg, downloads) is picked up on a later run.
YOUNG_FILE = timedelta(minutes=2)
DEFAULT_CLAUDE_TIMEOUT_S = 3600.0
HIGGSFIELD_PREFIX = "mcp__claude_ai_Higgsfield__"

# Content limits for sanitized text kept in the database (characters).
LIMIT_PROMPT = 64_000
LIMIT_TEXT = 16_000
LIMIT_TOOL_ARGS = 16_000
LIMIT_TOOL_RESULT = 8_000
LIMIT_COMMAND_ERROR = 4_000
LIMIT_MCP_RESULT = 32_000
LIMIT_JSON_BYTES = 256_000
LIMIT_QUESTION = 8_000
# Tools whose successful output is file text or command output: stored as size and hash
# only (the fixed position: no file contents). Failures keep a clipped, redacted excerpt.
OUTPUT_AS_HASH_TOOLS = {"Bash", "BashOutput", "Grep", "NotebookRead"}

MEDIA_TYPES = {
    ".mp4": ("video", "video/mp4"), ".mov": ("video", "video/quicktime"),
    ".webm": ("video", "video/webm"), ".mkv": ("video", "video/x-matroska"),
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"), ".webp": ("image", "image/webp"),
    ".gif": ("image", "image/gif"),
    ".mp3": ("audio", "audio/mpeg"), ".wav": ("audio", "audio/wav"),
    ".m4a": ("audio", "audio/mp4"), ".aac": ("audio", "audio/aac"),
    ".flac": ("audio", "audio/flac"), ".ogg": ("audio", "audio/ogg"),
    ".json": ("data", "application/json"), ".jsonl": ("data", "application/x-ndjson"),
    ".md": ("document", "text/markdown"), ".txt": ("document", "text/plain"),
    ".html": ("document", "text/html"), ".srt": ("document", "application/x-subrip"),
    ".vtt": ("document", "text/vtt"), ".log": ("log", "text/plain"),
}
PROBE_TYPES = {"video", "audio", "image"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".remotion", ".cache"}
SCENE_RE = re.compile(r"(?<![A-Za-z0-9])(sc(?:ene)?[_\-]?\d{1,3})(?![0-9])", re.I)
LABEL_SUFFIX_RE = re.compile(r"(?:_revise|_continue_\d+|_pacing_auto_\d+|_after_plan_\d+|"
                             r"_after_hero_\d+)$")


# ===========================================================================
# Redaction — everything that leaves this user goes through Redactor
# ===========================================================================

REDACTION_POLICY = "panda-ingest-redact-v2"

_SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
        re.S)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|or-v1-|or-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("elevenlabs_key", re.compile(r"\bsk_[0-9a-f]{40,64}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    ("discord_webhook", re.compile(
        r"https://(?:[a-z]+\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_\-]+")),
    # user:password@ in any URI (a DSN too); the password may contain '/'.
    ("url_password", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,20}://[^\s/@:]+:)([^\s@]+)(@)")),
    ("auth_header", re.compile(
        r"(?i)\b(authorization|proxy-authorization|x-api-key|xi-api-key|api-key|cookie|set-cookie)"
        r"([ \t]*[:=][ \t]*)(\"[^\"\n]*\"|'[^'\n]*'|[^\n]+)")),
    ("bearer", re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]{16,}=*")),
    # --password X, --api-key=X, --token X ...; curl -u user:pass; aws configure set ... X
    ("cli_secret_flag", re.compile(
        r"(?i)((?<![A-Za-z0-9_])--?(?:password|passwd|pass|api[-_]?key|apikey|token|"
        r"access[-_]?token|auth[-_]?token|secret|secret[-_]?key|access[-_]?key|client[-_]?secret|"
        r"private[-_]?key|key)(?:[ \t]+|=))(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)")),
    ("basic_auth_flag", re.compile(r"((?<![A-Za-z0-9_])(?:-u|--user)[ \t]+[^\s:\"']+:)([^\s\"']+)")),
    ("aws_configure", re.compile(
        r"(?i)(\baws\s+configure\s+set\s+\S*(?:secret|key|token)\S*\s+)(\S+)")),
)
# Bounded: an unbounded name class backtracks quadratically on long base64url-like tokens.
_SECRET_NAME = (r"[A-Za-z0-9_.\-]{0,64}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|PASS(?![A-Za-z])|"
                r"CREDENTIAL|AUTH|DSN|WEBHOOK|SIGNATURE|COOKIE|SESSION)[A-Za-z0-9_.\-]{0,64}")
# NAME=value / "NAME": "value" / 'NAME': 'value' / \"NAME\":\"value\" (JSON inside a string).
_SECRET_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])((?:\\?[\"'])?" + _SECRET_NAME + r"(?:\\?[\"'])?)([ \t]*[=:][ \t]*)"
    r"(\\\"(?:[^\"\\\n]|\\(?!\"))*\\\"|\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;}\\]+)")
_HARMLESS_VALUE = re.compile(
    r"^(?:|\"\"|''|\\\"\\\"|true|false|null|none|0|1|yes|no|on|off|\*+|\[redacted[^\]]*\]|"
    r"<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)$", re.I)
_URL = re.compile(r"\b(?:https?|wss?|s3|gs|ftp)://[^\s\"'<>()\[\]{}]+", re.I)
# A percent-encoded link (inside another URL's query, or a form body).
_ENCODED_URL = re.compile(r"(?i)\b(?:https?|wss?)%3A%2F%2F[^\s\"'<>()\[\]{}&]+")
_URL_USERINFO = re.compile(r"^([a-z][a-z0-9+.\-]*://)(?:[^/@\s:]+:[^@\s]+|[^/@\s]+)@", re.I)
_BASE64_RUN = re.compile(r"(?:data:[a-z]+/[a-z0-9.+\-]+;base64,)?[A-Za-z0-9+/]{400,}={0,2}")
_HIGH_ENTROPY = re.compile(
    r"(?<![A-Za-z0-9_\-/.])[A-Za-z0-9_\-+/]{40,}={0,2}(?![A-Za-z0-9_\-/.])")
_HEX_OR_UUID = re.compile(r"^(?:[0-9a-f]+|[0-9A-F]+|[0-9a-f\-]{36})$")
# JSON keys whose string values are credentials whatever they look like (hex and UUID too).
_JSON_SECRET_NAME = re.compile(
    r"(?:^|_)(?:api_?key|apikey|secret|secretkey|secret_?key|password|passwd|pwd|pass|token|"
    r"access_?token|refresh_?token|id_?token|auth|auth_?token|authorization|credentials?|cookie|"
    r"signature|sig|private_?key|access_?key|session_?key|session_?token|client_?secret|dsn|"
    r"webhook(?:_?url)?)(?:_|$)")
# Keys ending in "key" (FAL_KEY, HIGGSFIELD_KEY): credentials when the value looks like one.
_JSON_KEYISH_NAME = re.compile(r"(?:^|_)[a-z0-9]*key$")
_JSON_SAFE_KEYS = {
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "thinking_tokens", "max_tokens", "total_tokens", "tokens", "token_count",
    "max_output_tokens", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
    "tool_use_id", "max_budget_tokens", "output_tokens_details", "token_usage",
    # this exporter's own keys and common non-secret *_key names
    "event_key", "run_key", "record_key", "domain_key", "dedupe_key", "sort_key", "cache_key",
    "scene_key", "section_key", "logical_key", "group_key", "params_key", "idempotency_key",
    "primary_key", "foreign_key", "partition_key", "object_key",
}
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{2,}$")
_ENV_DUMP_LINE = re.compile(
    r"^(?:export\s+|declare\s+-[A-Za-z]+\s+)?[A-Z_][A-Z0-9_]{2,}=.*$", re.M)
_ENV_ITEM = re.compile(r"^(?:export\s+|declare\s+-[A-Za-z]+\s+)?[A-Z_][A-Z0-9_]{2,}=")
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)",
                      re.S)
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def clean_str(value: str) -> str:
    """PostgreSQL text/jsonb cannot hold U+0000, and a lone surrogate (a transcript cut in
    the middle of an emoji) cannot be encoded as UTF-8: both become U+FFFD."""
    if "\x00" in value:
        value = value.replace("\x00", "\ufffd")
    return _LONE_SURROGATE.sub("\ufffd", value)


def clean_value(value: Any) -> Any:
    """clean_str over every string (keys included) of a row."""
    if isinstance(value, str):
        return clean_str(value)
    if isinstance(value, dict):
        return {clean_str(str(k)): clean_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_value(v) for v in value]
    return value


def sha256_hex(data: Any) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    elif not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _entropy(text: str) -> float:
    counts: Dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def redact_url(url: str) -> str:
    """Drop userinfo, every query string and the fragment: signed links carry credentials
    there (S3/CloudFront/GCS/Azure/Dify), and no investigation needs them."""
    url = _URL_USERINFO.sub(lambda m: m.group(1) + "[redacted]@", url)
    base, sep, _query = url.partition("?")
    base = base.partition("#")[0]
    return base + "?[query-redacted]" if sep else base


def _secret_key_kind(name: str) -> Optional[str]:
    """'strong' for credential names, 'keyish' for *KEY names, None for anything else."""
    norm = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if name in _JSON_SAFE_KEYS or norm in _JSON_SAFE_KEYS:
        return None
    if _JSON_SECRET_NAME.search(norm):
        return "strong"
    if _JSON_KEYISH_NAME.search(norm):
        return "keyish"
    return None


class Redactor:
    """Sanitizes text and JSON; `hits` counts what was removed (kept in row metadata)."""

    def __init__(self) -> None:
        self.hits: Dict[str, int] = {}

    def _hit(self, kind: str, n: int = 1) -> None:
        self.hits[kind] = self.hits.get(kind, 0) + n

    def text(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        out = clean_str(value)
        out = _BASE64_RUN.sub(self._base64_sub, out)
        out = _URL.sub(self._url_sub, out)
        out = _ENCODED_URL.sub(self._encoded_url_sub, out)
        for kind, pattern in _SECRET_PATTERNS:
            out = pattern.sub(lambda m, k=kind: self._secret_sub(m, k), out)
        if len(_ENV_DUMP_LINE.findall(out)) >= 5:
            # An environment dump or .env listing: keep the names, drop every value.
            out = _ENV_DUMP_LINE.sub(self._env_line_sub, out)
        out = _SECRET_ASSIGN.sub(self._assign_sub, out)
        out = _HIGH_ENTROPY.sub(self._entropy_sub, out)
        return out

    def _env_line_sub(self, m: "re.Match[str]") -> str:
        name = m.group(0).split("=", 1)[0]
        self._hit("env_dump_value")
        return f"{name}=[redacted]"

    def _base64_sub(self, m: "re.Match[str]") -> str:
        self._hit("base64")
        blob = m.group(0)
        return f"[base64 omitted: {len(blob)} chars sha256={sha256_hex(blob)[:16]}]"

    def _secret_sub(self, m: "re.Match[str]", kind: str) -> str:
        self._hit(kind)
        if kind == "auth_header":
            return f"{m.group(1)}{m.group(2)}[redacted]"
        if kind == "url_password":
            return f"{m.group(1)}[redacted]{m.group(3)}"
        if kind == "bearer":
            return f"{m.group(1)} [redacted]"
        if kind in ("cli_secret_flag", "basic_auth_flag", "aws_configure"):
            return f"{m.group(1)}[redacted]"
        return f"[redacted:{kind}]"

    def _assign_sub(self, m: "re.Match[str]") -> str:
        value = m.group(3)
        if _HARMLESS_VALUE.match(value.strip()) or "[redacted" in value:
            return m.group(0)
        self._hit("secret_assignment")
        return f"{m.group(1)}{m.group(2)}[redacted]"

    def _url_sub(self, m: "re.Match[str]") -> str:
        url = m.group(0)
        clean = redact_url(url)
        if clean != url:
            self._hit("url")
        return clean

    def _encoded_url_sub(self, m: "re.Match[str]") -> str:
        url = m.group(0)
        cut = min((i for i in (url.upper().find("%3F"), url.upper().find("%23")) if i >= 0),
                  default=-1)
        if cut < 0:
            return url
        self._hit("url")
        return url[:cut] + "%3F[query-redacted]"

    def _entropy_sub(self, m: "re.Match[str]") -> str:
        token = m.group(0)
        if _HEX_OR_UUID.match(token) or token.startswith("[") or "redacted" in token:
            return token
        kinds = (any(c.islower() for c in token), any(c.isupper() for c in token),
                 any(c.isdigit() for c in token))
        if sum(kinds) < 3 or _entropy(token) < 4.2:
            return token
        self._hit("high_entropy")
        return f"[redacted:high_entropy {len(token)} chars]"

    def key(self, name: str) -> str:
        """A dict key through the text rules (a signed URL or a token can be a key)."""
        clean = self.text(name) or ""
        if clean != name and len(clean) > 200:
            clean = f"[key {sha256_hex(name)[:16]}]"
        return clean

    def _secret_value(self, kind: Optional[str], item: Any) -> bool:
        if kind is None or isinstance(item, bool) or not isinstance(item, (str, int, float)):
            return False
        text = str(item).strip()
        if _HARMLESS_VALUE.match(text):
            return False
        if kind == "strong":
            return True
        # *KEY names: only values shaped like a credential (no spaces, no path, long enough).
        return isinstance(item, str) and len(text) >= 16 and not re.search(r"[\s/]", text)

    def json(self, value: Any, depth: int = 0) -> Any:
        if depth > 40:
            return "[depth limit]"
        if isinstance(value, dict):
            # An environment dump as JSON ({"PATH": ..., "FAL_KEY": ...}): keep names only.
            env_like = sum(1 for k in value if isinstance(k, str) and _ENV_NAME.match(k)) >= 5
            out: Dict[str, Any] = {}
            for key, item in value.items():
                raw_name = str(key)
                name = self.key(raw_name)
                while name in out:
                    name += "#"
                if raw_name in ("data", "base64") and isinstance(item, str) and len(item) > 200:
                    self._hit("base64")
                    out[name] = f"[omitted: {len(item)} chars sha256={sha256_hex(item)[:16]}]"
                elif env_like and _ENV_NAME.match(raw_name) and isinstance(item, (str, int, float)) \
                        and not isinstance(item, bool) and not _HARMLESS_VALUE.match(str(item)):
                    self._hit("env_dump_value")
                    out[name] = "[redacted]"
                elif self._secret_value(_secret_key_kind(raw_name), item):
                    self._hit("json_secret_key")
                    out[name] = "[redacted]"
                else:
                    out[name] = self.json(item, depth + 1)
            return out
        if isinstance(value, list):
            # Docker/compose style environment lists: ["PATH=...", "FAL_KEY=...", ...]
            items = [v for v in value if isinstance(v, str)]
            if len(items) >= 5 and sum(1 for v in items if _ENV_ITEM.match(v)) >= 5:
                out_list = []
                for item in value:
                    if isinstance(item, str) and _ENV_ITEM.match(item):
                        self._hit("env_dump_value")
                        out_list.append(item.split("=", 1)[0] + "=[redacted]")
                    else:
                        out_list.append(self.json(item, depth + 1))
                return out_list
            return [self.json(item, depth + 1) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value


def clip(text: Optional[str], limit: int) -> Tuple[Optional[str], bool]:
    """Head and tail of an over-long text with the cut marked; (text, truncated)."""
    if text is None or len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n[... {cut} chars omitted ...]\n{text[-tail:]}", True


def clip_json(value: Any, limit_bytes: int) -> Tuple[Any, bool]:
    """A JSON value small enough to store, or a summary of its shape when it is not."""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= limit_bytes:
        return value, False
    if isinstance(value, dict):
        return {"_truncated": True, "_bytes": len(raw), "_keys": sorted(map(str, value))[:200]}, True
    if isinstance(value, list):
        return {"_truncated": True, "_bytes": len(raw), "_items": len(value)}, True
    text, _cut = clip(str(value), limit_bytes // 2)
    return text, True


def summarize_question(question: Optional[str]) -> Optional[str]:
    """A leg timeout puts the whole claude argv (prompt and brief) into the question, often
    wrapped ("Background processing stopped ... (error: Command '[...]' timed out after N
    seconds)"): the argv is replaced, the rest of the text is kept."""
    if not question:
        return question
    start = question.find("Command '[")
    if start < 0:
        return question
    end = -1
    for marker in ("' timed out after", "' returned non-zero exit status", "' died with"):
        found = question.rfind(marker)
        if found > start:
            end = max(end, found)
    if end < 0:
        return question[:start] + "Command [claude command line omitted]"
    return question[:start] + "Command [claude command line omitted]" + question[end + 1:]


def safe_relpath(rel: str) -> str:
    """A project-relative path that is safe to store: a query-like tail (a file saved by
    `wget <signed url>` keeps '?Expires=..&Signature=..' in its name) becomes a hash, and '#'
    is percent-encoded, so no stored location carries a query string or fragment."""
    rel = rel.replace("\\", "/")
    if "?" in rel:
        head, _sep, tail = rel.partition("?")
        rel = f"{head}%3F[query {sha256_hex(tail)[:12]}]"
    return rel.replace("#", "%23")


_ENUM_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,59}$")


def enum_value(value: Any) -> Optional[str]:
    """A short status-like value (pass, fail_timing, completed); anything else is dropped."""
    return value if isinstance(value, str) and _ENUM_VALUE.match(value) else None


def strip_heredocs(command: str) -> Tuple[str, int]:
    """Replace heredoc bodies (`cat > f <<'EOF' ... EOF`: the file a leg writes) with their
    size and hash; (command, bodies replaced)."""
    count = 0

    def sub(m: "re.Match[str]") -> str:
        nonlocal count
        count += 1
        body = m.group(3)
        head = m.group(0)[: m.start(3) - m.start(0)]
        return (f"{head}[heredoc body omitted: {len(body)} chars sha256="
                f"{sha256_hex(body)[:16]}]\n{m.group(2)}")

    return _HEREDOC.sub(sub, command), count


# ===========================================================================
# Small helpers
# ===========================================================================

def did(*parts: Any) -> str:
    """Deterministic row id: uuid5 of the row's domain key."""
    return str(uuid.uuid5(ID_NAMESPACE, "\x1f".join(str(p) for p in parts)))


def hash_id(value: str) -> str:
    """Join key for a provider job id (the raw id stays in the sanitized payloads)."""
    return sha256_hex(value)[:32]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", text)
        if not match:
            return None
        frac = (match.group(2) or "")[:7]
        try:
            parsed = datetime.fromisoformat(match.group(1) + frac + match.group(3))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ms_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    if not start or not end:
        return None
    return max(int(round((end - start).total_seconds() * 1000)), 0)


def mtime_dt(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def read_json(path: Path) -> Tuple[Optional[Any], Optional[bytes]]:
    """(parsed JSON, raw bytes); (None, raw) when the file is mid-write or not JSON."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None
    try:
        return json.loads(raw.decode("utf-8", "replace")), raw
    except ValueError:
        return None, raw


def file_sha256(path: Path) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def stage_of_label(label: str, pipeline: Optional[str] = None) -> str:
    """Pipeline stage of a leg label (timing.jsonl 'stage' holds the label). The start leg is
    scene_plan for panda-image (it has no script stage) and script everywhere else."""
    base = label or "unknown"
    while True:
        stripped = LABEL_SUFFIX_RE.sub("", base)
        if stripped == base:
            break
        base = stripped
    if base.startswith("start"):
        return "scene_plan" if pipeline == "panda-image" else "script"
    if base in ("hero_still", "stills", "motion_sample") or base.startswith("assets"):
        return "assets"
    return base or "unknown"


def scene_of(text: str) -> Optional[str]:
    match = SCENE_RE.search(text or "")
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return f"sc{int(digits)}" if digits else None


def iter_jsonl(path: Path) -> Iterator[Tuple[int, str, Dict[str, Any]]]:
    """Complete JSON lines only, streamed: (line number, raw line, record). Lines are split on
    b'\\n' alone: JSON.stringify leaves U+2028/U+2029 raw inside strings, so splitting the
    decoded text with str.splitlines() would cut such records in two. A last line without
    its newline is still being written and is skipped."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        for number, raw in enumerate(handle):
            if not raw.endswith(b"\n"):
                break
            line = raw.rstrip(b"\r\n").decode("utf-8", "replace")
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield number, line, record


def read_jsonl(path: Path) -> List[Tuple[int, str, Dict[str, Any]]]:
    return list(iter_jsonl(path))


# ===========================================================================
# Settings
# ===========================================================================

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# Paths and the leg timeout only: no credential is ever read from the launcher .env.
ENV_KEYS = ("DIFY_DATA_DIR", "OPENMONTAGE_PROJECTS_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_TIMEOUT_S")


def parse_env_text(text: str) -> Dict[str, str]:
    """KEY=VALUE lines as a shell assigns them, without executing anything."""
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        match = _ENV_LINE.match(raw)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2)
        value = raw_value.strip()
        if value[:1] in {'"', "'"}:
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            comment = re.search(r"\s#", raw_value)
            value = (raw_value[:comment.start()] if comment else raw_value).strip()
        values[key] = value
    return values


def claude_project_slug(path: Path) -> str:
    """Claude Code's folder name for a working directory (non-alphanumerics become '-')."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(path))
    if len(slug) > 200:
        slug = slug[:200] + "-" + sha256_hex(str(path))[:16]
    return slug


@dataclass
class Settings:
    repo: Path
    data_dir: Path
    projects_dir: Path
    transcript_dirs: List[Path]
    mcp_log_dirs: List[Path]
    spool: Path
    state_dir: Path
    host_label: str
    ffprobe: Optional[str]
    claude_timeout_s: float = DEFAULT_CLAUDE_TIMEOUT_S


def _resolve(value: str, repo: Path, default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo / path


def load_settings(args: argparse.Namespace) -> Settings:
    repo = Path(args.repo or os.environ.get("PANDA_INGEST_REPO") or
                Path(__file__).resolve().parents[1]).expanduser().resolve()
    dotenv: Dict[str, str] = {}
    env_file = repo / ".env"
    if env_file.is_file():
        try:
            parsed = parse_env_text(env_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            parsed = {}
        dotenv = {k: v for k, v in parsed.items() if k in ENV_KEYS}   # paths only, no secrets

    def setting(key: str) -> str:
        return os.environ.get(key) or dotenv.get(key, "")

    data_dir = Path(args.data_dir) if args.data_dir else _resolve(
        os.environ.get("PANDA_INGEST_DATA_DIR") or setting("DIFY_DATA_DIR"), repo, repo / "data")
    projects_dir = Path(args.projects_dir) if args.projects_dir else _resolve(
        os.environ.get("PANDA_INGEST_PROJECTS_DIR") or setting("OPENMONTAGE_PROJECTS_DIR"),
        repo, repo / "projects")
    slug = claude_project_slug(repo)
    if args.transcripts_dir:
        transcript_dirs = [Path(p).expanduser() for p in args.transcripts_dir]
    else:
        config_dir = Path(setting("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
        transcript_dirs = [config_dir / "projects" / slug]
    if args.mcp_logs_dir:
        mcp_dirs = [Path(p).expanduser() for p in args.mcp_logs_dir]
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").expanduser()
        mcp_dirs = [cache / "claude-cli-nodejs" / slug]
    spool = Path(args.spool or os.environ.get("PANDA_INGEST_SPOOL") or "/var/spool/panda-ingest")
    state_dir = Path(args.state_dir or os.environ.get("PANDA_INGEST_STATE_DIR")
                     or Path.home() / ".local" / "state" / "panda-ingest").expanduser()
    host = os.environ.get("PANDA_INGEST_HOST_LABEL") or socket.gethostname()
    ffprobe = None if args.no_probe else (shutil.which("ffprobe")
                                          or shutil.which("ffprobe", path="/usr/local/bin:/usr/bin"))
    timeout = as_float(os.environ.get("PANDA_INGEST_CLAUDE_TIMEOUT_S") or
                       setting("CLAUDE_TIMEOUT_S")) or DEFAULT_CLAUDE_TIMEOUT_S
    return Settings(repo=repo, data_dir=data_dir.resolve(), projects_dir=projects_dir.resolve(),
                    transcript_dirs=transcript_dirs, mcp_log_dirs=mcp_dirs,
                    spool=spool.expanduser(), state_dir=state_dir, host_label=host,
                    ffprobe=ffprobe, claude_timeout_s=timeout)


# ===========================================================================
# Row collection
# ===========================================================================

PRIMARY_KEYS: Dict[str, str] = {
    "jobs": "job_id",
    "ingestion_sources": "ingestion_source_id",
    "ingestion_records": "ingestion_record_id",
    "job_state_observations": "job_state_observation_id",
    "checkpoints": "checkpoint_id",
    "decisions": "decision_row_id",
    "agent_runs": "agent_run_id",
    "artifacts": "artifact_id",
    "cost_observations": "cost_observation_id",
    "timing_events": "timing_event_id",
    "llm_inferences": "llm_inference_id",
    "llm_blocks": "llm_block_id",
    "trace_spans": "span_id",
    "span_links": "span_link_id",
    "runtime_events": "runtime_event_id",
    "approval_events": "approval_event_id",
}
# Projection rows that are re-sent when they change; every other table is append-only.
MUTABLE_TABLES = {"jobs", "artifacts"}


class Rows:
    """Rows for one job, deduplicated by primary key (the first version wins)."""

    def __init__(self) -> None:
        self._rows: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add(self, table: str, row: Dict[str, Any]) -> str:
        pk = str(row[PRIMARY_KEYS[table]])
        self._rows.setdefault((table, pk), row)
        return pk

    def has(self, table: str, pk: Optional[str]) -> bool:
        return pk is not None and (table, pk) in self._rows

    def items(self) -> List[Tuple[str, Dict[str, Any]]]:
        return [(table, row) for (table, _pk), row in self._rows.items()]

    def __len__(self) -> int:
        return len(self._rows)


@dataclass
class Source:
    """One input file (provenance)."""
    source_id: str
    kind: str
    logical_path: str


class JobContext:
    """Everything known about one job while its rows are built."""

    def __init__(self, job_id: str, settings: Settings, exported_at: datetime,
                 cache: Dict[str, Any]) -> None:
        self.job_id = job_id
        self.settings = settings
        self.exported_at = exported_at
        self.cache = cache                                   # exporter state (sha/probe cache)
        self.rows = Rows()
        self.trace_id = did("trace", job_id)
        self.root_span_id = did("span", job_id, "job")
        self.state: Dict[str, Any] = {}
        self.project: Dict[str, Any] = {}
        self.checkpoints: List[Dict[str, Any]] = []          # parsed snapshots, all versions
        self.legs: List[Dict[str, Any]] = []                 # timing rows, ordered by end
        self.attempt_logs: Dict[str, List[List[Dict[str, Any]]]] = {}
        self.sessions: List["Session"] = []
        self.manifest_items: Dict[str, Dict[str, Any]] = {}  # path/basename -> manifest entry
        self.referenced_paths: Set[str] = set()
        self.provider_jobs: Dict[str, Dict[str, Any]] = {}   # provider job hash -> lifecycle
        self.asset_job_links: List[Tuple[str, str]] = []     # (provider job hash, asset path)
        self.artifact_ids: Dict[str, str] = {}               # project-relative path -> id
        self.tool_runs: List[Dict[str, Any]] = []            # events.jsonl finishes
        self.warnings: List[str] = []
        # Rows held back until time alone makes them final (a running session, media still
        # being written): while any remain, the job is rechecked even when nothing changed.
        self.deferred: List[str] = []
        self.timeout_s = settings.claude_timeout_s

    @property
    def terminal(self) -> bool:
        return str(self.state.get("status") or "") in TERMINAL_STATUSES

    @property
    def pipeline(self) -> Optional[str]:
        value = self.state.get("pipeline") or self.project.get("pipeline_type")
        return str(value) if value else None

    # -- provenance -------------------------------------------------------------
    def source(self, kind: str, logical_path: str, metadata: Optional[Dict[str, Any]] = None
               ) -> Source:
        logical_path = safe_relpath(logical_path)
        source_id = did("src", self.job_id, kind, logical_path)
        self.rows.add("ingestion_sources", {
            "ingestion_source_id": source_id,
            "job_id": self.job_id,
            "source_kind": kind,
            "canonical_path": logical_path[:1000],
            "file_identity": "path",
            "first_seen_at": iso(self.exported_at),
            "metadata_redacted": dict({"parser": PARSER_VERSION,
                                       "host": self.settings.host_label}, **(metadata or {})),
        })
        return Source(source_id, kind, logical_path)

    def record(self, source: Source, record_key: str, content_sha: str,
               source_ts: Optional[datetime], metadata: Optional[Dict[str, Any]] = None) -> str:
        record_key = safe_relpath(record_key)
        record_id = did("rec", source.source_id, record_key)
        self.rows.add("ingestion_records", {
            "ingestion_record_id": record_id,
            "ingestion_source_id": source.source_id,
            "record_key": record_key[:1000],
            "content_sha256": content_sha,
            "source_timestamp": iso(source_ts),
            "observed_at": iso(self.exported_at),
            "metadata_redacted": dict({"redaction_policy": REDACTION_POLICY}, **(metadata or {})),
        })
        return record_id

    def event(self, name: str, dedupe: str, *, source_service: str, severity: str = "info",
              occurred_at: Optional[datetime] = None, stage: Optional[str] = None,
              scene_id: Optional[str] = None, outcome: Optional[str] = None,
              message: Optional[str] = None, attributes: Optional[Dict[str, Any]] = None,
              span_id: Optional[str] = None, agent_run_id: Optional[str] = None,
              record_id: Optional[str] = None, artifact_id: Optional[str] = None) -> None:
        red = Redactor()
        text, _cut = clip(red.text(message) if message else None, 4000)
        attrs, _big = clip_json(red.json(attributes or {}), 64_000)
        if not isinstance(attrs, dict):
            attrs = {"value": attrs}
        dedupe = safe_relpath(dedupe)
        self.rows.add("runtime_events", {
            "runtime_event_id": did("evt", self.job_id, dedupe),
            "job_id": self.job_id,
            "trace_id": self.trace_id,
            "span_id": span_id,
            "agent_run_id": agent_run_id,
            "ingestion_record_id": record_id,
            "dedupe_key": (red.text(dedupe) or "")[:500],
            "event_name": name[:120],
            "source_service": source_service,
            "severity": severity,
            "stage": stage,
            "scene_id": scene_id,
            "artifact_id": artifact_id,
            "outcome": clip(red.text(outcome), 200)[0] if outcome else None,
            "occurred_at": iso(occurred_at),
            "message_redacted": text,
            "attributes_redacted": attrs,
        })

    def project_path(self, *parts: str) -> Path:
        return self.settings.projects_dir / self.job_id / Path(*parts)

    def launcher_path(self, *parts: str) -> Path:
        return self.settings.data_dir / "jobs" / self.job_id / Path(*parts)

    def leg_at(self, moment: Optional[datetime], slack_s: float = 2.0) -> Optional[Dict[str, Any]]:
        if moment is None:
            return None
        slack = timedelta(seconds=slack_s)
        for leg in self.legs:
            if leg["start"] - slack <= moment <= leg["end"] + slack:
                return leg
        return None


def _span(ctx: JobContext, span_id: str, *, kind: str, name: str, service: str,
          stage: Optional[str] = None, parent: Optional[str] = None,
          started: Optional[datetime] = None, ended: Optional[datetime] = None,
          transport: str = "success", outcome: Optional[str] = None,
          record_id: Optional[str] = None, attributes: Optional[Dict[str, Any]] = None,
          method: str = "derived", confidence: float = 1.0,
          session: Optional["Session"] = None, tool_use_id: Optional[str] = None,
          provider_hash: Optional[str] = None, request_block: Optional[str] = None,
          response_block: Optional[str] = None, error: Optional[str] = None,
          operation_id: Optional[str] = None, scene_id: Optional[str] = None,
          attempt_no: Optional[int] = None, agent_run_id: Optional[str] = None
          ) -> Dict[str, Any]:
    attrs, _cut = clip_json(Redactor().json(attributes or {}), 64_000)
    if stage is None and session is not None:
        stage = session.stage
    return {
        "span_id": span_id,
        "trace_id": ctx.trace_id,
        "job_id": ctx.job_id,
        "agent_run_id": agent_run_id or (session.agent_run_id if session else None),
        "claude_session_id": session.session_id if session else None,
        "parent_span_id": parent,
        "operation_id": operation_id,
        "ingestion_record_id": record_id,
        "span_kind": kind,
        "name": name[:300],
        "source_service": service,
        "stage": stage,
        "scene_id": scene_id,
        "attempt_no": attempt_no,
        "tool_use_id": tool_use_id,
        "provider_job_id_hash": provider_hash,
        "transport_status": transport,
        "semantic_outcome": outcome,
        "started_at": iso(started),
        "ended_at": iso(ended),
        "duration_ms": ms_between(started, ended),
        "error_class": None,
        "error_message_redacted": error,
        "request_block_id": request_block,
        "response_block_id": response_block,
        "attributes_redacted": attrs if isinstance(attrs, dict) else {"value": attrs},
        "correlation_method": method,
        "correlation_confidence": round(confidence, 3),
    }


# ===========================================================================
# Job folder: launcher state, project marker, checkpoints, decisions
# ===========================================================================

class DeferJob(Exception):
    """The job cannot be exported consistently right now (a file is mid-write): retry later."""


def build_job_rows(ctx: JobContext) -> None:
    state_path = ctx.launcher_path("state.json")
    state, raw = read_json(state_path)
    if raw is not None and not isinstance(state, dict):
        # The launcher rewrites state.json in place: a read can land mid-write. Exporting it
        # would project status 'unknown' with a version newer than the complete file's.
        modified = mtime_dt(state_path)
        if modified is None or ctx.exported_at - modified < timedelta(minutes=10):
            raise DeferJob("state.json is being rewritten")
        ctx.warnings.append("state.json is not valid JSON; exported without launcher state")
    ctx.state = state if isinstance(state, dict) else {}
    project, _ = read_json(ctx.project_path("project.json"))
    ctx.project = project if isinstance(project, dict) else {}
    inputs, _ = read_json(ctx.project_path("inputs", "inputs.json"))

    red = Redactor()
    st = ctx.state
    # The version is the state's own updated_at; the file mtime only stands in for a state
    # that parsed but has no updated_at (an unparsable one gets 0, so it never wins).
    updated = parse_ts(st.get("updated_at")) or (mtime_dt(state_path) if st else None)
    # Never the export clock: a value that changes every run would re-send the row forever.
    created = (parse_ts(ctx.project.get("created_at")) or parse_ts(st.get("created_at"))
               or parse_ts(st.get("processing_started_at")) or updated)
    status = str(st.get("status") or "unknown")
    finished = parse_ts(st.get("processing_finished_at"))
    brief, _ = clip(red.text(str(st.get("brief") or ctx.project.get("title") or "")), LIMIT_PROMPT)
    question_raw = st.get("question") if isinstance(st.get("question"), str) else None
    _note_timeout(ctx, question_raw)
    question, _ = clip(red.text(summarize_question(question_raw)), LIMIT_QUESTION)
    options = st.get("options") if isinstance(st.get("options"), dict) else {}
    options_red, _ = clip_json(red.json({k: v for k, v in options.items() if k != "media"}),
                               LIMIT_JSON_BYTES)
    input_list = inputs if isinstance(inputs, list) else (
        st.get("inputs") if isinstance(st.get("inputs"), list) else [])
    inputs_red, _ = clip_json(red.json(input_list), LIMIT_JSON_BYTES)
    version = int(updated.timestamp() * 1_000_000) if updated else 0
    job_row: Dict[str, Any] = {
        "job_id": ctx.job_id,
        "pipeline": str(st.get("pipeline") or ctx.project.get("pipeline_type") or "unknown"),
        "profile": st.get("profile"),
        "brief": brief or "",
        "options_redacted": options_red if isinstance(options_red, dict) else {},
        "inputs_redacted": inputs_red if isinstance(inputs_red, list) else [],
        "current_status": status,
        "current_stage": st.get("stage"),
        "current_gate": st.get("gate"),
        "current_question": question,
        "state_version": version,
        "processing_started_at": iso(parse_ts(st.get("processing_started_at"))),
        "processing_finished_at": iso(finished),
        "completed_at": iso(finished or updated) if status == "done" else None,
        "failed_at": iso(updated) if status == "failed" else None,
        "terminal_error_redacted": {"question": question} if status == "failed" else None,
        "created_at": iso(created),
        "trace_id": ctx.trace_id,
    }
    if created is None:
        job_row.pop("created_at")                     # unknown: let the database default it
    ctx.rows.add("jobs", job_row)
    if raw is None or not isinstance(state, dict):
        return
    source = ctx.source("state_json", f"launcher/jobs/{ctx.job_id}/state.json")
    content_sha = sha256_hex(raw)
    record_id = ctx.record(source, f"snapshot:{content_sha}", content_sha, updated)
    snapshot = {k: v for k, v in st.items() if k not in ("brief", "question")}
    if isinstance(snapshot.get("artifacts"), dict):
        # _checkpoint_artifacts repeats the latest checkpoint, which is stored on its own.
        snapshot["artifacts"] = {k: v for k, v in snapshot["artifacts"].items()
                                 if k != "_checkpoint_artifacts"}
    snap_red, _ = clip_json(red.json(snapshot), LIMIT_JSON_BYTES)
    ctx.rows.add("job_state_observations", {
        "job_state_observation_id": did("jso", ctx.job_id, content_sha),
        "job_id": ctx.job_id,
        "ingestion_record_id": record_id,
        "status": status,
        "stage": st.get("stage"),
        "gate": st.get("gate"),
        "question": question,
        "state_redacted": snap_red if isinstance(snap_red, dict) else {},
        "state_timestamp": iso(updated),
        "observed_at": iso(ctx.exported_at),
    })
    _collect_paths(st.get("artifacts"), ctx.referenced_paths)
    operation = str(st.get("_processing_operation") or "")
    started = parse_ts(st.get("processing_started_at"))
    if status == "running" and operation.startswith("resume:") and started:
        # Only this transient snapshot holds the exact /respond receipt time (the worker
        # restores the stale value when it ends), so record it whenever a sweep sees it.
        gate = operation.split(":", 1)[1]
        recovery = st.get("_recovery_gate")
        hint = None
        if isinstance(recovery, str) and recovery:
            hint = "revise_or_other" if recovery == gate else "approve"
        ctx.event("gate.response_received", f"respond:{gate}:{iso(started)}",
                  source_service="launcher", occurred_at=started, stage=st.get("stage"),
                  outcome=hint, record_id=record_id,
                  attributes={"gate": gate, "recovery_gate": recovery, "decision_hint": hint})
    if status == "failed":
        ctx.event("job.failed", f"job.failed:{content_sha}", source_service="launcher",
                  severity="error", occurred_at=updated, stage=st.get("stage"),
                  outcome="failed", message=question, record_id=record_id)
    if question_raw and "Background processing stopped" in question_raw:
        ctx.event("job.recovered", f"recovered:{content_sha}", source_service="launcher",
                  severity="warning", occurred_at=updated, stage=st.get("stage"),
                  outcome="orphan_recovered", message=question, record_id=record_id)


def _note_timeout(ctx: JobContext, text: Optional[str]) -> None:
    """A recovered timeout names the leg timeout the launcher really used."""
    match = re.search(r"timed out after ([0-9.]+) seconds", text or "")
    seconds = as_float(match.group(1)) if match else None
    if seconds and seconds > 60:
        ctx.timeout_s = seconds


def _collect_paths(value: Any, into: Set[str]) -> None:
    """Every string that looks like a file reference (as given and its basename)."""
    if isinstance(value, dict):
        for item in value.values():
            _collect_paths(item, into)
    elif isinstance(value, list):
        for item in value:
            _collect_paths(item, into)
    elif isinstance(value, str) and len(value) < 1024 and re.search(r"\.[A-Za-z0-9]{2,5}$", value):
        norm = value.replace("\\", "/")
        into.add(norm)
        into.add(norm.rsplit("/", 1)[-1])


def build_checkpoint_rows(ctx: JobContext) -> None:
    project_dir = ctx.project_path()
    files = sorted(project_dir.glob("checkpoint_*.json"))
    history_dir = project_dir / "history"
    history = sorted(history_dir.glob("checkpoint_*.json")) if history_dir.is_dir() else []
    seen: Set[Tuple[str, str]] = set()
    for path in history + files:
        data, raw = read_json(path)
        if not isinstance(data, dict) or raw is None:
            continue
        stage = str(data.get("stage") or path.stem.replace("checkpoint_", "").split("_")[0])
        content_sha = sha256_hex(raw)
        if (stage, content_sha) in seen:
            continue
        seen.add((stage, content_sha))
        rel = path.relative_to(project_dir).as_posix()
        source = ctx.source("checkpoint_json", f"projects/{ctx.job_id}/{rel}")
        ts = parse_ts(data.get("timestamp")) or mtime_dt(path) or ctx.exported_at
        record_id = ctx.record(source, f"snapshot:{content_sha}", content_sha, ts)
        red = Redactor()

        def red_json(key: str) -> Any:
            value = data.get(key)
            if value is None:
                return None
            out, _cut = clip_json(red.json(value), LIMIT_JSON_BYTES)
            return out

        artifacts = red_json("artifacts")
        error = data.get("error")
        partial = data.get("partial_progress") if isinstance(data.get("partial_progress"), dict) \
            else {}
        checkpoint_id = did("cp", ctx.job_id, stage, content_sha)
        if isinstance(data.get("question"), str):
            _note_timeout(ctx, data["question"])
        question, _ = clip(red.text(summarize_question(data.get("question")))
                           if isinstance(data.get("question"), str) else None, LIMIT_QUESTION)
        ctx.rows.add("checkpoints", {
            "checkpoint_id": checkpoint_id,
            "job_id": ctx.job_id,
            "ingestion_record_id": record_id,
            "stage": stage,
            "gate": None,
            "checkpoint_version": str(data.get("version") or "1.0"),
            "pipeline_type": str(data.get("pipeline_type") or "unknown"),
            "status": str(data.get("status") or "unknown"),
            "checkpoint_policy": data.get("checkpoint_policy"),
            "claimed_human_approval_required": bool(data.get("human_approval_required")),
            "claimed_human_approved": bool(data.get("human_approved")),
            "question": question,
            "artifacts_redacted": artifacts if isinstance(artifacts, dict) else {},
            "review_redacted": red_json("review"),
            "cost_snapshot_redacted": red_json("cost_snapshot"),
            "metadata_redacted": red_json("metadata"),
            "partial_progress_redacted": red_json("partial_progress"),
            "error_redacted": ({"message": clip(red.text(str(error)), 4000)[0]}
                               if error not in (None, "") and not isinstance(error, dict)
                               else red_json("error")),
            "checkpoint_timestamp": iso(ts),
            "content_sha256": content_sha,
        })
        current = path.parent == project_dir
        ctx.checkpoints.append({"stage": stage, "status": str(data.get("status") or ""),
                                "ts": ts, "sha": content_sha, "id": checkpoint_id,
                                "human_approved": bool(data.get("human_approved")),
                                "record_id": record_id, "current": current,
                                "data": data})
        if current:
            _collect_paths(data.get("artifacts"), ctx.referenced_paths)
        ctx.event("checkpoint.written", f"checkpoint:{stage}:{content_sha}",
                  source_service="pipeline", occurred_at=ts, stage=stage,
                  outcome=str(data.get("status") or "unknown"),
                  severity="error" if error else "info",
                  message=str(error) if error else None,
                  attributes={"human_approval_required": bool(data.get("human_approval_required")),
                              "human_approved": bool(data.get("human_approved")),
                              "phase": partial.get("phase")},
                  record_id=record_id)
        arts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
        for key in ("final_review", "render_report"):
            if isinstance(arts.get(key), dict):
                _review_event(ctx, key, arts[key], f"checkpoint:{stage}:{content_sha}", ts,
                              record_id)
    ctx.checkpoints.sort(key=lambda c: (c["ts"], c["stage"]))
    for leftover in project_dir.glob("checkpoint_*.json.tmp"):
        when = mtime_dt(leftover)
        if when and when < ctx.exported_at - timedelta(minutes=10):
            ctx.event("crash.leftover_temp_file", f"tmp:{leftover.name}:{iso(when)}",
                      source_service="pipeline", severity="warning", occurred_at=when,
                      outcome="write_interrupted", attributes={"file": leftover.name})


def _review_event(ctx: JobContext, kind: str, data: Dict[str, Any], origin: str,
                  when: Optional[datetime], record_id: Optional[str] = None) -> None:
    """final_review / render_report: delivery vs quality status, issues, render outputs."""
    attrs: Dict[str, Any] = {"origin": origin}
    outcome = None
    if kind == "final_review":
        outcome = str(data.get("status") or "") or None
        issues = data.get("issues_found")
        attrs.update(status=data.get("status"), recommended_action=data.get("recommended_action"),
                     issue_count=len(issues) if isinstance(issues, list) else None,
                     issues=issues, checks=data.get("checks"))
    else:
        outputs = data.get("outputs") if isinstance(data.get("outputs"), list) else []
        attrs.update(outputs=outputs, warnings=data.get("warnings"),
                     render_time_seconds=data.get("render_time_seconds"))
        outcome = "rendered" if outputs else None
    severity = "warning" if (kind == "final_review" and outcome not in (None, "pass", "passed",
                                                                         "approved")) else "info"
    ctx.event(f"review.{kind}", f"{kind}:{origin}", source_service="pipeline",
              severity=severity, occurred_at=when, stage="compose", outcome=outcome,
              attributes=attrs, record_id=record_id)


def build_decision_rows(ctx: JobContext) -> None:
    path = ctx.project_path("decision_log.json")
    data, raw = read_json(path)
    if not isinstance(data, dict) or raw is None:
        return
    source = ctx.source("decision_log_json", f"projects/{ctx.job_id}/decision_log.json")
    content_sha = sha256_hex(raw)
    record_id = ctx.record(source, f"snapshot:{content_sha}", content_sha, mtime_dt(path))
    for decision in data.get("decisions") or []:
        if not isinstance(decision, dict) or not decision.get("decision_id"):
            continue
        red = Redactor()
        key = str(decision["decision_id"])
        confidence = as_float(decision.get("confidence"))
        if confidence is not None and not 0 <= confidence <= 1:
            confidence = None
        options = decision.get("options_considered")
        ctx.rows.add("decisions", {
            "decision_row_id": did("dec", ctx.job_id, key),
            "job_id": ctx.job_id,
            "ingestion_record_id": record_id,
            "decision_key": key[:500],
            "stage": str(decision.get("stage") or "unknown"),
            "category": str(decision.get("category") or "unknown"),
            "subject": clip(red.text(str(decision.get("subject") or "")), 2000)[0] or "",
            "options_considered_redacted": red.json(options) if isinstance(options, list) else [],
            "selected_redacted": {"selected": red.json(decision.get("selected"))},
            "reason_redacted": clip(red.text(str(decision.get("reason") or "")), 8000)[0] or "",
            "user_visible": bool(decision.get("user_visible")),
            "claimed_user_approved": bool(decision.get("user_approved")),
            "confidence": confidence,
            "decided_at": None,
        })


# ===========================================================================
# Legs: timing.jsonl and agent logs
# ===========================================================================

_ATTEMPT_HEADER = re.compile(r"^===== attempt (\d+) rc=(\S*) =====\r?$", re.M)


def parse_agent_log(text: str) -> List[List[Dict[str, Any]]]:
    """Legs of one label, each a list of attempts {attempt, rc, stdout, stderr, raw, offset}."""
    text = text.replace("\r\n", "\n")
    invocations: List[List[Dict[str, Any]]] = []
    headers = list(_ATTEMPT_HEADER.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end():end]
        stdout, stderr = "", ""
        before = body
        if "\n--- stderr ---\n" in body:
            before, _sep, stderr = body.partition("\n--- stderr ---\n")
        if "--- stdout ---\n" in before:
            stdout = before.split("--- stdout ---\n", 1)[1]
        attempt = int(header.group(1))
        rc_text = header.group(2)
        entry = {"attempt": attempt,
                 "rc": int(rc_text) if re.fullmatch(r"-?\d+", rc_text) else None,
                 "stdout": stdout.strip("\n"), "stderr": stderr.strip("\n"),
                 "raw": text[header.start():end], "offset": header.start()}
        if attempt == 0 or not invocations:
            invocations.append([entry])
        else:
            invocations[-1].append(entry)
    return invocations


def build_leg_rows(ctx: JobContext) -> None:
    timing_path = ctx.project_path("artifacts", "timing.jsonl")
    lines = read_jsonl(timing_path)
    source = ctx.source("timing_jsonl", f"projects/{ctx.job_id}/artifacts/timing.jsonl") \
        if lines else None
    label_counts: Dict[str, int] = {}
    for number, line, entry in lines:
        end = parse_ts(entry.get("ts"))
        seconds = as_float(entry.get("seconds"))
        label = str(entry.get("stage") or "unknown")
        if end is None or seconds is None or source is None:
            continue
        start = end - timedelta(seconds=seconds)
        invocation = label_counts.get(label, 0)
        label_counts[label] = invocation + 1
        content_sha = sha256_hex(line)
        record_id = ctx.record(source, f"line:{number}:{content_sha}", content_sha, end,
                               {"line_number": number})
        ctx.legs.append({
            "label": label, "stage": stage_of_label(label, ctx.pipeline), "start": start,
            "end": end, "seconds": seconds, "invocation": invocation, "record_id": record_id,
            "span_id": did("span", ctx.job_id, "leg", label, entry.get("ts"), seconds),
            "event_key": f"{label}:{entry.get('ts')}:{seconds}", "sessions": [],
            "attempts": [], "timed_out": False, "log_match": "none"})
    ctx.legs.sort(key=lambda leg: leg["end"])
    for log_path in sorted(ctx.project_path("artifacts").glob("agent_*.log")):
        label = log_path.stem[len("agent_"):]
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ctx.attempt_logs[label] = parse_agent_log(text)


def assign_attempt_logs(ctx: JobContext) -> None:
    """Give each leg its agent-log block group. Every attempt that exits appends one block,
    starting at 'attempt 0'; an attempt killed by the leg timeout writes none, yet its leg
    still gets a timing line. So a leg whose first attempt timed out has no group at all,
    and joining groups to legs by position would hand its group to the wrong leg.

    A leg can only have timed out if it ran at least the timeout. Its sessions (one per
    attempt) then tell whether earlier attempts exited (a group) or not. Without sessions
    (transcripts gone), the number of groups decides how many such legs had one."""
    limit = ctx.timeout_s - 1.0
    for label, groups in ctx.attempt_logs.items():
        legs = [leg for leg in ctx.legs if leg["label"] == label]
        has: List[Optional[bool]] = []
        for leg in legs:
            sessions = [s for s in leg["sessions"] if not s.info.is_subagent]
            if leg["seconds"] < limit:
                has.append(True)
            elif sessions:
                has.append(len(sessions) >= 2)
            else:
                has.append(None)
        certain = sum(1 for h in has if h)
        unknown = [i for i, h in enumerate(has) if h is None]
        spare = max(0, min(len(unknown), len(groups) - certain))
        for position, index in enumerate(unknown):
            has[index] = position < spare
        cursor = 0
        for leg, with_group in zip(legs, has):
            if with_group and cursor < len(groups):
                leg["attempts"] = groups[cursor]
                cursor += 1
                leg["log_match"] = "ordered_with_timeout_check"
            sessions = [s for s in leg["sessions"] if not s.info.is_subagent]
            # Timed out: long enough, and fewer exited attempts than attempts started.
            leg["timed_out"] = leg["seconds"] >= limit and (
                len(leg["attempts"]) < max(len(sessions), 1))


def _run_totals(ctx: JobContext, session: "Session") -> Dict[str, Any]:
    """A leg attempt's totals including its subagents (their inferences carry its run id)."""
    totals = session.totals()
    for sub in ctx.sessions:
        if sub.info.is_subagent and sub.agent_run_id == session.agent_run_id:
            for key, value in sub.totals().items():
                totals[key] = (totals.get(key) or 0) + (value or 0)
    return totals


def finish_leg_rows(ctx: JobContext) -> None:
    """Agent runs (one per attempt), timing events, leg spans and gaps between legs.

    Runs after the sessions were parsed, so session totals are known. An attempt killed by
    the leg timeout writes no agent-log block: its session still makes it an agent run."""
    previous: Optional[Dict[str, Any]] = None
    awaiting = [c for c in ctx.checkpoints if c["status"] == "awaiting_human"]
    for leg in ctx.legs:
        log_source: Optional[Source] = None
        attempts = leg["attempts"]
        sessions = [s for s in leg["sessions"] if not s.info.is_subagent]
        count = max(len(attempts), len(sessions), 1)
        last_rc = attempts[-1].get("rc") if attempts else None
        if leg["timed_out"]:
            leg_transport = "timeout"
        else:
            leg_transport = "success" if last_rc == 0 else ("error" if last_rc is not None
                                                            else "unknown")
        ctx.rows.add("trace_spans", _span(
            ctx, leg["span_id"], kind="agent_leg", name=leg["label"], service="launcher",
            stage=leg["stage"], parent=ctx.root_span_id, started=leg["start"], ended=leg["end"],
            transport=leg_transport, record_id=leg["record_id"],
            outcome="timed_out" if leg["timed_out"] else None,
            attributes={"attempt_blocks": len(attempts), "sessions": len(sessions),
                        "invocation": leg["invocation"], "seconds": leg["seconds"],
                        "timeout_seconds": ctx.timeout_s if leg["timed_out"] else None,
                        "agent_log_match": leg["log_match"]},
            method="timing_jsonl"))
        first_run_id: Optional[str] = None
        for index in range(count):
            attempt = attempts[index] if index < len(attempts) else None
            session = sessions[index] if index < len(sessions) else None
            attempt_no = attempt["attempt"] if attempt is not None else index
            run_key = f"{leg['label']}:{leg['invocation']}:{attempt_no}"
            if attempt is not None:
                if log_source is None:
                    log_source = ctx.source(
                        "agent_log", f"projects/{ctx.job_id}/artifacts/agent_{leg['label']}.log")
                attempt_sha = sha256_hex(attempt["raw"])
                record_id = ctx.record(
                    log_source, f"attempt:{run_key}:{attempt['offset']}:{attempt_sha}",
                    attempt_sha, leg["end"])
            elif session is not None and session.record_id:
                record_id = session.record_id
            elif index == 0:
                record_id = leg["record_id"]
            else:
                continue
            # Session-based while the transcript exists; the loader skips the run_key-based
            # duplicate built after Claude Code deletes the transcript (first version wins).
            run_id = session.agent_run_id if session else did("run", ctx.job_id, run_key)
            first_run_id = first_run_id or run_id
            rc = attempt.get("rc") if attempt else None
            red = Redactor()
            error_text = None
            if rc not in (0, None):
                error_text = clip(red.text((attempt.get("stdout") or "") + "\n"
                                           + (attempt.get("stderr") or "")), 4000)[0]
            timed_out = attempt is None and leg["timed_out"] and index == count - 1
            if timed_out:
                status = "timed_out"                      # killed by the leg timeout
                error_type = "leg_timeout"
            elif attempt is None:
                status = "no_exit_record"                 # killed, or the claude binary failed
                error_type = "no_attempt_log"
            else:
                status = "completed" if rc == 0 else ("failed" if rc is not None else "unknown")
                error_type = "nonzero_exit" if rc not in (0, None) else None
            started = session.first_ts if session else (leg["start"] if count == 1 else None)
            finished = session.last_ts if session else (leg["end"] if count == 1 else None)
            row = {
                "agent_run_id": run_id,
                "job_id": ctx.job_id,
                "ingestion_record_id": record_id,
                "run_key": run_key,
                "stage": leg["stage"],
                "label": leg["label"],
                "attempt_no": attempt_no + 1,
                "claude_session_id": session.session_id if session else None,
                "runner": "claude",
                "executable_version": session.version if session else None,
                "model": session.model if session else None,
                "effort": session.effort if session else None,
                "prompt_sha256": session.prompt_sha if session else None,
                "status": status,
                "exit_code": rc,
                "timeout_seconds": int(ctx.timeout_s) if timed_out else None,
                "started_at": iso(started),
                "finished_at": iso(finished),
                "duration_ms": ms_between(started, finished),
                "error_type": error_type,
                "error_message_redacted": error_text,
                "metadata_redacted": {
                    "stdout_chars": len(attempt.get("stdout") or "") if attempt else None,
                    "stderr_chars": len(attempt.get("stderr") or "") if attempt else None,
                    "leg_seconds": leg["seconds"],
                    "agent_log_match": leg["log_match"],
                    "subagents": sum(1 for sub in ctx.sessions if sub.info.is_subagent
                                     and session is not None
                                     and sub.agent_run_id == session.agent_run_id),
                    "totals_include_subagents": True,
                    "unanswered_tool_calls": session.unanswered if session else None},
                "root_span_id": session.span_id if session else leg["span_id"],
                "session_mapping": session.mapping if session else "none",
                "session_mapping_confidence": session.confidence if session else None,
            }
            if session:
                row.update(_run_totals(ctx, session))
            ctx.rows.add("agent_runs", row)
            if status != "completed":
                ctx.event("agent.attempt.failed", f"attempt:{run_key}:{leg['event_key']}",
                          source_service="launcher", severity="error",
                          occurred_at=finished or leg["end"], stage=leg["stage"],
                          outcome=status if rc is None else f"exit_{rc}",
                          message=error_text, agent_run_id=run_id, span_id=leg["span_id"],
                          record_id=record_id,
                          attributes={"label": leg["label"], "attempt": attempt_no + 1,
                                      "leg_seconds": leg["seconds"]})
        ctx.rows.add("timing_events", {
            "timing_event_id": did("tim", ctx.job_id, leg["event_key"]),
            "job_id": ctx.job_id,
            "agent_run_id": first_run_id if ctx.rows.has("agent_runs", first_run_id) else None,
            "ingestion_record_id": leg["record_id"],
            "event_key": leg["event_key"],
            "category": "agent_leg",
            "stage": leg["label"],
            "started_at": iso(leg["start"]),
            "finished_at": iso(leg["end"]),
            "duration_ms": int(round(leg["seconds"] * 1000)),
            "source_timestamp": iso(leg["end"]),
            "metadata_redacted": {"start_derived_from_finish_minus_duration": True,
                                  "label": leg["label"], "pipeline_stage": leg["stage"]},
            "span_id": leg["span_id"],
        })
        if previous is not None:
            gap_ms = ms_between(previous["end"], leg["start"])
            if gap_ms and gap_ms > 1000:
                gated = any(previous["start"] <= c["ts"] <= previous["end"] + timedelta(seconds=5)
                            for c in awaiting)
                ctx.rows.add("trace_spans", _span(
                    ctx, did("span", ctx.job_id, "gap", previous["span_id"], leg["span_id"]),
                    kind="gap", name="between_legs", service="launcher", stage=leg["stage"],
                    parent=ctx.root_span_id, started=previous["end"], ended=leg["start"],
                    outcome="likely_human_gate" if gated else "queue_or_launcher",
                    attributes={"after": previous["label"], "before": leg["label"],
                                "previous_leg_ended_at_gate": gated},
                    method="timing_jsonl_gap", confidence=0.8 if gated else 0.6))
        previous = leg
    for session in ctx.sessions:
        if session.leg_span_id or session.info.is_subagent or not session.complete \
                or session.record_id is None:
            continue
        interactive = session.interactive
        run_key = f"{'interactive' if interactive else 'untimed'}:{session.session_id}"
        ctx.rows.add("agent_runs", dict({
            "agent_run_id": session.agent_run_id,
            "job_id": ctx.job_id,
            "ingestion_record_id": session.record_id,
            "run_key": run_key,
            "stage": "unknown",
            "label": "interactive" if interactive else "unknown",
            "attempt_no": 1,
            "claude_session_id": session.session_id,
            "runner": "claude",
            "executable_version": session.version,
            "model": session.model,
            "effort": session.effort,
            "prompt_sha256": session.prompt_sha,
            "status": "interactive" if interactive else "no_timing_row",
            "exit_code": None,
            "timeout_seconds": None,
            "started_at": iso(session.first_ts),
            "finished_at": iso(session.last_ts),
            "duration_ms": ms_between(session.first_ts, session.last_ts),
            "error_type": None if interactive else "orphaned_or_launcher_died",
            "error_message_redacted": None,
            "metadata_redacted": {"unanswered_tool_calls": session.unanswered,
                                  "totals_include_subagents": True},
            "root_span_id": session.span_id,
            "session_mapping": session.mapping,
            "session_mapping_confidence": session.confidence,
        }, **_run_totals(ctx, session)))
        if interactive:
            continue                                   # someone asked Claude about the job
        ctx.event("agent.session.orphaned", f"orphan:{session.session_id}",
                  source_service="launcher", severity="warning", occurred_at=session.first_ts,
                  outcome="no_timing_row", agent_run_id=session.agent_run_id,
                  span_id=session.span_id,
                  attributes={"note": "leg with no timing.jsonl line: launcher restarted or "
                                      "killed while it ran, or the job overlapped itself"})


# ===========================================================================
# Claude transcripts
# ===========================================================================

@dataclass
class TranscriptInfo:
    """Cheap facts about one transcript file, cached between runs by (size, mtime)."""
    path: str
    size: int
    mtime_ns: int
    session_id: Optional[str]
    job_id: Optional[str]
    first_ts: Optional[str]
    last_ts: Optional[str]
    is_subagent: bool
    prompt_source: Optional[str]
    permission_mode: Optional[str]
    leg_prompt: bool = False                        # opens like a runner leg prompt


def _first_prompt(record: Dict[str, Any]) -> Optional[str]:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return None


def scan_transcript_header(path: Path, is_subagent: bool) -> TranscriptInfo:
    """Session id, first/last timestamps and the job named by the leg prompt."""
    stat = path.stat()
    session_id: Optional[str] = None
    first_ts: Optional[str] = None
    prompt: Optional[str] = None
    enqueued: Optional[str] = None
    prompt_source = permission_mode = None
    try:
        with open(path, "rb") as handle:
            for _index, raw in zip(range(600), handle):
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                session_id = session_id or record.get("sessionId")
                first_ts = first_ts or record.get("timestamp")
                rtype = record.get("type")
                if rtype == "queue-operation" and record.get("operation") == "enqueue" \
                        and enqueued is None:
                    enqueued = str(record.get("content") or "")
                if rtype == "user" and not record.get("isMeta") \
                        and not record.get("isCompactSummary"):
                    text = _first_prompt(record)
                    if text and text.strip():
                        prompt = text
                        prompt_source = record.get("promptSource")
                        permission_mode = record.get("permissionMode")
                        break
    except OSError:
        pass
    text = prompt or enqueued or ""
    match = PROMPT_JOB_RE.search(text)
    return TranscriptInfo(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                          session_id=session_id, job_id=match.group(1) if match else None,
                          first_ts=first_ts, last_ts=_last_timestamp(path),
                          is_subagent=is_subagent, prompt_source=prompt_source,
                          permission_mode=permission_mode,
                          leg_prompt=bool(LEG_PROMPT_RE.match(text)))


def _last_timestamp(path: Path) -> Optional[str]:
    """Timestamp of the last complete record. Reads backwards in growing windows: a final
    record can be several MB (an image Read)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = 262_144
            while True:
                handle.seek(max(size - window, 0))
                tail = handle.read().split(b"\n")
                if size > window:
                    tail = tail[1:]                     # the first piece is a partial line
                for raw in reversed(tail):
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("timestamp"):
                        return record["timestamp"]
                if window >= size or window >= 64 * 1024 * 1024:
                    return None
                window *= 4
    except OSError:
        return None


def discover_transcripts(settings: Settings, state: Dict[str, Any]) -> List[TranscriptInfo]:
    if state.get("index_version") != INDEX_VERSION:
        state["transcripts"] = {}
        state["mcp_logs"] = {}
        state["index_version"] = INDEX_VERSION
    known: Dict[str, Any] = state.setdefault("transcripts", {})
    found: List[TranscriptInfo] = []
    alive: Set[str] = set()
    for base in settings.transcript_dirs:
        if not base.is_dir():
            continue
        candidates = [(p, False) for p in base.glob("*.jsonl") if ".orphaned-" not in p.name]
        candidates += [(p, True) for p in base.glob("*/subagents/*.jsonl")]
        candidates += [(p, True) for p in base.glob("*/subagents/workflows/*/agent-*.jsonl")]
        for path, is_subagent in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            alive.add(key)
            cached = known.get(key)
            if cached and cached.get("size") == stat.st_size \
                    and cached.get("mtime_ns") == stat.st_mtime_ns:
                try:
                    found.append(TranscriptInfo(**cached))
                    continue
                except TypeError:
                    pass
            try:
                info = scan_transcript_header(path, is_subagent)
            except OSError:
                continue
            known[key] = info.__dict__
            found.append(info)
    for key in list(known):
        if key not in alive:
            known.pop(key, None)                      # removed by Claude's cleanup
    return found


@dataclass
class McpLogInfo:
    path: str
    size: int
    mtime_ns: int
    session_id: Optional[str]
    server: str


def discover_mcp_logs(settings: Settings, state: Dict[str, Any]) -> List[McpLogInfo]:
    known: Dict[str, Any] = state.setdefault("mcp_logs", {})
    found: List[McpLogInfo] = []
    alive: Set[str] = set()
    for base in settings.mcp_log_dirs:
        if not base.is_dir():
            continue
        for path in base.glob("mcp-logs-*/*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            alive.add(key)
            cached = known.get(key)
            if cached and cached.get("size") == stat.st_size \
                    and cached.get("mtime_ns") == stat.st_mtime_ns:
                found.append(McpLogInfo(**cached))
                continue
            session_id = None
            try:
                with open(path, "rb") as handle:
                    for _index, raw in zip(range(20), handle):
                        try:
                            record = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(record, dict) and record.get("sessionId"):
                            session_id = record["sessionId"]
                            break
            except OSError:
                continue
            info = McpLogInfo(path=key, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                              session_id=session_id,
                              server=path.parent.name[len("mcp-logs-"):])
            known[key] = info.__dict__
            found.append(info)
    for key in list(known):
        if key not in alive:
            known.pop(key, None)
    return found


class Session:
    """One Claude session (a leg attempt, or one of its subagents)."""

    def __init__(self, info: TranscriptInfo, ctx: JobContext) -> None:
        self.info = info
        self.ctx = ctx
        self.session_id = info.session_id or did("session", info.path)
        self.span_id = did("span", ctx.job_id, "session", self.session_id, Path(info.path).name)
        self.first_ts = parse_ts(info.first_ts)
        self.last_ts = parse_ts(info.last_ts)
        self.version: Optional[str] = None
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        self.prompt_sha: Optional[str] = None
        self.prompt_text: Optional[str] = None
        self.mapping = "job_id_in_prompt" if not info.is_subagent else "parent_session"
        self.confidence = 1.0
        self.agent_run_id: Optional[str] = None
        self.leg_span_id: Optional[str] = None
        self.leg: Optional[Dict[str, Any]] = None
        self.stage: Optional[str] = None
        self.attempt_no: Optional[int] = None
        self.interactive = False                       # an operator session naming the job
        self.complete = False                          # the leg ended: totals are final
        self.record_id: Optional[str] = None
        self.unanswered = 0
        self.tokens = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0, "thinking_tokens": 0}
        self.inference_count = 0
        self.tool_call_count = 0
        self.tool_error_count = 0
        self.used_higgsfield = False
        self.mcp: Dict[str, Any] = {"deltas": 0, "failed": set(), "needs_auth": set(),
                                    "pending": set(), "last_pending": set(),
                                    "higgsfield_tools": False, "instructions": False,
                                    "log_connected": False, "first_ts": None}

    @property
    def last_activity(self) -> Optional[datetime]:
        """The later of the last record's time and the file's mtime."""
        stamps = [t for t in (self.last_ts, datetime.fromtimestamp(
            self.info.mtime_ns / 1e9, tz=timezone.utc)) if t is not None]
        return max(stamps) if stamps else None

    def totals(self) -> Dict[str, Any]:
        return dict(self.tokens, inference_count=self.inference_count,
                    tool_call_count=self.tool_call_count,
                    tool_error_count=self.tool_error_count)


def _is_leg_session(info: TranscriptInfo) -> bool:
    """A launcher leg: print mode with the launcher's permission flags, or a prompt that
    opens like one of the runner's leg prompts. Anything else that names the job (an
    operator asking Claude about it) is recorded as interactive, never as an attempt."""
    return info.leg_prompt or (info.permission_mode == "bypassPermissions"
                               and info.prompt_source in (None, "sdk"))


def _leg_for_session(ctx: JobContext, session: "Session") -> Optional[Dict[str, Any]]:
    """The finished leg this session ran inside. An attempt starts after its leg started and
    ends before the leg's timing line is written; a chained leg's session starts after the
    previous leg's line, so it never matches that leg while it is still running."""
    first, last = session.first_ts, session.last_ts
    if first is None:
        return None
    candidates = []
    for leg in ctx.legs:
        if first > leg["end"] + timedelta(seconds=0.5):
            continue
        if first < leg["start"] - timedelta(seconds=15):
            continue
        if last is not None and last > leg["end"] + timedelta(seconds=5):
            continue
        candidates.append(leg)
    if not candidates:
        return None
    inside = [leg for leg in candidates if leg["start"] - timedelta(seconds=1) <= first]
    pool = inside or candidates
    return min(pool, key=lambda leg: abs((first - leg["start"]).total_seconds()))


def map_sessions(ctx: JobContext, transcripts: List[TranscriptInfo]) -> None:
    """The job's sessions: main sessions whose leg prompt names the job, plus subagent files
    of those sessions; leg sessions are attached to the leg they ran inside, in attempt
    order. A session is complete (its rows are final) once its leg has a timing line, or
    once it has been quiet long enough that nothing can follow."""
    main = [t for t in transcripts if not t.is_subagent and t.job_id == ctx.job_id]
    main_ids = {t.session_id for t in main if t.session_id}
    subs = [t for t in transcripts if t.is_subagent and t.session_id in main_ids]
    sessions = [Session(info, ctx) for info in sorted(main, key=lambda t: t.first_ts or "")]
    by_id = {s.session_id: s for s in sessions}
    for session in sessions:
        session.agent_run_id = did("run", ctx.job_id, session.session_id)
        if not _is_leg_session(session.info):
            session.interactive = True
            session.mapping = "interactive_prompt_names_job"
            session.confidence = 0.3
            continue
        leg = _leg_for_session(ctx, session)
        if leg is not None:
            leg["sessions"].append(session)
            session.mapping += "+timing_window"
            session.leg_span_id = leg["span_id"]
            session.leg = leg
            session.stage = leg["stage"]
            session.complete = True
        else:
            session.confidence = min(session.confidence, 0.7)
    for leg in ctx.legs:
        leg["sessions"].sort(key=lambda s: s.first_ts or leg["start"])
        for index, session in enumerate(leg["sessions"]):
            session.attempt_no = index
    for session in sessions:
        if session.complete:
            continue
        idle = ctx.exported_at - (session.last_activity or ctx.exported_at)
        if (ctx.terminal and idle >= IDLE_AFTER_TERMINAL) or idle >= STALE_SESSION:
            session.complete = True                    # crashed or abandoned: nothing follows
        else:
            ctx.deferred.append(f"session {session.session_id} still active")
    for info in sorted(subs, key=lambda t: t.path):
        sub = Session(info, ctx)
        parent = by_id.get(info.session_id or "")
        if parent is not None:
            sub.agent_run_id = parent.agent_run_id
            sub.leg_span_id = parent.span_id
            sub.leg = parent.leg
            sub.stage = parent.stage
            sub.complete = parent.complete
            sub.confidence = parent.confidence
            sub.interactive = parent.interactive
        sessions.append(sub)
    ctx.sessions = sessions


# --- tool classification ------------------------------------------------------

_UUIDISH = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{12}$|^[A-Za-z0-9_\-]{12,80}$")
_FAILED_STATUSES = {"failed", "error", "nsfw", "cancelled", "canceled", "rejected"}
_DONE_STATUSES = {"completed", "succeeded", "success", "done"}


def _result_payload(content: Any) -> Tuple[Optional[Any], str]:
    """(parsed JSON if the result text is JSON, the plain text) of tool_result content."""
    texts: List[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
    text = "\n".join(texts)
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped), text
        except ValueError:
            first = texts[0].strip() if texts else ""
            if first[:1] in ("{", "[") and first != stripped:
                try:
                    return json.loads(first), text
                except ValueError:
                    pass
    return None, text


def _ids_from(items: Any, *keys: str) -> List[str]:
    out: List[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                for key in keys:
                    value = item.get(key)
                    if isinstance(value, str) and _UUIDISH.match(value):
                        out.append(value)
                        break
    return out


def _dedupe(values: List[str]) -> List[str]:
    seen: Set[str] = set()
    return [v for v in values if not (v in seen or seen.add(v))]


def _params_key(params: Any) -> Optional[str]:
    if not isinstance(params, dict):
        return None
    stripped = {k: v for k, v in params.items() if k not in ("get_cost",)}
    return sha256_hex(json.dumps(stripped, sort_keys=True, default=str))[:24]


@dataclass
class ToolOutcome:
    outcome: str
    attrs: Dict[str, Any]
    job_ids: List[str]
    relation: Optional[str]                            # creates | polls | None
    credits: Optional[float] = None                    # a cost preflight quote
    # Each provider job's own status in this call (a wave poll covers several jobs).
    per_job: Optional[Dict[str, str]] = None


def classify_tool(name: str, tool_input: Any, payload: Any, text: str, is_error: bool,
                  bash: Optional[Dict[str, Any]] = None) -> ToolOutcome:
    """Transport status is is_error; this is the semantic outcome of the call."""
    attrs: Dict[str, Any] = {}
    low = (text or "").lower()
    inp = tool_input if isinstance(tool_input, dict) else {}
    if not name.startswith(HIGGSFIELD_PREFIX):
        if name == "Bash" and bash:
            if bash.get("timedOutAfterMs"):
                attrs["timed_out_after_ms"] = bash.get("timedOutAfterMs")
                return ToolOutcome("timeout", attrs, [], None)
            if bash.get("interrupted"):
                return ToolOutcome("interrupted", attrs, [], None)
        if is_error:
            code = re.search(r"Exit code (\d+)", text or "")
            if code:
                attrs["exit_code"] = int(code.group(1))
            return ToolOutcome("error", attrs, [], None)
        return ToolOutcome("ok", attrs, [], None)

    tool = name[len(HIGGSFIELD_PREFIX):]
    params = inp.get("params")
    result = payload if isinstance(payload, dict) else {}
    if tool == "jobs_wait":
        polled = _ids_from(inp.get("jobs"), "job_id", "jobId")
        returned = _ids_from(result.get("jobs"), "job_id", "jobId")
        ids = _dedupe(polled + returned)
        if not isinstance(inp.get("jobs"), list):
            attrs["malformed_input"] = True
        if is_error:
            malformed = attrs.get("malformed_input") or "invalid" in low
            return ToolOutcome("malformed_arguments" if malformed else "error", attrs, ids,
                               "polls")
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        for key in ("total", "completed", "failed", "active", "errors"):
            if as_int(summary.get(key)) is not None:
                attrs[key] = as_int(summary.get(key))
        statuses: Dict[str, int] = {}
        per_job: Dict[str, str] = {}
        for job in result.get("jobs") or []:
            if isinstance(job, dict) and isinstance(job.get("status"), str):
                statuses[job["status"]] = statuses.get(job["status"], 0) + 1
                job_id = job.get("job_id") or job.get("jobId") or job.get("id")
                if isinstance(job_id, str) and _UUIDISH.match(job_id):
                    per_job[job_id] = job["status"].lower()
        attrs.update(timed_out=bool(result.get("timed_out")),
                     all_terminal=bool(result.get("all_terminal")), job_statuses=statuses)
        if as_float(inp.get("timeout_seconds")) is not None:
            attrs["wait_budget_seconds"] = as_float(inp.get("timeout_seconds"))
        if result.get("all_terminal"):
            bad = (attrs.get("failed") or 0) + (attrs.get("errors") or 0) + sum(
                n for s, n in statuses.items() if s in _FAILED_STATUSES)
            return ToolOutcome("terminal_with_failures" if bad else "completed", attrs, ids,
                               "polls", per_job=per_job)
        return ToolOutcome("timed_out_nonterminal" if result.get("timed_out") else "nonterminal",
                           attrs, ids, "polls", per_job=per_job)
    if tool == "job_status":
        generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
        ids = _dedupe([v for v in (inp.get("jobId"), generation.get("id"))
                       if isinstance(v, str) and _UUIDISH.match(v)])
        if is_error:
            return ToolOutcome("error", attrs, ids, "polls")
        status = str(generation.get("status") or "").lower()
        attrs["status"] = status or None
        if isinstance(generation.get("params"), dict):
            gparams = generation["params"]
            attrs["input_media_types"] = [m.get("data", {}).get("type") for m in
                                          gparams.get("medias") or [] if isinstance(m, dict)
                                          and isinstance(m.get("data"), dict)]
            attrs["reference_elements"] = len(gparams.get("reference_elements") or [])
        if generation.get("results") or status in _DONE_STATUSES:
            outcome = "completed"
        elif status in _FAILED_STATUSES:
            outcome = "failed"
        else:
            outcome = "nonterminal"
        per_job = {job_id: ("completed" if outcome == "completed" else status or outcome)
                   for job_id in ids}
        return ToolOutcome(outcome, attrs, ids, "polls", per_job=per_job)
    if tool.endswith("_batch"):
        ids = _ids_from(result.get("jobs"), "job_id", "id")
        if is_error:
            return ToolOutcome("error", attrs, ids, "creates")
        submitted = as_int(result.get("submitted_count"))
        failed = as_int(result.get("failed_count")) or 0
        attrs.update(submitted_count=submitted, failed_count=failed,
                     requests=len(inp.get("requests") or []) if isinstance(
                         inp.get("requests"), list) else None)
        if ids or submitted:
            return ToolOutcome("partially_submitted" if failed else "submitted", attrs, ids,
                               "creates")
        return ToolOutcome("no_job_returned", attrs, ids, None)
    creating = tool.startswith("generate_") or tool in (
        "upscale_image", "upscale_video", "outpaint_image", "reframe", "remove_background",
        "motion_control", "voice_change", "dubbing", "execute_preset", "create_voice")
    if creating:
        if isinstance(params, str):
            attrs["params_sent_as_string"] = True
        if is_error:
            if attrs.get("params_sent_as_string") or any(
                    w in low[:2000] for w in ("invalid", "validation", "required", "must be")):
                return ToolOutcome("malformed_arguments", attrs, [], None)
            if any(w in low[:2000] for w in ("insufficient", "not enough credit")):
                return ToolOutcome("rejected_credits", attrs, [], None)
            if "unauthorized" in low[:2000]:
                return ToolOutcome("unauthorized", attrs, [], None)
            return ToolOutcome("error", attrs, [], None)
        results = result.get("results")
        ids = _ids_from(results, "id", "job_id")
        if isinstance(params, dict):
            attrs["model"] = params.get("model")
            if params.get("declined_preset_id"):
                attrs["declined_preset"] = True
        if isinstance(result.get("adjustments"), dict) and result["adjustments"]:
            attrs["parameters_adjusted"] = sorted(map(str, result["adjustments"]))
        if ids:
            attrs["submitted_jobs"] = len(ids)
            return ToolOutcome("submitted", attrs, ids, "creates")
        cost = result.get("cost") if isinstance(result.get("cost"), dict) else None
        if cost is not None:
            credits = as_float(cost.get("credits_exact"))
            credits = credits if credits is not None else as_float(cost.get("credits"))
            attrs["quoted_credits"] = credits
            return ToolOutcome("cost_preflight", attrs, [], None, credits)
        notice = result.get("notice") if isinstance(result.get("notice"), dict) else {}
        if notice.get("type") == "preset_recommendation":
            data = notice.get("data") if isinstance(notice.get("data"), dict) else {}
            preset = data.get("preset") if isinstance(data.get("preset"), dict) else {}
            attrs["recommended_preset"] = preset.get("name") or preset.get("id")
            return ToolOutcome("recommendation_only", attrs, [], None)
        return ToolOutcome("no_job_returned", attrs, [], None)
    if is_error:
        return ToolOutcome("error", attrs, [], None)
    if tool == "media_upload":
        uploads = result.get("uploads") if isinstance(result.get("uploads"), list) else []
        attrs["uploads"] = len(uploads)
        return ToolOutcome("media_issued", attrs, [], None)
    if tool == "media_confirm":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        statuses = [r.get("status") for r in results if isinstance(r, dict)]
        attrs["confirmed"] = sum(1 for s in statuses if s == "uploaded")
        attrs["requested"] = len(statuses)
        ok = bool(statuses) and all(s == "uploaded" for s in statuses)
        return ToolOutcome("media_confirmed" if ok else "media_not_confirmed", attrs, [], None)
    if tool == "balance":
        attrs["credits"] = as_float(result.get("credits"))
        attrs["plan"] = result.get("subscription_plan_type")
    return ToolOutcome("read", attrs, [], None)


# --- block construction -------------------------------------------------------

def _tool_input_view(name: str, tool_input: Any, red: Redactor) -> Tuple[Any, bool, Optional[str]]:
    """Sanitized tool arguments: (json, truncated, omitted reason)."""
    if not isinstance(tool_input, dict):
        value, cut = clip_json(red.json(tool_input), LIMIT_TOOL_ARGS)
        return value, cut, None
    if name == "Bash" and isinstance(tool_input.get("command"), str):
        # cat > file <<'EOF' ... EOF writes a file: keep the command, not the file body.
        command, bodies = strip_heredocs(tool_input["command"])
        if bodies:
            view = dict(tool_input, command=command, heredoc_bodies_omitted=bodies)
            value, cut = clip_json(red.json(view), LIMIT_TOOL_ARGS)
            return value, cut, "heredoc_body"
    if name in ("Write", "NotebookEdit"):
        content = str(tool_input.get("content") or tool_input.get("new_source") or "")
        view = {k: v for k, v in tool_input.items() if k not in ("content", "new_source")}
        view.update(content_chars=len(content), content_sha256=sha256_hex(content))
        return red.json(view), False, "file_content"
    if name in ("Edit", "MultiEdit"):
        view = {k: v for k, v in tool_input.items()
                if k not in ("old_string", "new_string", "edits")}
        edits = tool_input.get("edits") if isinstance(tool_input.get("edits"), list) else []
        view.update(edit_count=len(edits) or 1,
                    old_chars=len(str(tool_input.get("old_string") or "")),
                    new_chars=len(str(tool_input.get("new_string") or "")))
        return red.json(view), False, "edit_content"
    value, cut = clip_json(red.json(tool_input), LIMIT_TOOL_ARGS)
    return value, cut, None


def _result_view(name: str, content: Any, payload: Any, text: str, is_error: bool,
                 outcome: str, red: Redactor
                 ) -> Tuple[Optional[str], Any, bool, Optional[str], Dict[str, Any]]:
    """(text, json, truncated, omitted reason, metadata) for one tool result."""
    meta: Dict[str, Any] = {"result_chars": len(text)}
    images = 0
    if isinstance(content, list):
        images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
    if images:
        meta["image_blocks"] = images
    if "<persisted-output>" in text or "exceeds maximum allowed tokens" in text:
        meta["output_offloaded"] = True
    failed = is_error or outcome in ("timeout", "interrupted", "error")
    as_hash = (name == "Read" or (name in OUTPUT_AS_HASH_TOOLS and not failed)
               or (name.startswith("mcp__") and not name.startswith(HIGGSFIELD_PREFIX)
                   and not failed))
    if as_hash:
        # File text (Read, cat/sed/jq through Bash, Grep matches) or another server's output:
        # size and hash only. Failures keep a clipped, redacted excerpt below.
        meta.update(result_sha256=sha256_hex(text),
                    lines=text.count("\n") + (1 if text else 0))
        reason = "file_content" if name in ("Read", "Grep", "NotebookRead") else "command_output"
        return None, None, False, reason, meta
    if name in OUTPUT_AS_HASH_TOOLS or (name.startswith("mcp__")
                                        and not name.startswith(HIGGSFIELD_PREFIX)):
        limit = LIMIT_COMMAND_ERROR                    # only failures get here
    elif name.startswith(HIGGSFIELD_PREFIX):
        limit = LIMIT_MCP_RESULT
    else:
        limit = LIMIT_TOOL_RESULT
    if payload is not None:
        value, cut = clip_json(red.json(payload), limit * 2)
        return None, value, cut, None, meta
    clean, cut = clip(red.text(text), limit)
    return clean, None, cut, None, meta


class _Inference:
    def __init__(self, first: int, started: Optional[datetime], record: Dict[str, Any]) -> None:
        message = record.get("message") or {}
        self.first = first
        self.last = first
        self.started = started
        self.finished = parse_ts(record.get("timestamp"))
        self.model = message.get("model")
        self.request_id = record.get("requestId")
        self.effort = record.get("effort")
        self.usage: Dict[str, Any] = {}
        self.stop_reason: Optional[str] = None
        self.blocks = 0
        self.tools = 0


def build_session_rows(ctx: JobContext, session: Session) -> None:
    path = Path(session.info.path)
    job = ctx.job_id
    sidechain = session.info.is_subagent
    source = ctx.source("claude_transcript", f"claude/{path.parent.name}/{path.name}",
                        {"session_id": session.session_id, "subagent": sidechain})
    record_key = f"session:{session.session_id}:{path.name}:size:{session.info.size}"
    record_id = did("rec", source.source_id, safe_relpath(record_key))
    session.record_id = record_id
    pending: Dict[str, Dict[str, Any]] = {}
    inferences: Dict[str, _Inference] = {}
    quotes: Dict[str, float] = {}
    last_non_assistant: Optional[datetime] = None
    prompt_seen = False
    seen_uuids: Set[str] = set()
    records = 0
    for number, _raw, record in iter_jsonl(path):     # streamed: transcripts can be large
        records += 1
        uid = record.get("uuid")
        if isinstance(uid, str):
            if uid in seen_uuids:
                continue                               # resume/compaction copies
            seen_uuids.add(uid)
        rtype = record.get("type")
        ts = parse_ts(record.get("timestamp"))
        if ts is not None and (session.last_ts is None or ts > session.last_ts):
            session.last_ts = ts
        session.version = session.version or record.get("version")
        if rtype == "system":
            _system_event(ctx, session, record, number, ts)
            last_non_assistant = ts or last_non_assistant
            continue
        if rtype == "attachment":
            _attachment_event(ctx, session, record, number, ts)
            continue
        if rtype == "summary":
            continue
        if rtype not in ("user", "assistant"):
            continue                                   # titles, queue, file history, ...
        message = record.get("message") if isinstance(record.get("message"), dict) else None
        if message is None:
            continue
        if record.get("isCompactSummary"):
            continue                                   # model summary of earlier context
        if rtype == "assistant" and (message.get("model") == "<synthetic>"
                                     or record.get("isApiErrorMessage")):
            ctx.event("claude.api.error", f"synthetic:{session.session_id}:{uid or number}",
                      source_service="claude", severity="error", occurred_at=ts,
                      outcome=str(record.get("error") or record.get("apiErrorStatus") or "error"),
                      message=_text_of(message.get("content"))[:1000],
                      attributes={"api_error_status": record.get("apiErrorStatus")},
                      agent_run_id=session.agent_run_id, span_id=session.span_id)
            continue
        if record.get("toolDenialKind") or (isinstance(record.get("permissionDecision"), dict)
                                            and record["permissionDecision"].get("decision")
                                            == "deny"):
            ctx.event("claude.tool.denied", f"denied:{session.session_id}:{uid or number}",
                      source_service="claude", severity="warning", occurred_at=ts,
                      outcome=str(record.get("toolDenialKind") or "denied"),
                      agent_run_id=session.agent_run_id, span_id=session.span_id)
        role = message.get("role") or rtype
        content = message.get("content")
        blocks: List[Any] = content if isinstance(content, list) else \
            [{"type": "text", "text": content}] if isinstance(content, str) else []
        message_id = message.get("id") if rtype == "assistant" else None
        if rtype == "assistant":
            session.model = session.model or message.get("model")
            session.effort = session.effort or record.get("effort")
            if message_id:
                inf = inferences.get(message_id)
                if inf is None:
                    inf = inferences[message_id] = _Inference(number, last_non_assistant, record)
                inf.last = number
                inf.finished = ts or inf.finished
                if isinstance(message.get("usage"), dict):
                    inf.usage = message["usage"]       # the last record's usage is the total
                inf.stop_reason = message.get("stop_reason") or inf.stop_reason
        else:
            last_non_assistant = ts or last_non_assistant
        is_meta = bool(record.get("isMeta"))
        results_here = sum(1 for b in blocks if isinstance(b, dict)
                           and b.get("type") == "tool_result")
        structured = None
        mcp_meta = record.get("mcpMeta")
        if isinstance(mcp_meta, dict) and isinstance(mcp_meta.get("structuredContent"),
                                                     (dict, list)):
            structured = mcp_meta["structuredContent"]
        tool_use_result = record.get("toolUseResult") if results_here == 1 else None
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "unknown")
            red = Redactor()
            row: Dict[str, Any] = {
                "llm_block_id": did("blk", session.session_id, uid or f"line-{number}", index),
                "job_id": job,
                "llm_inference_id": did("inf", session.session_id, message_id) if message_id else None,
                "agent_run_id": session.agent_run_id,
                "span_id": None,
                "claude_session_id": session.session_id,
                "source_uuid": str(uid or f"line-{number}"),
                "parent_source_uuid": record.get("parentUuid"),
                "message_id": message_id,
                "sequence_no": number,
                "block_index": index,
                "role": role,
                "block_type": btype,
                "tool_use_id": None,
                "tool_name": None,
                "is_error": None,
                "is_sidechain": bool(record.get("isSidechain")) or sidechain,
                "content_text_redacted": None,
                "content_json_redacted": None,
                "content_sha256": None,
                "content_bytes": None,
                "content_truncated": False,
                "content_omitted_reason": None,
                "occurred_at": iso(ts),
                "metadata_redacted": {},
            }
            if btype == "text":
                text = str(block.get("text") or "")
                is_prompt = rtype == "user" and not is_meta and not prompt_seen
                clean, cut = clip(red.text(text), LIMIT_PROMPT if is_prompt else LIMIT_TEXT)
                row.update(content_text_redacted=clean, content_truncated=cut,
                           content_sha256=sha256_hex(text),
                           content_bytes=len(text.encode("utf-8")))
                if is_prompt:
                    prompt_seen = True
                    session.prompt_sha = sha256_hex(text)
                    session.prompt_text = text
                    row["metadata_redacted"] = {"leg_prompt": True}
                elif is_meta:
                    row["metadata_redacted"] = {"meta": True}
            elif btype in ("thinking", "redacted_thinking"):
                body = str(block.get("thinking") or block.get("data") or "")
                row.update(content_omitted_reason="hidden_reasoning",
                           content_sha256=sha256_hex(body) if body else None,
                           content_bytes=len(body.encode("utf-8")))
            elif btype == "tool_use":
                name = str(block.get("name") or "unknown")
                tool_use_id = str(block.get("id") or "")
                view, cut, omitted = _tool_input_view(name, block.get("input"), red)
                row.update(tool_use_id=tool_use_id or None, tool_name=name,
                           content_json_redacted=view if isinstance(view, (dict, list))
                           else {"value": view},
                           content_truncated=cut, content_omitted_reason=omitted,
                           content_sha256=sha256_hex(block.get("input")),
                           content_bytes=len(json.dumps(block.get("input"), default=str)))
                session.tool_call_count += 1
                session.used_higgsfield = session.used_higgsfield or \
                    name.startswith(HIGGSFIELD_PREFIX)
                if message_id and message_id in inferences:
                    inferences[message_id].tools += 1
                if tool_use_id:
                    pending[tool_use_id] = {"name": name, "input": block.get("input"),
                                            "started": ts, "block_id": row["llm_block_id"]}
            elif btype == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                call = pending.pop(tool_use_id, None)
                name = call["name"] if call else "unknown"
                is_error = bool(block.get("is_error"))
                parsed, text = _result_payload(block.get("content"))
                if not name.startswith("mcp__"):
                    parsed = None                      # JSON printed by Bash/Grep is file text
                payload = structured if (structured is not None and name.startswith("mcp__")) \
                    else parsed
                bash = tool_use_result if (name == "Bash" and isinstance(tool_use_result, dict)) \
                    else None
                result = classify_tool(name, call["input"] if call else None, payload, text,
                                       is_error, bash)
                text_view, json_view, cut, omitted, meta = _result_view(
                    name, block.get("content"), payload, text, is_error, result.outcome, red)
                if bash:
                    meta.update(interrupted=bool(bash.get("interrupted")),
                                timed_out_after_ms=bash.get("timedOutAfterMs"),
                                return_code=bash.get("returnCodeInterpretation"))
                row.update(tool_use_id=tool_use_id or None, tool_name=name, is_error=is_error,
                           content_text_redacted=text_view,
                           content_json_redacted=(json_view if isinstance(json_view, (dict, list))
                                                  or json_view is None else {"value": json_view}),
                           content_truncated=cut, content_omitted_reason=omitted,
                           content_sha256=sha256_hex(block.get("content")),
                           content_bytes=len(json.dumps(block.get("content"), default=str)),
                           metadata_redacted=dict(meta, outcome=result.outcome))
                if is_error:
                    session.tool_error_count += 1
                if call:
                    _tool_span(ctx, session, call, tool_use_id, ts, row["llm_block_id"],
                               is_error, text, result, quotes)
            elif btype in ("image", "document"):
                src = block.get("source") if isinstance(block.get("source"), dict) else {}
                data = str(src.get("data") or "")
                row.update(content_omitted_reason=btype,
                           content_sha256=sha256_hex(data) if data else None,
                           content_bytes=len(data) * 3 // 4 if data else None,
                           metadata_redacted={"media_type": src.get("media_type"),
                                              "source_type": src.get("type"),
                                              "title": block.get("title")})
            else:
                value, cut = clip_json(red.json(block), LIMIT_TOOL_ARGS)
                row.update(content_json_redacted=value if isinstance(value, (dict, list))
                           else {"value": value}, content_truncated=cut)
            if red.hits:
                row["metadata_redacted"] = dict(row["metadata_redacted"], redactions=red.hits)
            ctx.rows.add("llm_blocks", row)
            if message_id and message_id in inferences:
                inferences[message_id].blocks += 1
    if not records:
        session.record_id = None
        return
    ctx.record(source, record_key,
               sha256_hex(f"{path}:{session.info.size}:{session.info.mtime_ns}"),
               session.last_ts, {"records": records})
    session.unanswered = len(pending)
    if session.complete:
        for message_id, inf in inferences.items():
            _inference_rows(ctx, session, message_id, inf)
        for tool_use_id, call in pending.items():
            ctx.event("claude.tool.unanswered", f"unanswered:{session.session_id}:{tool_use_id}",
                      source_service="claude", severity="warning", occurred_at=call["started"],
                      outcome="no_result",
                      message=f"{call['name']} never returned (aborted, or the leg was killed)",
                      agent_run_id=session.agent_run_id, span_id=session.span_id,
                      attributes={"tool_name": call["name"]})
        ctx.rows.add("trace_spans", _span(
            ctx, session.span_id, kind="claude_session", name="claude_session",
            service="claude", parent=session.leg_span_id or ctx.root_span_id,
            started=session.first_ts, ended=session.last_ts, session=session,
            record_id=record_id,
            attributes=dict(session.totals(), subagent=sidechain, version=session.version,
                            model=session.model, effort=session.effort,
                            prompt_source=session.info.prompt_source,
                            permission_mode=session.info.permission_mode,
                            unanswered_tool_calls=session.unanswered),
            method=session.mapping, confidence=session.confidence,
            attempt_no=session.attempt_no))


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _inference_rows(ctx: JobContext, session: Session, message_id: str, inf: _Inference) -> None:
    usage = inf.usage or {}
    details = usage.get("output_tokens_details") if isinstance(
        usage.get("output_tokens_details"), dict) else {}
    cache_detail = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) \
        else {}
    cache_write = as_int(usage.get("cache_creation_input_tokens"))
    nested = (as_int(cache_detail.get("ephemeral_5m_input_tokens")) or 0) + \
        (as_int(cache_detail.get("ephemeral_1h_input_tokens")) or 0)
    if nested and (cache_write is None or nested > cache_write):
        cache_write = nested
    tokens = {
        "input_tokens": as_int(usage.get("input_tokens")),
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": as_int(usage.get("cache_read_input_tokens")),
        "output_tokens": as_int(usage.get("output_tokens")),
        "thinking_tokens": as_int(details.get("thinking_tokens")),   # included in output
    }
    for key, value in tokens.items():
        if value:
            session.tokens[key] += value
    session.inference_count += 1
    span_id = did("span", ctx.job_id, "inference", session.session_id, message_id)
    ctx.rows.add("llm_inferences", dict({
        "llm_inference_id": did("inf", session.session_id, message_id),
        "job_id": ctx.job_id,
        "agent_run_id": session.agent_run_id,
        "span_id": span_id,
        "claude_session_id": session.session_id,
        "model_message_id": message_id,
        "request_id": inf.request_id,
        "first_sequence_no": inf.first,
        "last_sequence_no": inf.last,
        "model": inf.model,
        "service_tier": usage.get("service_tier"),
        "stop_reason": inf.stop_reason,
        "block_count": inf.blocks,
        "tool_use_count": inf.tools,
        "started_at": iso(inf.started),
        "finished_at": iso(inf.finished),
        "duration_ms": ms_between(inf.started, inf.finished),
        "metadata_redacted": {"effort": inf.effort,
                              "thinking_tokens_included_in_output": True},
    }, **tokens))
    ctx.rows.add("trace_spans", _span(
        ctx, span_id, kind="llm_inference", name=str(inf.model or "model"), service="claude",
        parent=session.span_id, started=inf.started, ended=inf.finished, outcome=inf.stop_reason,
        session=session, attributes={"output_tokens": tokens["output_tokens"],
                                     "tool_use_count": inf.tools},
        method="transcript_message_id"))


def _system_event(ctx: JobContext, session: Session, record: Dict[str, Any], number: int,
                  ts: Optional[datetime]) -> None:
    subtype = str(record.get("subtype") or "system")
    key = f"system:{session.session_id}:{record.get('uuid') or number}"
    if subtype == "api_error":
        error = record.get("error") if isinstance(record.get("error"), dict) else {}
        ctx.event("claude.api.retry", key, source_service="claude", severity="warning",
                  occurred_at=ts, outcome=str(error.get("status") or "error"),
                  message=str(error.get("message") or "")[:1000],
                  attributes={"status": error.get("status"),
                              "retry_attempt": record.get("retryAttempt"),
                              "max_retries": record.get("maxRetries"),
                              "retry_in_ms": record.get("retryInMs"),
                              "source": record.get("source"),
                              "network_down": error.get("isNetworkDown")},
                  agent_run_id=session.agent_run_id, span_id=session.span_id)
    elif subtype == "compact_boundary":
        meta = record.get("compactMetadata") if isinstance(record.get("compactMetadata"), dict) \
            else {}
        ctx.event("claude.context.compacted", key, source_service="claude", severity="warning",
                  occurred_at=ts, outcome=str(meta.get("trigger") or "compacted"),
                  attributes={k: meta.get(k) for k in ("trigger", "preTokens", "postTokens",
                                                       "durationMs", "cumulativeDroppedTokens")},
                  agent_run_id=session.agent_run_id, span_id=session.span_id)


HIGGSFIELD_SERVER = "claude.ai Higgsfield"


def _attachment_event(ctx: JobContext, session: Session, record: Dict[str, Any], number: int,
                      ts: Optional[datetime]) -> None:
    """Attachments are an ALLOWLIST: most carry the user's e-mail, org ids, the system
    prompt or file contents. Only MCP connector availability is kept, as server and tool
    names, and summarised per session (a connector is 'pending' for seconds at every start)."""
    attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
    mcp = session.mcp
    if attachment.get("type") == "mcp_instructions_delta":
        # Server names only (the instruction text itself is never read).
        if HIGGSFIELD_SERVER in [n for n in attachment.get("addedNames") or []
                                 if isinstance(n, str)]:
            mcp["instructions"] = True
        return
    if attachment.get("type") != "deferred_tools_delta":
        return
    mcp["deltas"] += 1
    for key, target in (("failedMcpServers", "failed"), ("needsAuthMcpServers", "needs_auth"),
                        ("pendingMcpServers", "pending")):
        for name in attachment.get(key) or []:
            if isinstance(name, str):
                mcp[target].add(name)
    mcp["last_pending"] = {n for n in attachment.get("pendingMcpServers") or []
                           if isinstance(n, str)}
    for name in attachment.get("addedNames") or []:
        if isinstance(name, str) and name.startswith(HIGGSFIELD_PREFIX):
            mcp["higgsfield_tools"] = True
    if mcp["first_ts"] is None:
        mcp["first_ts"] = ts


def _mcp_summary(ctx: JobContext, session: Session) -> None:
    """One event per finished session: the Higgsfield connector's status. A connector is
    'pending' for seconds at every start; it is connected when it left pending without
    failing, when its tools or instructions were added, when its MCP log says so, or when
    a Higgsfield tool was called. Only failed / needs-auth (or still pending when the
    session ended) is an error."""
    mcp = session.mcp
    if not mcp["deltas"] and not mcp["log_connected"] and not session.used_higgsfield:
        return
    failed = HIGGSFIELD_SERVER in mcp["failed"]
    needs_auth = HIGGSFIELD_SERVER in mcp["needs_auth"]
    still_pending = HIGGSFIELD_SERVER in mcp["last_pending"]
    cleared = HIGGSFIELD_SERVER in mcp["pending"] and not still_pending
    connected = (session.used_higgsfield or mcp["higgsfield_tools"] or mcp["instructions"]
                 or mcp["log_connected"] or (cleared and not failed and not needs_auth))
    if connected:
        status, severity = "connected", "info"
    elif failed or needs_auth:
        status, severity = ("failed" if failed else "needs_auth"), "error"
    elif still_pending:
        status, severity = "still_pending_at_end", "error"
    else:
        status, severity = "not_listed", "warning"
    attrs = {"higgsfield": status, "tools_listed": mcp["higgsfield_tools"],
             "instructions_added": mcp["instructions"], "log_connected": mcp["log_connected"],
             "used": session.used_higgsfield, "failed": sorted(mcp["failed"]),
             "needs_auth": sorted(mcp["needs_auth"]), "seen_pending": sorted(mcp["pending"])}
    ctx.event("claude.mcp.status", f"mcpstatus:{session.session_id}:{Path(session.info.path).name}",
              source_service="claude", severity=severity,
              occurred_at=mcp["first_ts"], outcome=f"higgsfield_{status}", attributes=attrs,
              agent_run_id=session.agent_run_id, span_id=session.span_id)


def _tool_span(ctx: JobContext, session: Session, call: Dict[str, Any], tool_use_id: str,
               ts: Optional[datetime], result_block_id: str, is_error: bool, text: str,
               result: ToolOutcome, quotes: Dict[str, float]) -> None:
    name = call["name"]
    span_id = did("span", ctx.job_id, "tool", session.session_id, tool_use_id)
    hashes = [hash_id(j) for j in result.job_ids]
    operation = did("op", ctx.job_id, *sorted(hashes)) if hashes else None
    attrs = dict(result.attrs)
    if hashes:
        attrs["provider_job_count"] = len(hashes)
        attrs["provider_job_hashes"] = hashes[:50]
    error = clip(Redactor().text(text), 2000)[0] if is_error else None
    tool_input = call["input"] if isinstance(call["input"], dict) else {}
    scene = scene_of(json.dumps(tool_input, default=str)[:4000]) if tool_input else None
    transport = "error" if is_error else ("timeout" if result.outcome == "timeout" else "success")
    service = "higgsfield_mcp" if name.startswith(HIGGSFIELD_PREFIX) else (
        "mcp" if name.startswith("mcp__") else "claude")
    ctx.rows.add("trace_spans", _span(
        ctx, span_id, kind="tool_call", name=name, service=service, parent=session.span_id,
        started=call["started"], ended=ts, transport=transport, outcome=result.outcome,
        session=session, tool_use_id=tool_use_id,
        provider_hash=hashes[0] if len(hashes) == 1 else None,
        request_block=call["block_id"], response_block=result_block_id, error=error,
        operation_id=operation, scene_id=scene, attributes=attrs, method="exact_tool_use_id"))
    if not name.startswith(HIGGSFIELD_PREFIX):
        return
    # Quoted credits per submission: the bridge skill runs a get_cost preflight with the same
    # params right before each generation. Pair them so rejected takes are counted too.
    params = tool_input.get("params")
    if result.outcome == "cost_preflight" and result.credits is not None:
        key = _params_key(params)
        if key:
            quotes[key] = result.credits
    if result.outcome in ("submitted", "partially_submitted") and hashes:
        requests = tool_input.get("requests") if isinstance(tool_input.get("requests"), list) \
            else None
        quoted: Optional[float] = None
        if requests is None:
            quoted = quotes.get(_params_key(params) or "")
        else:
            found = [quotes.get(_params_key(r.get("params")) or "") for r in requests
                     if isinstance(r, dict)]
            if found and all(v is not None for v in found):
                quoted = float(sum(v for v in found if v is not None))
        domain = f"higgsfield:quote:{hashes[0]}"
        if quoted is not None and not session.complete:
            pass                       # sent with the leg's run (its FK target) when it ends
        elif quoted is not None:
            ctx.rows.add("cost_observations", {
                "cost_observation_id": did("cost", ctx.job_id, domain),
                "job_id": ctx.job_id,
                "agent_run_id": session.agent_run_id,
                "ingestion_record_id": session.record_id,
                "domain_key": domain,
                "provider": "higgsfield",
                "usage_kind": "quoted_submission_credits",
                "unit": "credits",
                "amount": max(quoted, 0.0),
                "cost_usd": None,
                "basis": "item",
                "estimated": True,
                "observed_at": iso(ts),
                "metadata_redacted": {"tool": name, "jobs": len(hashes), "scene_id": scene,
                                      "model": params.get("model") if isinstance(params, dict)
                                      else None,
                                      "note": "get_cost preflight quote, not a billed amount"},
                "span_id": span_id,
            })
        else:
            attrs_note = {"tool": name, "jobs": len(hashes)}
            ctx.event("cost.quote_missing", f"quote_missing:{span_id}", source_service="claude",
                      severity="info", occurred_at=ts, outcome="no_preflight_seen",
                      attributes=attrs_note, agent_run_id=session.agent_run_id, span_id=span_id)
    if result.relation is None:
        return
    per_job = result.per_job or {}
    for job_id, job_hash in zip(result.job_ids, hashes):
        provider_span = did("span", ctx.job_id, "provider_job", job_hash)
        info = ctx.provider_jobs.setdefault(job_hash, {
            "first": call["started"], "last": ts, "terminal": None, "final": None,
            "polls": 0, "creates": 0, "tool": name, "scene": scene, "session": session,
            "outcomes": {}})
        info["last"] = max(filter(None, (info["last"], ts)), default=ts)
        info["polls" if result.relation == "polls" else "creates"] += 1
        # This job's own status in the call; a call's aggregate outcome only speaks for a
        # job when the call covered that job alone.
        status = per_job.get(job_id)
        if status is None and len(hashes) == 1:
            status = {"completed": "completed", "failed": "failed",
                      "terminal_with_failures": "failed"}.get(result.outcome)
        job_outcome = ("completed" if status in _DONE_STATUSES else
                       "failed" if status in _FAILED_STATUSES else
                       result.outcome if result.relation == "creates" else
                       (status or "status_not_reported"))
        info["outcomes"][job_outcome] = info["outcomes"].get(job_outcome, 0) + 1
        if result.relation == "polls" and (status in _DONE_STATUSES or status in _FAILED_STATUSES):
            info["terminal"] = info["terminal"] or ts
            info["final"] = "completed" if status in _DONE_STATUSES else "failed"
        if result.relation == "creates":
            info["tool"] = name
            info["scene"] = info["scene"] or scene
            info["first"] = min(filter(None, (info["first"], call["started"])),
                                default=call["started"])
        ctx.rows.add("span_links", {
            "span_link_id": did("link", span_id, result.relation, provider_span),
            "job_id": ctx.job_id,
            "from_span_id": span_id,
            "to_span_id": provider_span,
            "to_artifact_id": None,
            "relation": result.relation,
            "metadata_redacted": {"tool_outcome": result.outcome, "job_outcome": job_outcome,
                                  "job_status": enum_value(status), "provider_job_id_hash": job_hash},
            "correlation_method": "provider_job_id_in_tool_io",
            "correlation_confidence": 1.0,
        })


def build_provider_spans(ctx: JobContext) -> None:
    """One span per provider job once its terminal status was seen (or the job ended)."""
    for job_hash, info in ctx.provider_jobs.items():
        if not info["terminal"] and not ctx.terminal:
            continue                                   # still running: wait for the outcome
        outcome = info["final"] or "no_terminal_status_seen"
        outcomes = info["outcomes"]
        session = info["session"]
        ctx.rows.add("trace_spans", _span(
            ctx, did("span", ctx.job_id, "provider_job", job_hash), kind="provider_job",
            name=info["tool"], service="higgsfield", parent=session.span_id,
            started=info["first"], ended=info["terminal"] or info["last"], outcome=outcome,
            session=session, provider_hash=job_hash, scene_id=info["scene"],
            attributes={"polls": info["polls"], "creates": info["creates"],
                        "call_outcomes": outcomes},
            method="provider_job_id_in_tool_io", confidence=1.0))


def revise_note(prompt: str) -> Optional[Tuple[str, str, str]]:
    """(stage text, job id, reviewer note) of a runner revise prompt, without the text the
    runner wraps around the note (runner._revise_prompt)."""
    match = REVISE_RE.search(prompt or "")
    if not match:
        return None
    note = match.group(3)
    end = note.rfind(REVISE_TEMPLATE_END)
    if end >= 0:
        note = note[:end]
    cuts = [note.find(marker) for marker in REVISE_NOTE_SUFFIXES]
    cuts = [c for c in cuts if c >= 0]
    if cuts:
        note = note[:min(cuts)]
    elif note.endswith("."):
        note = note[:-1]                              # the '.' the template adds after it
    return match.group(1), match.group(2), note.strip()


def build_revise_rows(ctx: JobContext) -> None:
    """A revise leg's prompt carries the reviewer's note: one approval event per revise leg
    (its retries repeat the same prompt), received just before the leg's first attempt."""
    for leg in ctx.legs:
        if not leg["label"].endswith("_revise"):
            continue
        sessions = [s for s in leg["sessions"] if not s.info.is_subagent]
        first = sessions[0] if sessions else None
        parsed = revise_note(first.prompt_text or "") if first else None
        if first is None or parsed is None or parsed[1] != ctx.job_id \
                or first.record_id is None:
            continue
        _stage_text, _job, note = parsed
        phase = leg["label"][: -len("_revise")]
        red = Redactor()
        ctx.rows.add("approval_events", {
            "approval_event_id": did("apr", ctx.job_id, f"revise:{leg['event_key']}"),
            "job_id": ctx.job_id,
            "checkpoint_id": None,
            "ingestion_record_id": first.record_id,
            "event_key": f"revise:{leg['event_key']}"[:500],
            "gate": f"stage:{leg['stage']}",
            "decision": "revise",
            "answer_redacted": clip(red.text(note), 4000)[0],
            "provenance": "legacy_inferred",
            "actor_reference": None,
            "received_at": iso(first.first_ts or leg["start"]),
            "metadata_redacted": {"inferred_from": "revise_leg_prompt", "phase": phase,
                                  "label": leg["label"], "received_at_is_leg_start": True},
        })


# ===========================================================================
# MCP logs: connector health per session
# ===========================================================================

_MCP_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = (
    ("mcp.connection.error", re.compile(r"connection error|terminal connection error", re.I),
     "error"),
    ("mcp.connection.dropped", re.compile(
        r"connection (?:closed|dropped)(?!.*\(cleanly\))|stream dropped", re.I), "warning"),
    ("mcp.tools.fetch_failed", re.compile(r"failed to fetch tools", re.I), "error"),
    ("mcp.call.failed", re.compile(r"^Tool \S+ failed after", re.I), "warning"),
    ("mcp.negotiation.timeout", re.compile(r"negotiation probe timed out", re.I), "warning"),
)


def build_mcp_log_rows(ctx: JobContext, logs: List[McpLogInfo]) -> None:
    by_session: Dict[str, Session] = {s.session_id: s for s in ctx.sessions
                                      if not s.info.is_subagent}
    for info in logs:
        session = by_session.get(info.session_id or "")
        if session is None:
            continue
        calls = completed = failures = drops = 0
        longest = 0
        path = Path(info.path)
        source = None
        higgsfield_log = "higgsfield" in info.server.lower()
        for number, raw, record in read_jsonl(path):
            message = str(record.get("error") or record.get("debug") or "")
            if message.startswith("Calling MCP tool"):
                calls += 1
                continue
            if message.startswith("Successfully connected"):
                if higgsfield_log:
                    session.mcp["log_connected"] = True
                continue
            if "(cleanly)" in message or message.startswith("Cleared connection cache"):
                continue                               # a normal shutdown, not a drop
            timing = re.search(r"completed successfully in (\d+)ms", message)
            if timing:
                completed += 1
                longest = max(longest, int(timing.group(1)))
                continue
            name = severity = None
            if record.get("error"):
                name, severity = "mcp.error", "error"
            for event_name, pattern, level in _MCP_PATTERNS:
                if pattern.search(message):
                    name, severity = event_name, level
                    break
            if not name:
                continue
            failures += severity == "error"
            drops += name == "mcp.connection.dropped"
            if source is None:
                source = ctx.source("mcp_log", f"mcp/{path.parent.name}/{path.name}",
                                    {"server": info.server, "session_id": info.session_id})
            ctx.event(name, f"mcplog:{path.parent.name}:{path.name}:{number}",
                      source_service=f"mcp:{info.server}", severity=severity or "warning",
                      occurred_at=parse_ts(record.get("timestamp")), outcome=info.server,
                      message=message[:1000], agent_run_id=session.agent_run_id,
                      span_id=session.span_id)
        if (calls or failures or drops) and session.complete:
            # Once per log, when its session is over (running totals would add up twice).
            ctx.event("mcp.log.summary", f"mcpsummary:{path.parent.name}:{path.name}",
                      source_service=f"mcp:{info.server}",
                      severity="warning" if failures or drops else "info",
                      occurred_at=session.last_ts, outcome=info.server,
                      attributes={"server": info.server, "calls": calls, "completed": completed,
                                  "failures": failures, "drops": drops,
                                  "longest_call_ms": longest or None},
                      agent_run_id=session.agent_run_id, span_id=session.span_id)


# ===========================================================================
# Approvals inferred from checkpoint history
# ===========================================================================

def _gate_enabled(ctx: JobContext, stage: str) -> bool:
    """False when options.gates is set and omits the stage (the launcher then approves it
    itself: runner._script_gate_enabled, the carousel gate-collapse)."""
    options = ctx.state.get("options") if isinstance(ctx.state.get("options"), dict) else {}
    gates = options.get("gates")
    if not gates or not isinstance(gates, (list, tuple)):
        return True
    names = {str(g).lower().replace("approve_", "").strip() for g in gates}
    return stage in names


def build_approval_rows(ctx: JobContext) -> None:
    """awaiting_human -> completed+human_approved is an approval. When the completed version
    was written outside every leg window, the launcher wrote it at /respond time, so its
    timestamp is the receipt time; inside a window the agent wrote it (time is approximate).

    A version written after the last finished leg while the job is running may belong to a
    leg that has no timing line yet: it waits for that line, so who wrote it is known. An
    approval the launcher made itself (the stage's gate is switched off) is not a human
    decision: it is recorded as an event, not as an approval."""
    by_stage: Dict[str, List[Dict[str, Any]]] = {}
    for cp in ctx.checkpoints:
        by_stage.setdefault(cp["stage"], []).append(cp)
    last_leg_end = ctx.legs[-1]["end"] if ctx.legs else None
    running = str(ctx.state.get("status") or "") == "running"
    for stage, versions in by_stage.items():
        versions.sort(key=lambda c: c["ts"])
        for before, after in zip(versions, versions[1:]):
            if not (before["status"] == "awaiting_human" and after["status"] == "completed"
                    and after["human_approved"]):
                continue
            if running and (last_leg_end is None or after["ts"] > last_leg_end):
                continue                               # decided once the running leg ends
            if not _gate_enabled(ctx, stage):
                ctx.event("gate.auto_approved", f"auto_approve:{stage}:{after['sha']}",
                          source_service="launcher", occurred_at=after["ts"], stage=stage,
                          outcome="gate_disabled", record_id=after["record_id"],
                          attributes={"note": "options.gates omits this stage, so the launcher "
                                              "approved it without asking"})
                continue
            # The launcher writes the approval just before it spawns the next leg (within a
            # second or two), so only the exact leg windows count here.
            launcher_write = ctx.leg_at(after["ts"], slack_s=0) is None
            ctx.rows.add("approval_events", {
                "approval_event_id": did("apr", ctx.job_id, f"approve:{stage}:{after['sha']}"),
                "job_id": ctx.job_id,
                "checkpoint_id": before["id"],
                "ingestion_record_id": after["record_id"],
                "event_key": f"approve:{stage}:{after['sha'][:16]}",
                "gate": f"stage:{stage}",
                "decision": "approve",
                "answer_redacted": None,
                "provenance": "legacy_inferred",
                "actor_reference": None,
                "received_at": iso(after["ts"]),
                "metadata_redacted": {
                    "inferred_from": "checkpoint_history_awaiting_to_completed",
                    "written_by": "launcher" if launcher_write else "agent_or_unknown",
                    "received_at_exact": launcher_write},
            })


# ===========================================================================
# Manifest, tool events, artifacts
# ===========================================================================

def _current_manifest(ctx: JobContext) -> Tuple[Optional[Dict[str, Any]], Optional[Path],
                                                Optional[bytes]]:
    """The live asset manifest: checkpoint_assets.json artifacts, else the artifacts file."""
    path = ctx.project_path("checkpoint_assets.json")
    data, raw = read_json(path)
    if isinstance(data, dict) and isinstance((data.get("artifacts") or {}).get("asset_manifest"),
                                             dict):
        return data["artifacts"]["asset_manifest"], path, raw
    for rel in ("artifacts/asset_manifest.json", "asset_manifest.json"):
        path = ctx.project_path(rel)
        data, raw = read_json(path)
        if isinstance(data, dict):
            return data, path, raw
    return None, None, None


def build_manifest_rows(ctx: JobContext) -> None:
    manifest, path, raw = _current_manifest(ctx)
    if manifest is None or path is None or raw is None:
        return
    rel = path.relative_to(ctx.project_path()).as_posix()
    # One logical source whichever copy is live (checkpoint_assets.json or the artifacts
    # file): the cost views count the latest snapshot per source, so two sources would keep
    # an abandoned copy's takes counting.
    source = ctx.source("project_file", f"projects/{ctx.job_id}/asset_manifest:live",
                        {"series": "asset_manifest"})
    manifest_sha = sha256_hex(json.dumps(manifest, sort_keys=True, default=str))
    observed = mtime_dt(path) or ctx.exported_at
    record_id = ctx.record(source, f"manifest:{manifest_sha}", manifest_sha, observed,
                           {"read_from": rel})
    items = 0
    asset_paths: Dict[str, str] = {}
    for asset in manifest.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        asset_path = str(asset.get("path") or "").replace("\\", "/")
        if asset.get("id") and asset_path:
            asset_paths[str(asset["id"])] = asset_path
        if asset_path:
            ctx.manifest_items[asset_path] = asset
            ctx.manifest_items[asset_path.rsplit("/", 1)[-1]] = asset
        credits = as_float(asset.get("credits"))
        if credits is None:
            continue
        provider = str(asset.get("provider") or "unknown")
        asset_key = asset.get("id") or asset_path or sha256_hex(asset)[:16]
        domain = f"{provider}:asset:{asset_key}"
        ctx.rows.add("cost_observations", {
            "cost_observation_id": did("cost", record_id, domain),
            "job_id": ctx.job_id,
            "agent_run_id": None,
            "ingestion_record_id": record_id,
            "domain_key": domain[:500],
            "provider": provider,
            "usage_kind": "manifest_credits",
            "unit": "credits",
            "amount": max(credits, 0.0),
            "cost_usd": None,
            "basis": "item",
            "estimated": True,
            "observed_at": iso(observed),
            "metadata_redacted": Redactor().json({
                "scene_id": asset.get("scene_id"), "type": asset.get("type"),
                "source_tool": asset.get("source_tool"), "model": asset.get("model"),
                "credits_source": asset.get("credits_source"), "path": asset_path,
                "note": "agent-recorded get_cost quote for the selected asset"}),
        })
        items += 1
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    lipsync = metadata.get("lip_sync_qa") if isinstance(metadata.get("lip_sync_qa"), dict) else {}
    # Legacy single-clip scenes live under scenes.<scene>; native-speech jobs keep one review
    # per speaking subshot under subshots.<scene>.<section> (asset_manifest schema).
    reviews: List[Tuple[str, Optional[str], Dict[str, Any]]] = []
    scenes = lipsync.get("scenes") if isinstance(lipsync.get("scenes"), dict) else {}
    for scene_id, scene in scenes.items():
        if isinstance(scene, dict):
            reviews.append((str(scene_id), None, scene))
    subshots = lipsync.get("subshots") if isinstance(lipsync.get("subshots"), dict) else {}
    for scene_id, sections in subshots.items():
        if not isinstance(sections, dict):
            continue
        for section_id, review in sections.items():
            if isinstance(review, dict):
                reviews.append((str(scene_id), str(section_id), review))
    for scene_id, section_id, review in reviews:
        attempts = review.get("attempts") if isinstance(review.get("attempts"), list) else []
        job_hashes = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            job_id = attempt.get("job_id")
            if isinstance(job_id, str) and job_id:
                job_hashes.append(hash_id(job_id))
                asset_id = str(attempt.get("video_asset_id") or "")
                if asset_id in asset_paths:
                    ctx.asset_job_links.append((hash_id(job_id), asset_paths[asset_id]))
        retry = review.get("retry_job_id")
        if isinstance(retry, str) and retry:
            job_hashes.append(hash_id(retry))
        review_sha = sha256_hex(json.dumps(review, sort_keys=True, default=str))
        verdict = enum_value(review.get("status") or review.get("verdict") or review.get("result"))
        concern = (verdict not in (None, "pass", "passed", "ok", "skipped", "not_applicable")
                   or bool(review.get("unresolved_warning")))
        where = f"{scene_id}:{section_id}" if section_id else scene_id
        ctx.event("qa.lipsync.scene", f"lipsync:{where}:{review_sha}", source_service="qa",
                  severity="warning" if concern else "info",
                  occurred_at=observed, stage="assets", scene_id=scene_id,
                  outcome=verdict, record_id=record_id,
                  attributes={"section_id": section_id, "subshot_id": review.get("subshot_id"),
                              "speaker": review.get("speaker"),
                              "selected_take": review.get("selected_take"),
                              "retry_count": review.get("retry_count"),
                              "attempts": len(attempts), "provider_job_hashes": job_hashes,
                              "unresolved_warning": review.get("unresolved_warning"),
                              "detail": review})
    contract = metadata.get("timeline_contract")
    if isinstance(contract, dict):
        contract_sha = sha256_hex(json.dumps(contract, sort_keys=True, default=str))
        summary = {k: contract.get(k) for k in (
            "target_duration_seconds", "output_duration_seconds", "status",
            "within_target_band", "tolerance_fraction")}
        summary["scenes"] = len(contract.get("scenes") or [])
        ctx.event("audio.timeline_contract", f"timeline:{contract_sha}", source_service="audio",
                  occurred_at=observed, stage="assets",
                  outcome=contract.get("status") if isinstance(contract.get("status"), str)
                  else None, record_id=record_id, attributes=summary)


def build_tool_event_rows(ctx: JobContext) -> None:
    """events.jsonl: local tool runs (TTS, voice changer, QA, renders) with ElevenLabs usage."""
    path = ctx.project_path("events.jsonl")
    lines = read_jsonl(path)
    if not lines:
        return
    source = ctx.source("events_jsonl", f"projects/{ctx.job_id}/events.jsonl")
    _unfinished_tool_runs(ctx, lines, source)
    for number, line, entry in lines:
        event = entry.get("event")
        if event not in ("finish", "error"):
            continue
        ts = parse_ts(entry.get("ts"))
        duration = as_float(entry.get("duration_s"))
        if ts is None:
            continue
        started = ts - timedelta(seconds=duration) if duration is not None else None
        tool = str(entry.get("tool") or "tool")
        content_sha = sha256_hex(line)
        record_id = ctx.record(source, f"line:{number}:{content_sha}", content_sha, ts,
                               {"line_number": number})
        success = entry.get("success")
        ok = event == "finish" and success is not False
        kind = "render" if re.search(r"render|compose", tool) else (
            "qa" if "qa" in tool else "tool_call")
        span_id = did("span", ctx.job_id, "toolrun", number, content_sha)
        leg = ctx.leg_at(started or ts)
        red = Redactor()
        attrs = {"scene_id": entry.get("scene_id"), "depth": entry.get("depth"),
                 "cost_usd_reported": entry.get("cost_usd"), "usage": entry.get("usage"),
                 "output_path": entry.get("output_path")}
        ctx.rows.add("trace_spans", _span(
            ctx, span_id, kind=kind, name=tool, service="openmontage_tool",
            stage=leg["stage"] if leg else None, parent=leg["span_id"] if leg else ctx.root_span_id,
            started=started, ended=ts, transport="success" if ok else "error",
            outcome="ok" if ok else "failed", record_id=record_id,
            error=clip(red.text(str(entry.get("error"))), 1000)[0] if entry.get("error") else None,
            scene_id=entry.get("scene_id") if isinstance(entry.get("scene_id"), str) else None,
            attributes=red.json(attrs), method="events_jsonl_finish"))
        ctx.tool_runs.append({"tool": tool, "start": started, "end": ts, "span_id": span_id})
        if leg and ts > leg["end"] + timedelta(seconds=5):
            ctx.event("tool.outlived_leg", f"outlived:{span_id}", source_service="openmontage_tool",
                      severity="warning", occurred_at=ts, stage=leg["stage"], outcome="orphan",
                      attributes={"tool": tool, "leg": leg["label"],
                                  "seconds_after_leg_end": round((ts - leg["end"]).total_seconds(),
                                                                 1)},
                      span_id=span_id)
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else None
        amount = as_float(usage.get("amount")) if usage else None
        if usage and amount is not None:
            platform = str(usage.get("platform") or "unknown")
            domain = f"{platform}:event:{content_sha[:24]}"
            ctx.rows.add("cost_observations", {
                "cost_observation_id": did("cost", ctx.job_id, domain),
                "job_id": ctx.job_id,
                "agent_run_id": None,
                "ingestion_record_id": record_id,
                "domain_key": domain,
                "provider": platform,
                "usage_kind": f"tool_usage:{tool}"[:120],
                "unit": str(usage.get("unit") or "units"),
                "amount": max(amount, 0.0),
                "cost_usd": None,
                "basis": "item",
                "estimated": str(usage.get("source") or "") != "actual",
                "observed_at": iso(ts),
                "metadata_redacted": {"tool": tool, "scene_id": entry.get("scene_id")},
                "span_id": span_id,
            })


def _unfinished_tool_runs(ctx: JobContext, lines: List[Tuple[int, str, Dict[str, Any]]],
                          source: Source) -> None:
    """A 'start' with no finish or error: the tool was killed (with its leg, or a restart).
    Reported once its leg has ended or the job is over; until then it may still finish."""
    open_starts: Dict[str, List[Tuple[int, str, datetime]]] = {}
    for number, line, entry in lines:
        tool = str(entry.get("tool") or "tool")
        ts = parse_ts(entry.get("ts"))
        if ts is None:
            continue
        if entry.get("event") == "start":
            open_starts.setdefault(tool, []).append((number, line, ts))
        elif entry.get("event") in ("finish", "error") and open_starts.get(tool):
            open_starts[tool].pop(0)                  # runs of one tool finish in order
    for tool, starts in open_starts.items():
        for number, line, ts in starts:
            leg = ctx.leg_at(ts)
            over = ctx.terminal or (leg is not None
                                    and ctx.exported_at - leg["end"] > timedelta(hours=1))
            if not over:
                if leg is not None:
                    ctx.deferred.append(f"tool run {tool} may still finish")
                continue                               # it may still be running
            content_sha = sha256_hex(line)
            record_id = ctx.record(source, f"line:{number}:{content_sha}", content_sha, ts,
                                   {"line_number": number})
            ctx.event("tool.no_finish", f"nofinish:{number}:{content_sha[:16]}",
                      source_service="openmontage_tool", severity="warning", occurred_at=ts,
                      stage=leg["stage"] if leg else None, outcome="killed_or_crashed",
                      record_id=record_id, span_id=leg["span_id"] if leg else None,
                      attributes={"tool": tool, "leg": leg["label"] if leg else None})


def _probe(ctx: JobContext, path: Path, sha: str) -> Dict[str, Any]:
    cache: Dict[str, Any] = ctx.cache.setdefault("probes", {})
    if sha in cache:
        return cache[sha]
    result: Dict[str, Any] = {}
    if ctx.settings.ffprobe:
        try:
            proc = subprocess.run(
                [ctx.settings.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                 "-show_streams", str(path)], capture_output=True, timeout=30, check=False,
                stdin=subprocess.DEVNULL)
            data = json.loads(proc.stdout or b"{}") if proc.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            data = {}
        streams = data.get("streams") if isinstance(data.get("streams"), list) else []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
        duration = as_float(fmt.get("duration"))
        if video:
            result.update(width=as_int(video.get("width")), height=as_int(video.get("height")),
                          video_codec=video.get("codec_name"))
            rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
            if "/" in rate:
                num, _sep, den = rate.partition("/")
                try:
                    if float(den):
                        result["fps"] = round(float(num) / float(den), 4)
                except ValueError:
                    pass
        if audio:
            result.update(audio_codec=audio.get("codec_name"),
                          sample_rate=as_int(as_float(audio.get("sample_rate"))))
        if duration is not None:
            result["duration"] = round(duration, 4)
    cache[sha] = result
    return result


def _stage_from_path(rel: str) -> Optional[str]:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    if low.startswith("renders/") or name.startswith("final"):
        return "compose"
    if low.startswith("assets/") or "still" in name or "storyboard" in name or "clip" in name:
        return "assets"
    if "scene_plan" in name:
        return "scene_plan"
    if "script" in name:
        return "script"
    return None


# Pipeline bookkeeping that grows while a job runs and is read as a source of its own: not
# catalogued as artifacts (each run would add another version).
BOOKKEEPING = {"events.jsonl", "artifacts/timing.jsonl", "decision_log.json", "project.json"}
_AGENT_LOG = re.compile(r"artifacts/agent_[^/]+\.log")
_TEMP_NAME = re.compile(r"(?:\.tmp|\.part|\.partial|\.crdownload|\.download)$|\.tmp\.", re.I)


def _superseded_status(rel: str) -> Optional[str]:
    """The launcher's rule for copies that are not the live file
    (dify_launcher/storyboard_preview.is_superseded_still): archived takes and rejects."""
    parts = rel.lower().split("/")
    name = parts[-1]
    if name.startswith(("rejected_", "rejected-")):
        return "rejected"
    if "history" in parts[:-1] or "superseded-stills" in parts[:-1] or ".pre-" in name:
        return "superseded"
    return None


def build_artifact_rows(ctx: JobContext) -> None:
    roots = [("projects", ctx.project_path()), ("launcher", ctx.launcher_path("artifacts"))]
    sha_cache: Dict[str, Any] = ctx.cache.setdefault("file_sha", {})
    seen: Dict[str, Any] = ctx.cache.setdefault("file_sha_seen", {})
    today = int(ctx.exported_at.timestamp() // 86400)
    versions: Dict[str, Any] = ctx.cache.setdefault("artifact_versions", {})
    for root_name, root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            if root_name == "projects" and (rel.startswith("checkpoint_") or
                                            rel.startswith("history/") or rel in BOOKKEEPING
                                            or _AGENT_LOG.fullmatch(rel)):
                continue                               # ingested as sources of their own
            if _TEMP_NAME.search(path.name):
                continue                               # a temp file renamed when complete
            try:
                stat = path.stat()
            except OSError:
                continue
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if ctx.exported_at - modified < YOUNG_FILE:
                # Possibly still being written (ffmpeg -y renders in place for minutes).
                ctx.deferred.append(f"file {root_name}/{safe_relpath(rel)} still changing")
                continue
            key = f"{path}|{stat.st_size}|{stat.st_mtime_ns}"
            sha = sha_cache.get(key) or file_sha256(path)
            if not sha:
                continue
            sha_cache[key] = sha
            seen[key] = today
            suffix = path.suffix.lower()
            artifact_type, mime = MEDIA_TYPES.get(suffix, ("other", None))
            logical = safe_relpath(f"{root_name}/{rel}")
            history = versions.setdefault(f"{ctx.job_id}|{logical}", [])
            if sha not in history:
                history.append(sha)
            probe = _probe(ctx, path, sha) if artifact_type in PROBE_TYPES else {}
            manifest = ctx.manifest_items.get(rel) or ctx.manifest_items.get(path.name) or {}
            retired = _superseded_status(rel)
            selected = retired is None and bool(
                rel in ctx.referenced_paths or path.name in ctx.referenced_paths
                or any(p.endswith("/" + rel) for p in ctx.referenced_paths if "/" in p))
            meta = {k: v for k, v in probe.items() if k not in ("width", "height", "fps",
                                                                 "duration")}
            if manifest:
                meta.update(credits=manifest.get("credits"),
                            credits_source=manifest.get("credits_source"),
                            manifest_id=manifest.get("id"), model=manifest.get("model"))
            artifact_id = did("art", ctx.job_id, logical, sha)
            source = ctx.source("artifact_file", logical)
            red = Redactor()
            ctx.rows.add("artifacts", {
                "artifact_id": artifact_id,
                "job_id": ctx.job_id,
                "checkpoint_id": None,
                "ingestion_record_id": ctx.record(source, f"file:{sha}", sha, modified),
                "logical_id": (red.text(logical) or "")[:500],
                "artifact_version": history.index(sha) + 1,
                "artifact_type": artifact_type,
                "stage": _stage_from_path(rel),
                "scene_id": manifest.get("scene_id") or scene_of(rel),
                "source_tool": manifest.get("source_tool"),
                "provider": manifest.get("provider"),
                "provider_job_id": None,
                "model": manifest.get("model"),
                "storage_backend": "local_project" if root_name == "projects"
                else "launcher_artifacts",
                "object_uri": (red.text(safe_relpath(f"{root_name}/{ctx.job_id}/{rel}"))
                               or "")[:1000],
                "mime_type": mime,
                "byte_size": stat.st_size,
                "content_sha256": sha,
                "duration_seconds": probe.get("duration"),
                "width_pixels": probe.get("width"),
                "height_pixels": probe.get("height"),
                "fps": probe.get("fps"),
                "artifact_status": retired or "available",
                "selected": selected,
                "rejected_reason_redacted": None,
                "metadata_redacted": red.json(meta),
                "created_at": iso(modified),
                "expires_at": None,
            })
            if root_name == "projects":
                if retired is None:
                    ctx.artifact_ids[rel] = artifact_id
                    ctx.artifact_ids.setdefault(path.name, artifact_id)
                ctx.event("artifact.selection", f"selection:{artifact_id}:{selected}",
                          source_service="pipeline", occurred_at=ctx.exported_at,
                          stage=_stage_from_path(rel), outcome="selected" if selected
                          else (retired or "not_selected"), artifact_id=artifact_id,
                          attributes={"path": safe_relpath(rel), "selected": selected})
                if suffix == ".json" and path.name.startswith("_"):
                    _report_event(ctx, path, safe_relpath(rel), sha)
                if rel in ("artifacts/final_review.json", "artifacts/render_report.json"):
                    data, _raw = read_json(path)
                    if isinstance(data, dict):
                        _review_event(ctx, path.stem, data, f"file:{rel}:{sha}", mtime_dt(path))
    for job_hash, asset_path in ctx.asset_job_links:
        rel = asset_path
        root_prefix = str(ctx.project_path()).replace("\\", "/") + "/"
        if rel.startswith(root_prefix):
            rel = rel[len(root_prefix):]
        artifact_id = ctx.artifact_ids.get(rel) or ctx.artifact_ids.get(rel.rsplit("/", 1)[-1])
        if not artifact_id:
            continue
        provider_span = did("span", ctx.job_id, "provider_job", job_hash)
        ctx.rows.add("span_links", {
            "span_link_id": did("link", provider_span, "produces", artifact_id),
            "job_id": ctx.job_id,
            "from_span_id": provider_span,
            "to_span_id": None,
            "to_artifact_id": artifact_id,
            "relation": "produces",
            "metadata_redacted": {"via": "asset_manifest.lip_sync_qa"},
            "correlation_method": "asset_manifest_lip_sync_attempts",
            "correlation_confidence": 0.9,
        })


def _report_event(ctx: JobContext, path: Path, rel: str, sha: str) -> None:
    """Agent scratch reports (assets/**/_*.json): no schema, so stored as sanitized JSON."""
    data, _raw = read_json(path)
    if data is None:
        return
    stem = path.stem.lstrip("_")
    kind = "qa" if "qa" in stem else ("audio" if "/audio/" in f"/{rel}" else "pipeline")
    summary = _report_summary(data)
    text = json.dumps(data, default=str).lower()
    severity = "warning" if kind == "qa" and any(
        w in text for w in ("needs_visual_review", "\"fail", "mismatch")) else "info"
    name = re.sub(r"[^A-Za-z0-9_.\-]", "_", stem)[:100]
    ctx.event(f"report.{name}", f"report:{rel}:{sha}", source_service=kind,
              severity=severity, occurred_at=mtime_dt(path), stage="assets",
              outcome=enum_value(summary.get("verdict")),
              attributes={"path": rel, "summary": summary,
                          "report": clip_json(Redactor().json(data), 48_000)[0]})


def _report_summary(data: Any) -> Dict[str, Any]:
    """Counts of status-like values in a report (status, verdict, result, outcome)."""
    counts: Dict[str, int] = {}

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in ("status", "verdict", "result", "outcome") \
                        and isinstance(item, str):
                    # Only status-like values become keys; free text is counted as "other".
                    label = enum_value(item) or "other"
                    counts[label] = counts.get(label, 0) + 1
                else:
                    walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(data)
    summary: Dict[str, Any] = {"value_counts": counts}
    if len(counts) == 1 and "other" not in counts:
        summary["verdict"] = next(iter(counts))
    return summary


def build_root_span(ctx: JobContext) -> None:
    status = str(ctx.state.get("status") or "")
    created = (parse_ts(ctx.project.get("created_at")) or
               (ctx.legs[0]["start"] if ctx.legs else None))
    if status not in TERMINAL_STATUSES or created is None:
        return
    ended = (parse_ts(ctx.state.get("processing_finished_at")) or
             parse_ts(ctx.state.get("updated_at")) or (ctx.legs[-1]["end"] if ctx.legs else None))
    ctx.rows.add("trace_spans", _span(
        ctx, ctx.root_span_id, kind="job", name=str(ctx.state.get("pipeline") or "job"),
        service="launcher", stage=ctx.state.get("stage"), started=created, ended=ended,
        transport="success" if status == "done" else "error", outcome=status,
        attributes={"legs": len(ctx.legs), "sessions": len(ctx.sessions)},
        method="project_created_to_terminal_state", confidence=0.9))


# ===========================================================================
# Export orchestration
# ===========================================================================

def build_all_rows(job_id: str, settings: Settings, transcripts: List[TranscriptInfo],
                   mcp_logs: List[McpLogInfo], cache: Dict[str, Any],
                   exported_at: datetime) -> JobContext:
    """All rows for one job. Sessions are mapped to legs first (ids are deterministic), then
    parsed (their totals feed agent_runs), then legs, approvals, artifacts and provider
    spans are built."""
    ctx = JobContext(job_id, settings, exported_at, cache)
    build_job_rows(ctx)
    build_checkpoint_rows(ctx)
    build_decision_rows(ctx)
    build_leg_rows(ctx)
    build_manifest_rows(ctx)
    map_sessions(ctx, transcripts)
    assign_attempt_logs(ctx)
    for session in ctx.sessions:
        build_session_rows(ctx, session)
    finish_leg_rows(ctx)
    build_revise_rows(ctx)
    build_mcp_log_rows(ctx, mcp_logs)
    for session in ctx.sessions:
        if session.complete and not session.info.is_subagent:
            _mcp_summary(ctx, session)
    build_tool_event_rows(ctx)
    build_approval_rows(ctx)
    build_artifact_rows(ctx)
    build_provider_spans(ctx)
    build_root_span(ctx)
    return ctx


def discover_jobs(settings: Settings) -> List[str]:
    jobs: Set[str] = set()
    for base in (settings.data_dir / "jobs", settings.projects_dir):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.is_dir() and re.fullmatch(r"job_[A-Za-z0-9_\-]+", entry.name):
                jobs.add(entry.name)
    return sorted(jobs)


def job_fingerprint(settings: Settings, job_id: str, transcripts: List[TranscriptInfo],
                    mcp_logs: List[McpLogInfo]) -> Tuple[str, List[str]]:
    """(digest of the job's own files, one short digest per transcript/MCP log). They are
    kept apart so that Claude Code deleting old transcripts (cleanupPeriodDays) does not
    count as a change: a job is rebuilt only when a file is new or different."""
    digest = hashlib.sha256()
    for base in (settings.data_dir / "jobs" / job_id, settings.projects_dir / job_id):
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                digest.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    digest.update(PARSER_VERSION.encode())
    sessions = {t.session_id for t in transcripts if not t.is_subagent and t.job_id == job_id}
    logs = [f"{t.path}|{t.size}|{t.mtime_ns}" for t in transcripts if t.session_id in sessions]
    logs += [f"{m.path}|{m.size}|{m.mtime_ns}" for m in mcp_logs if m.session_id in sessions]
    return digest.hexdigest(), sorted(sha256_hex(entry)[:16] for entry in logs)


def _ids_path(state_dir: Path, job_id: str) -> Path:
    return state_dir / "jobs" / f"{job_id}.ids"


def load_exported(state_dir: Path, job_id: str) -> Dict[str, str]:
    """{row key: row digest} of the rows already written for this job."""
    path = _ids_path(state_dir, job_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out: Dict[str, str] = {}
    for line in lines:
        key, _sep, digest = line.partition(" ")
        if key:
            out[key] = digest
    return out


def save_exported(state_dir: Path, job_id: str, exported: Dict[str, str]) -> None:
    path = _ids_path(state_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{k} {v}\n" for k, v in sorted(exported.items())), encoding="utf-8")
    os.replace(tmp, path)


def write_batch(target_dir: Path, job_id: str, rows: List[Tuple[str, Dict[str, Any]]],
                final: bool, settings: Settings, exported_at: datetime, shared: bool) -> Path:
    # Last line of defence: no U+0000 or lone surrogate reaches the spool from any column.
    body = b"\n".join(json.dumps({"table": table, "row": clean_value(row)}, ensure_ascii=False,
                                 sort_keys=True, default=str).encode("utf-8")
                      for table, row in rows)
    header = {
        "format": BATCH_FORMAT, "version": BATCH_VERSION, "job_id": job_id,
        "created_at": iso(exported_at), "exporter_version": EXPORTER_VERSION,
        "parser_version": PARSER_VERSION, "redaction_policy": REDACTION_POLICY,
        "host": settings.host_label, "final": final, "row_count": len(rows),
        "rows_sha256": hashlib.sha256(body).hexdigest(),
    }
    data = json.dumps(header, sort_keys=True).encode("utf-8") + b"\n" + body + b"\n"
    job_dir = target_dir / job_id
    _mkdir_shared(job_dir, shared)
    stamp = exported_at.strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{stamp}-{job_id}-{header['rows_sha256'][:12]}.jsonl.gz"
    tmp = job_dir / f".{name}.tmp"
    with open(tmp, "wb") as raw_handle:
        with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as handle:
            handle.write(data)
        raw_handle.flush()
        os.fsync(raw_handle.fileno())
    if os.name == "posix":
        os.chmod(tmp, 0o640)
    final_path = job_dir / name
    os.replace(tmp, final_path)
    return final_path


def _mkdir_shared(path: Path, shared: bool) -> None:
    """Spool folders the loader's group can read and move files out of (setgid, 2770)."""
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True)
    if shared and os.name == "posix":
        for folder in missing:
            try:
                os.chmod(folder, 0o2770)
            except OSError:
                pass


def _row_digest(row: Dict[str, Any]) -> str:
    return sha256_hex(json.dumps(row, sort_keys=True, default=str))[:16]


VOLATILE_COLUMNS = ("observed_at", "first_seen_at", "occurred_at")


def export_job(job_id: str, settings: Settings, transcripts: List[TranscriptInfo],
               mcp_logs: List[McpLogInfo], state: Dict[str, Any], target: Path, *,
               full: bool, dry_run: bool, exported_at: datetime) -> Dict[str, Any]:
    try:
        ctx = build_all_rows(job_id, settings, transcripts, mcp_logs, state, exported_at)
    except DeferJob as exc:
        return {"job_id": job_id, "skipped": str(exc), "rows": 0, "new": 0, "settled": False}
    exported = {} if full else load_exported(settings.state_dir, job_id)
    new_rows: List[Tuple[str, Dict[str, Any]]] = []
    updated = dict(exported)
    for table, row in ctx.rows.items():
        key = f"{table}:{row[PRIMARY_KEYS[table]]}"
        digest = _row_digest({k: v for k, v in row.items() if k not in VOLATILE_COLUMNS})
        previous = exported.get(key)
        if previous is None or (table in MUTABLE_TABLES and previous != digest):
            new_rows.append((table, row))
            updated[key] = digest
    summary: Dict[str, Any] = {"job_id": job_id, "rows": len(ctx.rows), "new": len(new_rows),
                               "sessions": len(ctx.sessions), "legs": len(ctx.legs),
                               "status": ctx.state.get("status"), "warnings": ctx.warnings}
    if new_rows:
        path = write_batch(target, job_id, new_rows, ctx.terminal, settings, exported_at,
                           shared=not dry_run)
        summary["batch"] = str(path)
    if not dry_run and (new_rows or updated != exported):
        save_exported(settings.state_dir, job_id, updated)
    # Settled: nothing is held back that time alone would release (a running session, a
    # file still being written). An unchanged settled job is skipped on later runs.
    summary["settled"] = not ctx.deferred
    if ctx.deferred:
        summary["deferred"] = ctx.deferred[:5]
    return summary


def read_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _prune_cache(state: Dict[str, Any], today: int, keep_days: int = 30,
                 limit: int = 500_000) -> None:
    """Drop file hashes not seen for keep_days (by last-seen day, not by name), then the
    oldest-seen ones above `limit`; probe results only for hashes no longer cached."""
    hashes = state.get("file_sha")
    seen = state.get("file_sha_seen")
    if not isinstance(hashes, dict) or not isinstance(seen, dict):
        state["file_sha"], state["file_sha_seen"] = {}, {}
        return
    for key in list(hashes):
        if today - int(seen.get(key) or 0) > keep_days:
            hashes.pop(key, None)
            seen.pop(key, None)
    if len(hashes) > limit:
        for key in sorted(hashes, key=lambda k: int(seen.get(k) or 0))[: len(hashes) - limit]:
            hashes.pop(key, None)
            seen.pop(key, None)
    for key in list(seen):
        if key not in hashes:
            seen.pop(key, None)
    probes = state.get("probes")
    if isinstance(probes, dict):
        live = set(hashes.values())
        for sha in list(probes):
            if sha not in live:
                probes.pop(sha, None)


def run_export(settings: Settings, *, only: Optional[str], full: bool,
               dry_run: Optional[Path]) -> int:
    started = time.monotonic()
    exported_at = utc_now()
    state_path = settings.state_dir / "export_state.json"
    state = read_state(state_path)
    jobs_state: Dict[str, Any] = state.setdefault("jobs", {})
    transcripts = discover_transcripts(settings, state)
    mcp_logs = discover_mcp_logs(settings, state)
    target = dry_run or (settings.spool / "pending")
    if not dry_run and not settings.spool.is_dir():
        print(json.dumps({"ok": False, "error": f"spool {settings.spool} does not exist; "
                          "create it as described in deploy/README.md"}))
        return 2
    results: List[Dict[str, Any]] = []
    errors = 0
    for job_id in discover_jobs(settings):
        if only and job_id != only:
            continue
        files_fp, logs_fp = job_fingerprint(settings, job_id, transcripts, mcp_logs)
        known = jobs_state.get(job_id) or {}
        # Unchanged: same job files, and no transcript or MCP log is new or different
        # (deleted ones do not count). Rechecked anyway while something waits on time.
        unchanged = (known.get("files") == files_fp
                     and set(logs_fp) <= set(known.get("logs") or []))
        if not full and not dry_run and unchanged and known.get("settled"):
            continue
        try:
            summary = export_job(job_id, settings, transcripts, mcp_logs, state, target,
                                 full=full, dry_run=bool(dry_run), exported_at=exported_at)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the others
            errors += 1
            results.append({"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        results.append(summary)
        if not dry_run and "skipped" not in summary:  # a skipped job is retried next run
            jobs_state[job_id] = {"files": files_fp, "logs": logs_fp,
                                  "exported_at": iso(exported_at),
                                  "status": summary.get("status"),
                                  "settled": bool(summary.get("settled"))}
    if not dry_run:
        _prune_cache(state, int(exported_at.timestamp() // 86400))
        state.update(version=EXPORTER_VERSION, last_run=iso(exported_at))
        write_state(state_path, state)
    print(json.dumps({
        "ok": errors == 0, "exported_at": iso(exported_at),
        "duration_s": round(time.monotonic() - started, 1),
        "jobs_exported": sum(1 for r in results if "error" not in r and "skipped" not in r),
        "jobs_skipped": sum(1 for r in results if "skipped" in r),
        "batches": sum(1 for r in results if r.get("batch")),
        "rows": sum(int(r.get("new") or 0) for r in results),
        "transcripts_seen": len(transcripts), "mcp_logs_seen": len(mcp_logs), "errors": errors,
        "results": results if (only or dry_run or errors) else
        [r for r in results if r.get("batch")][:50],
    }, sort_keys=True, default=str))
    return 1 if errors else 0


# ===========================================================================
# Cron install and status
# ===========================================================================

def _crontab_read() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, timeout=15, check=False)
    text = proc.stdout.decode("utf-8", "surrogateescape")
    if proc.returncode == 0:
        return text
    if "no crontab" in (proc.stderr.decode("utf-8", "replace") + text).lower():
        return ""
    raise SystemExit("could not read the current crontab, so it was not changed")


def install(settings: Settings, every: int) -> int:
    if os.name != "posix" or os.geteuid() == 0:
        raise SystemExit("run --install on the box as the launcher's user (not root)")
    if not 1 <= every <= 59:
        raise SystemExit("--every must be 1-59 minutes")
    if not settings.spool.is_dir():
        raise SystemExit(f"spool {settings.spool} does not exist; create it first "
                         "(deploy/README.md, 'Job history in PostgreSQL')")
    script = Path(__file__).resolve()
    python = "/usr/bin/python3" if os.access("/usr/bin/python3", os.X_OK) else sys.executable
    if " " in str(script) or " " in python:
        raise SystemExit("paths with spaces are not supported in the cron line")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    flock = shutil.which("flock", path="/usr/bin:/bin:/usr/local/bin") or ""
    lock = f"{flock} -n {settings.state_dir / 'export.lock'} " if flock else ""
    path_env = ":".join(p for p in (str(Path(settings.ffprobe).parent) if settings.ffprobe
                                    else "", "/usr/local/bin", "/usr/bin", "/bin") if p)
    extra = "" if str(settings.spool) == "/var/spool/panda-ingest" else f" --spool {settings.spool}"
    log_path = Path.home() / "panda-ingest.log"
    # Low CPU and IO priority: the export must never slow a render or a leg.
    nice = shutil.which("nice", path="/usr/bin:/bin") or ""
    ionice = shutil.which("ionice", path="/usr/bin:/bin") or ""
    gentle = (f"{nice} -n 10 " if nice else "") + (f"{ionice} -c3 " if ionice else "")
    line = (f"*/{every} * * * * PATH={path_env} {lock}{gentle}{python} {script}{extra} "
            f">> {log_path} 2>&1")
    current = _crontab_read()
    backup = settings.state_dir / f"crontab.before.{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    backup.write_text(current, encoding="utf-8", errors="surrogateescape")
    kept = [ln for ln in current.splitlines() if CRON_MARKER not in ln]
    new = "\n".join(kept + [line]) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new.encode("utf-8", "surrogateescape"),
                          capture_output=True, timeout=15, check=False)
    if proc.returncode != 0:
        raise SystemExit("crontab rejected the update: "
                         + proc.stderr.decode("utf-8", "replace")[-300:])
    print(json.dumps({"installed": line, "crontab_backup": str(backup)}, indent=2))
    return 0


def status(settings: Settings) -> int:
    state = read_state(settings.state_dir / "export_state.json")

    def count(folder: str) -> int:
        base = settings.spool / folder
        return len(list(base.rglob("*.jsonl.gz"))) if base.is_dir() else 0

    print(json.dumps({
        "last_run": state.get("last_run"), "jobs_tracked": len(state.get("jobs") or {}),
        "transcripts_tracked": len(state.get("transcripts") or {}),
        "mcp_logs_tracked": len(state.get("mcp_logs") or {}),
        "spool": str(settings.spool), "pending_batches": count("pending"),
        "loaded_batches_kept": count("done"), "failed_batches": count("failed"),
        "data_dir": str(settings.data_dir), "projects_dir": str(settings.projects_dir),
        "transcript_dirs": [str(p) for p in settings.transcript_dirs],
        "mcp_log_dirs": [str(p) for p in settings.mcp_log_dirs],
    }, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--job", help="export only this job id")
    parser.add_argument("--full", action="store_true",
                        help="re-send every row, ignoring what was exported before")
    parser.add_argument("--dry-run", type=Path, metavar="DIR",
                        help="write batches to DIR instead of the spool; change no state")
    parser.add_argument("--repo")
    parser.add_argument("--data-dir")
    parser.add_argument("--projects-dir")
    parser.add_argument("--transcripts-dir", action="append",
                        help="Claude transcript folder (repeatable)")
    parser.add_argument("--mcp-logs-dir", action="append",
                        help="Claude MCP log folder for the repo (repeatable)")
    parser.add_argument("--spool")
    parser.add_argument("--state-dir")
    parser.add_argument("--no-probe", action="store_true", help="skip ffprobe media metadata")
    parser.add_argument("--install", action="store_true", help="add the export cron line")
    parser.add_argument("--every", type=int, default=5, help="cron interval in minutes")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings(args)
    if args.install:
        return install(settings, args.every)
    if args.status:
        return status(settings)
    if args.dry_run:
        args.dry_run.mkdir(parents=True, exist_ok=True)
    return run_export(settings, only=args.job, full=args.full, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
