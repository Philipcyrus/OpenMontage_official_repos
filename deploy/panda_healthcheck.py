#!/usr/bin/env python3
"""Daily, zero-generation health check for every layer of the Panda launcher stack.

A Dify job crosses all of these layers, so one cron run checks all of them:

  launcher        local GET /health: up, the expected runner (claude, not mock), launcher
                  code not stale, and the loaded revision matching the checkout.
  launcher_reach  the same /health over the box's private IP. A 127.0.0.1-only bind passes
                  every loopback check yet gives Dify a 502 on everything.
  public_url      the URL Dify calls (DNS -> TLS -> proxy -> this launcher) and TLS expiry.
  launcher_env    what the RUNNING launcher resolves on its own PATH (claude, ffmpeg, and
                  Node 22 when screenshot jobs are on); no Anthropic key or base-URL override
                  that would take the legs off the subscription login; and what the next
                  restart would load from .env.
  claude_auth     `claude auth status --json`, using the binary the launcher's legs use.
  higgsfield_mcp  one bounded Claude Haiku turn calling the Claude.ai Higgsfield `balance`
                  tool. The structured tool exchange is verified, never the model's prose.
  elevenlabs      the API key authenticates and the character quota is not nearly spent.
  render          a 1-second ffmpeg/libx264 encode read back by ffprobe, plus Remotion
                  readiness when screenshot jobs are enabled.
  disk            free space where jobs, projects and Claude transcripts live.
  dify            Dify's file server (screenshot downloads) and any extra URLs.

Nothing generates media. Higgsfield `balance` and ElevenLabs `/v1/user/subscription` cost no
credits, and the Claude turn is capped by turns, budget and wall time. Secrets are read only
from the launcher's .env, used in memory and never printed. On a state change the script can
publish to AWS SNS and/or a Slack, Discord or Google Chat webhook. It always prints one JSON
summary and exits non-zero when unhealthy, so cron and people read the same result.

SETUP (once per box, as the user that owns the Claude login; no launcher restart):
  python3 deploy/panda_healthcheck.py --install          # daily 02:30 UTC (08:00 IST)
  python3 deploy/panda_healthcheck.py --install "40 2"   # another time (MINUTE HOUR, UTC)
  python3 deploy/panda_healthcheck.py --test-alert       # one test message
  python3 deploy/panda_healthcheck.py --no-alert         # run every check now
--install adds only this check's own line to the user's crontab (backing it up first).

SETTINGS go in the launcher's .env (process env and ~/.config/panda-healthcheck.env win).
Everything has a default; normally only the first three are set:
  PANDA_HEALTH_WEBHOOK_URL       Slack/Discord/Google Chat webhook (a credential)
  PANDA_HEALTH_PUBLIC_URL        Dify's BASE_URL for this box, e.g. https://dev.om.mvnoc.ai
  PANDA_HEALTH_BOX_NAME          label for alerts (default: hostname)
  PANDA_HEALTH_WEBHOOK_KIND      slack | discord | generic (Google Chat)       [slack]
  PANDA_HEALTH_SNS_TOPIC_ARN     AWS SNS topic instead of / as well as the webhook
  PANDA_HEALTH_NOTIFY_SUCCESS    a short OK message every day                  [true]
  PANDA_HEALTH_REMINDER_HOURS    repeat an unchanged failure after N hours     [24]
  PANDA_HEALTH_EXPECT_RUNNER     runner production must use; off disables      [claude]
  PANDA_HEALTH_LOW_CREDITS       Higgsfield credit threshold; off disables     [500]
  PANDA_HEALTH_ELEVENLABS_LOW_CHARS  ElevenLabs characters left; off disables  [10000]
  PANDA_HEALTH_DISK_MIN_FREE_GB / _PCT   free-space thresholds                 [5 / 10]
  PANDA_HEALTH_TLS_WARN_DAYS     warn this many days before the cert expires   [14]
  PANDA_HEALTH_EXTRA_URLS        name=https://... list; any HTTP answer < 500 passes
  PANDA_HEALTH_REQUIRE_REMOTION  auto (when DIFY_FILES_HOSTS is set) | true | false
  PANDA_HEALTH_CLAUDE_BIN        default: the claude the running launcher resolves
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


BALANCE_TOOL = "mcp__claude_ai_Higgsfield__balance"
DEFAULT_REPO = Path(__file__).resolve().parents[1]
ELEVENLABS_SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"
USER_AGENT = "panda-healthcheck/2.0"
MIN_NODE_MAJOR = 22            # tools/video/screen_overlay.py: Remotion screenshot scenes
REMINDER_SLACK_S = 600         # a daily cron starts a few seconds early on some days

COMPONENTS = (
    "launcher", "launcher_reach", "public_url", "launcher_env", "claude_auth",
    "higgsfield_mcp", "elevenlabs", "render", "disk", "dify",
)

# Settings read from the launcher's environment: the live process first, then its .env.
CONFIG_KEYS = (
    "CLAUDE_BIN", "DIFY_RUNNER", "DIFY_DATA_DIR", "OPENMONTAGE_PROJECTS_DIR",
    "DIFY_FILES_HOSTS", "DIFY_FILES_BASE", "FFMPEG_BIN", "FFPROBE_BIN",
    "OPENMONTAGE_BUILD_REVISION",
)
# Any of these set takes the claude legs off the subscription login, and with it off the
# Claude.ai Higgsfield connector. Only whether each is non-empty is kept, never the value.
ANTHROPIC_OVERRIDE_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)

_USAGE_LIMIT_MARKERS = (
    "usage limit", "weekly limit", "5-hour limit", "hour limit reached", "hit your limit",
)
_OVERLOAD_RE = re.compile(r"\b529\b|overloaded")
_RATE_LIMIT_RE = re.compile(r"\b429\b|rate[ _-]?limit")
_SECRET_RE = re.compile(
    r"\b(sk-ant-|sk-or-|sk_|xox[abposr]-)[A-Za-z0-9_\-]+"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=\-]{12,}"
    r"|[A-Za-z0-9_\-]{40,}"
)


@dataclass
class CheckResult:
    component: str
    ok: bool
    code: str
    detail: str
    severity: str = "critical"
    metadata: Optional[Dict[str, Any]] = None


Problem = Tuple[str, str, str]      # (severity, code, detail)


@dataclass
class Config:
    repo: Path
    claude_bin: Optional[Path]          # explicit override; None = the launcher's own claude
    launcher_url: str
    launcher_env_file: Path
    expect_runner: str                  # "" disables the runner check
    public_url: str
    tls_warn_days: Optional[int]                 # None = no expiry warning
    claude_model: str
    claude_timeout_s: int
    claude_max_budget_usd: float
    retry_count: int
    retry_delay_s: int
    low_credits: Optional[float]
    elevenlabs_enabled: bool
    elevenlabs_low_chars: Optional[int]
    require_remotion: Optional[bool]    # None = needed when DIFY_FILES_HOSTS is set
    disk_min_free_gb: Optional[float]            # None = no low-space warning
    disk_min_free_pct: Optional[float]           # (the 1 GB / 3% critical floor stays)
    extra_urls: List[Tuple[str, str]]
    box_name: str
    state_file: Path
    reminder_hours: int
    webhook_url: str
    webhook_kind: str
    sns_topic_arn: str
    aws_region: Optional[str]
    notify_success: bool


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_raw(name: str, default: Any) -> str:
    """A blank value means the default, so clearing a setting never breaks the run."""
    return os.environ.get(name, "").strip() or str(default)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(_env_raw(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(_env_raw(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_number_or_off(name: str, default: str, cast: Callable[[str], Any]) -> Optional[Any]:
    raw = _env_raw(name, default)
    if raw.lower() in {"off", "none"}:
        return None
    try:
        return cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number or off") from exc


def _env_auto_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name, "auto").strip().lower()
    if raw in {"", "auto"}:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be auto, true or false")


def parse_extra_urls(raw: str) -> List[Tuple[str, str]]:
    """``name=https://host/path,https://other`` -> [(name, url), ...]."""
    urls: List[Tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, url = item.partition("=")
        if not sep or "://" in name:
            name, url = "", item
        url = url.strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("PANDA_HEALTH_EXTRA_URLS entries must be name=http(s)://host/...")
        urls.append((name.strip() or parsed.hostname, url))
    return urls


def config_from_env() -> Config:
    repo = Path(os.environ.get("PANDA_HEALTH_REPO", str(DEFAULT_REPO))).expanduser()
    claude_raw = os.environ.get("PANDA_HEALTH_CLAUDE_BIN", "").strip()
    runner = os.environ.get("PANDA_HEALTH_EXPECT_RUNNER", "claude").strip()
    return Config(
        repo=repo,
        claude_bin=Path(claude_raw).expanduser() if claude_raw else None,
        launcher_url=os.environ.get(
            "PANDA_HEALTH_LAUNCHER_URL", "http://127.0.0.1:8501/health"
        ).strip(),
        launcher_env_file=Path(
            os.environ.get("PANDA_HEALTH_LAUNCHER_ENV_FILE", "").strip() or str(repo / ".env")
        ).expanduser(),
        expect_runner="" if runner.lower() in {"", "off", "none"} else runner,
        public_url=os.environ.get("PANDA_HEALTH_PUBLIC_URL", "").strip(),
        tls_warn_days=_env_number_or_off("PANDA_HEALTH_TLS_WARN_DAYS", "14", int),
        claude_model=os.environ.get("PANDA_HEALTH_CLAUDE_MODEL", "haiku"),
        claude_timeout_s=_env_int("PANDA_HEALTH_CLAUDE_TIMEOUT_S", 90, 10),
        claude_max_budget_usd=_env_float("PANDA_HEALTH_MAX_BUDGET_USD", 0.10, 0.01),
        retry_count=_env_int("PANDA_HEALTH_RETRY_COUNT", 1, 0),
        retry_delay_s=_env_int("PANDA_HEALTH_RETRY_DELAY_S", 60, 0),
        low_credits=_env_number_or_off("PANDA_HEALTH_LOW_CREDITS", "500", float),
        elevenlabs_enabled=_env_bool("PANDA_HEALTH_ELEVENLABS", True),
        elevenlabs_low_chars=_env_number_or_off(
            "PANDA_HEALTH_ELEVENLABS_LOW_CHARS", "10000", int
        ),
        require_remotion=_env_auto_bool("PANDA_HEALTH_REQUIRE_REMOTION"),
        disk_min_free_gb=_env_number_or_off("PANDA_HEALTH_DISK_MIN_FREE_GB", "5", float),
        disk_min_free_pct=_env_number_or_off("PANDA_HEALTH_DISK_MIN_FREE_PCT", "10", float),
        extra_urls=parse_extra_urls(os.environ.get("PANDA_HEALTH_EXTRA_URLS", "")),
        box_name=os.environ.get("PANDA_HEALTH_BOX_NAME", "").strip() or socket.gethostname(),
        state_file=Path(
            os.environ.get(
                "PANDA_HEALTH_STATE_FILE",
                "~/.local/state/panda-healthcheck/status.json",
            )
        ).expanduser(),
        reminder_hours=_env_int("PANDA_HEALTH_REMINDER_HOURS", 24, 1),
        webhook_url=os.environ.get("PANDA_HEALTH_WEBHOOK_URL", "").strip(),
        webhook_kind=os.environ.get("PANDA_HEALTH_WEBHOOK_KIND", "slack").strip().lower(),
        sns_topic_arn=os.environ.get("PANDA_HEALTH_SNS_TOPIC_ARN", "").strip(),
        aws_region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        notify_success=_env_bool("PANDA_HEALTH_NOTIFY_SUCCESS", True),
    )


# ---------------------------------------------------------------------------
# Env files and the running launcher's environment
# ---------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env_text(text: str) -> Dict[str, str]:
    """KEY=VALUE lines as a shell assigns them, without executing or expanding anything.

    Handles ``export KEY=...``, a value wrapped in single or double quotes, and a trailing
    ``# comment`` after an unquoted value. Later lines win, as when a shell sources the file.
    """
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
            # A # after whitespace starts a comment (KEY=   # note is empty); KEY=#x is literal.
            comment = re.search(r"\s#", raw_value)
            value = (raw_value[:comment.start()] if comment else raw_value).strip()
        values[key] = value
    return values


def load_env_file(path: Path) -> None:
    """Load the health check's own settings file. Existing process variables win."""
    if not path.is_file():
        return
    for key, value in parse_env_text(path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)


@dataclass
class DotEnv:
    """What the launcher's .env sets: the next restart loads exactly this."""
    found: bool
    values: Dict[str, str]                       # CONFIG_KEYS only
    overrides: Dict[str, bool]                   # ANTHROPIC_OVERRIDE_KEYS -> non-empty
    elevenlabs_key: Optional[str] = field(default=None, repr=False)
    error: str = ""


def read_launcher_dotenv(path: Path) -> DotEnv:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return DotEnv(False, {}, {})
    except OSError as exc:
        return DotEnv(False, {}, {}, error=_safe_error(exc))
    parsed = parse_env_text(text)
    return DotEnv(
        True,
        {key: parsed[key] for key in CONFIG_KEYS if key in parsed},
        {key: bool(parsed.get(key)) for key in ANTHROPIC_OVERRIDE_KEYS},
        elevenlabs_key=parsed.get("ELEVENLABS_API_KEY") or None,
    )


@dataclass
class LauncherProcess:
    pid: int
    host: str
    port: int
    env: Dict[str, str]                          # PATH and CONFIG_KEYS only
    overrides: Dict[str, bool]                   # ANTHROPIC_OVERRIDE_KEYS -> non-empty
    # The key the legs really send (memory only): a rotated key in .env is not live yet.
    elevenlabs_key: Optional[str] = field(default=None, repr=False)


def _uvicorn_bind(args: Sequence[str]) -> Tuple[str, int]:
    host, port = "127.0.0.1", 8000               # uvicorn's own defaults
    for index, arg in enumerate(args):
        for flag in ("--host", "--port"):
            value: Optional[str] = None
            if arg == flag and index + 1 < len(args):
                value = args[index + 1]
            elif arg.startswith(flag + "="):
                value = arg.split("=", 1)[1]
            if value is None:
                continue
            if flag == "--host":
                host = value
            else:
                try:
                    port = int(value)
                except ValueError:
                    pass
    return host, port


def _read_process_env(
    path: Path,
) -> Optional[Tuple[Dict[str, str], Dict[str, bool], Optional[str]]]:
    """From /proc/<pid>/environ: PATH, CONFIG_KEYS, override presence, the ElevenLabs key.

    Nothing else is kept. The key stays in memory for the one quota call; never printed.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    env: Dict[str, str] = {}
    overrides = {key: False for key in ANTHROPIC_OVERRIDE_KEYS}
    elevenlabs_key: Optional[str] = None
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if not sep:
            continue
        name = key.decode("utf-8", "replace")
        if name == "PATH" or name in CONFIG_KEYS:
            env[name] = value.decode("utf-8", "replace")
        elif name in overrides:
            overrides[name] = bool(value)
        elif name == "ELEVENLABS_API_KEY" and value:
            elevenlabs_key = value.decode("utf-8", "replace")
    return env, overrides, elevenlabs_key


def find_launcher_process(
    port: int, proc_root: Path = Path("/proc")
) -> Optional[LauncherProcess]:
    """The uvicorn process serving dify_launcher on ``port`` (Linux /proc only)."""
    if not proc_root.is_dir():
        return None
    try:
        entries = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return None
    for entry in entries:
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if not any("uvicorn" in arg for arg in args):
            continue
        if not any(arg.startswith("dify_launcher.app") for arg in args):
            continue
        host, proc_port = _uvicorn_bind(args)
        if proc_port != port:
            continue
        env = _read_process_env(entry / "environ")
        if env is None:
            continue
        return LauncherProcess(int(entry.name), host, proc_port, env[0], env[1],
                               elevenlabs_key=env[2])
    return None


@dataclass
class Context:
    """Facts gathered once and shared by the checks."""
    process: Optional[LauncherProcess]
    dotenv: DotEnv
    search_path: str                             # the launcher's own PATH when readable
    tool_env: Dict[str, str] = field(repr=False)
    launcher_claude: Optional[str] = None
    canary_claude: Optional[str] = None
    canary_claude_source: str = ""
    ffmpeg: Optional[str] = None
    ffprobe: Optional[str] = None
    remotion_needed: bool = False
    data_dir: Path = DEFAULT_REPO / "data"
    projects_dir: Path = DEFAULT_REPO / "projects"
    expected_revision: Optional[str] = None
    dify_files_base: str = ""


def _which(name: str, search_path: str) -> Optional[str]:
    if not name:
        return None
    if os.sep in name or (os.altsep and os.altsep in name):
        path = Path(name).expanduser()
        return str(path) if path.is_file() and os.access(str(path), os.X_OK) else None
    return shutil.which(name, path=search_path)


def _resolve_dir(value: str, repo: Path, default: Path) -> Path:
    if not value.strip():
        return default
    path = Path(os.path.expandvars(value.strip())).expanduser()
    return path if path.is_absolute() else repo / path


def git_head(repo: Path) -> Optional[str]:
    rc, stdout, _stderr, _timed_out = _run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], cwd=repo, timeout_s=10,
    )
    head = stdout.strip()
    return head if rc == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else None


def launcher_code_changed(repo: Path, loaded: str, expected: str) -> Optional[List[str]]:
    """Python files the running launcher may have imported that changed since it started.

    None when git cannot tell (then a restart is assumed to be due). Docs, tests, deploy/
    and data files do not count: the launcher reads those fresh or never imports them.
    """
    rc, stdout, _stderr, _timed_out = _run(
        ["git", "-C", str(repo), "diff", "--name-only", loaded, expected],
        cwd=repo, timeout_s=15,
    )
    if rc != 0:
        return None
    return [
        name for name in (line.strip() for line in stdout.splitlines())
        if name.endswith(".py") and not name.startswith(("tests/", "deploy/"))
        and not Path(name).name.startswith("test_")
    ]


def gather_context(config: Config, proc_root: Path = Path("/proc")) -> Context:
    port = urllib.parse.urlsplit(config.launcher_url).port or 80
    process = find_launcher_process(port, proc_root)
    dotenv = read_launcher_dotenv(config.launcher_env_file)

    def setting(key: str) -> str:
        if process is not None and key in process.env:
            return process.env[key]
        return dotenv.values.get(key, "")

    search_path = (process.env.get("PATH") if process else None) or os.environ.get(
        "PATH", os.defpath
    )
    tool_env = {k: v for k, v in os.environ.items() if not k.startswith("PANDA_HEALTH_")}
    tool_env["PATH"] = search_path
    launcher_claude = _which(setting("CLAUDE_BIN").strip() or "claude", search_path)
    if config.claude_bin is not None:
        canary, source = str(config.claude_bin), "PANDA_HEALTH_CLAUDE_BIN"
    elif launcher_claude:
        canary, source = launcher_claude, "launcher PATH" if process else "PATH"
    else:
        canary, source = None, "not found"
    remotion = config.require_remotion
    if remotion is None:
        remotion = bool(setting("DIFY_FILES_HOSTS").strip())
    return Context(
        process=process,
        dotenv=dotenv,
        search_path=search_path,
        tool_env=tool_env,
        launcher_claude=launcher_claude,
        canary_claude=canary,
        canary_claude_source=source,
        ffmpeg=_which(setting("FFMPEG_BIN").strip() or "ffmpeg", search_path),
        ffprobe=_which(setting("FFPROBE_BIN").strip() or "ffprobe", search_path),
        remotion_needed=remotion,
        data_dir=_resolve_dir(setting("DIFY_DATA_DIR"), config.repo, config.repo / "data"),
        projects_dir=_resolve_dir(
            setting("OPENMONTAGE_PROJECTS_DIR"), config.repo, config.repo / "projects"
        ),
        expected_revision=(
            None if setting("OPENMONTAGE_BUILD_REVISION").strip() else git_head(config.repo)
        ),
        dify_files_base=setting("DIFY_FILES_BASE").strip(),
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _run(
    argv: Sequence[str], *, cwd: Path, timeout_s: int, env: Optional[Dict[str, str]] = None
) -> Tuple[int, str, str, bool]:
    try:
        proc = subprocess.run(
            list(argv), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout_s, check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return 124, stdout, stderr, True
    except OSError as exc:
        return 127, "", str(exc), False


def http_get(
    url: str, *, headers: Optional[Dict[str, str]] = None, timeout: float = 10.0,
    use_proxy: bool = True, max_bytes: int = 256 * 1024,
) -> Tuple[int, bytes]:
    """GET ``url``. Returns (status, body) for any HTTP answer; raises on network failure."""
    handlers: List[Any] = [] if use_proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(max_bytes)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(max_bytes)
        except Exception:  # noqa: BLE001 — the status code is the useful part
            body = b""
        return exc.code, body


def _json_object(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _net_error_kind(exc: BaseException) -> str:
    reason: Any = getattr(exc, "reason", None) or exc
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        return "tls"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    text = str(reason).lower()
    if "timed out" in text:
        return "timeout"
    if "refused" in text:
        return "refused"
    return "network"


def _host_of(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or url
    return f"{host}:{parsed.port}" if parsed.port else host


def primary_ipv4() -> Optional[str]:
    """The address of the interface that routes off the box. A UDP connect sends nothing."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not address or address.startswith("127.") or address == "0.0.0.0":
        return None
    return address


def tls_days_left(host: str, port: int = 443, timeout: float = 10.0) -> float:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    not_after = cert.get("notAfter") if cert else None
    if not not_after:
        raise ValueError("the certificate carries no expiry date")
    return (ssl.cert_time_to_seconds(str(not_after)) - time.time()) / 86400.0


def summarize(
    component: str, problems: Sequence[Problem], ok_code: str, ok_detail: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> CheckResult:
    """One result per component: the worst problem leads, every problem is listed."""
    if not problems:
        return CheckResult(component, True, ok_code, ok_detail, severity="info",
                           metadata=metadata)
    ordered = sorted(problems, key=lambda problem: SEVERITY_RANK.get(problem[0], 9))
    severity, code, _detail = ordered[0]
    meta = dict(metadata or {})
    if len(ordered) > 1:
        meta["codes"] = [problem[1] for problem in ordered]
    return CheckResult(component, False, code, "; ".join(p[2] for p in ordered),
                       severity=severity, metadata=meta or None)


def result_codes(result: CheckResult) -> List[str]:
    codes = (result.metadata or {}).get("codes")
    return list(codes) if isinstance(codes, list) else [result.code]


def _node_major(node: str, env: Dict[str, str], cwd: Path) -> Optional[int]:
    rc, stdout, _stderr, _timed_out = _run([node, "-v"], cwd=cwd, timeout_s=20, env=env)
    match = re.match(r"v?(\d+)\.", stdout.strip())
    return int(match.group(1)) if rc == 0 and match else None


# ---------------------------------------------------------------------------
# Launcher layers
# ---------------------------------------------------------------------------

def check_launcher(config: Config, ctx: Context) -> Tuple[CheckResult, Optional[Dict[str, Any]]]:
    try:
        status, raw = http_get(config.launcher_url, timeout=5, use_proxy=False)
    except NET_ERRORS as exc:
        return CheckResult("launcher", False, "LAUNCHER_DOWN", _safe_error(exc)), None
    body = _json_object(raw)
    if status != 200 or body.get("status") != "ok":
        return CheckResult(
            "launcher", False, "LAUNCHER_UNHEALTHY",
            f"HTTP {status}; status={body.get('status')!r}",
        ), None
    problems: List[Problem] = []
    runner = body.get("runner")
    if config.expect_runner and runner != config.expect_runner:
        problems.append((
            "critical", "LAUNCHER_WRONG_RUNNER",
            f"the launcher runs the {runner!r} runner, not {config.expect_runner!r}: a "
            "restart that skipped sourcing .env starts the mock runner, which fakes results",
        ))
    if body.get("code_stale") is True:
        problems.append((
            "critical", "LAUNCHER_CODE_STALE",
            "launcher code changed on disk since the process started; POST /jobs answers "
            "503 until it is restarted",
        ))
    loaded = str(body.get("build_revision") or "")
    expected = ctx.expected_revision
    if loaded and loaded != "unknown" and expected and loaded != expected:
        changed = launcher_code_changed(config.repo, loaded, expected)
        if changed is None or changed:
            shown = ""
            if changed:
                more = ", ..." if len(changed) > 3 else ""
                shown = f" ({', '.join(changed[:3])}{more})"
            problems.append((
                "warning", "LAUNCHER_RESTART_PENDING",
                f"the launcher loaded {loaded[:12]} but the checkout is at {expected[:12]}"
                f"{shown}; restart it when no job is running",
            ))
    metadata = {
        "runner": runner,
        "async": body.get("async"),
        "montage_door": body.get("montage_door"),
        "build_revision": loaded[:12] or None,
        "process_started_at": body.get("process_started_at"),
    }
    return summarize("launcher", problems, "LAUNCHER_OK",
                     f"launcher /health ok (runner {runner})", metadata), body


def check_launcher_reach(
    config: Config, ctx: Context, local_body: Optional[Dict[str, Any]]
) -> CheckResult:
    if local_body is None:
        return CheckResult("launcher_reach", True, "REACH_NOT_TESTED",
                           "skipped: the launcher does not answer on loopback", severity="info")
    port = urllib.parse.urlsplit(config.launcher_url).port or 80
    host_flag = ctx.process.host if ctx.process else None
    loopback_bind = bool(host_flag) and (
        host_flag == "localhost" or str(host_flag).startswith("127.") or host_flag == "::1"
    )
    address = primary_ipv4()
    if address is None:
        if loopback_bind:
            return CheckResult(
                "launcher_reach", False, "LAUNCHER_LOOPBACK_ONLY",
                f"uvicorn was started with --host {host_flag}: the reverse proxy and Dify "
                "cannot reach it (502)",
            )
        return CheckResult("launcher_reach", True, "REACH_NOT_TESTED",
                           "could not determine this box's private IP", severity="info")
    try:
        status, raw = http_get(f"http://{address}:{port}/health", timeout=5, use_proxy=False)
    except NET_ERRORS as exc:
        if _net_error_kind(exc) == "refused" or loopback_bind:
            return CheckResult(
                "launcher_reach", False, "LAUNCHER_LOOPBACK_ONLY",
                f"the launcher answers on 127.0.0.1 but refuses {address}:{port} "
                f"(--host {host_flag or 'unknown'}): the reverse proxy and Dify get 502",
            )
        return CheckResult("launcher_reach", False, "LAUNCHER_PRIVATE_UNREACHABLE",
                           f"{address}:{port}: {_safe_error(exc)}", severity="warning")
    body = _json_object(raw)
    if status != 200 or body.get("status") != "ok":
        return CheckResult("launcher_reach", False, "LAUNCHER_PRIVATE_UNHEALTHY",
                           f"{address}:{port} answered HTTP {status}", severity="warning")
    if body.get("process_started_at") != local_body.get("process_started_at"):
        return CheckResult(
            "launcher_reach", False, "LAUNCHER_REACH_OTHER_PROCESS",
            f"{address}:{port} is served by a different process than 127.0.0.1:{port}",
            severity="warning",
        )
    return CheckResult("launcher_reach", True, "LAUNCHER_REACHABLE",
                       f"reachable on {address}:{port}, not only on loopback", severity="info")


def _public_health_url(base: str) -> str:
    """Dify's BASE_URL (root or a sub-path) -> its /health URL."""
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path.rstrip("/")
    if not path.endswith("/health"):
        path += "/health"
    return urllib.parse.urlunsplit(parsed._replace(path=path, query="", fragment=""))


def check_public_url(config: Config, local_body: Optional[Dict[str, Any]]) -> CheckResult:
    if not config.public_url:
        return CheckResult(
            "public_url", True, "PUBLIC_URL_NOT_CONFIGURED",
            "not tested: set PANDA_HEALTH_PUBLIC_URL to Dify's BASE_URL to cover DNS, TLS "
            "and the reverse proxy", severity="info",
        )
    url = _public_health_url(config.public_url)
    host = _host_of(url)
    status, raw, error = 0, b"", None
    for attempt in range(2):
        try:
            status, raw = http_get(url, timeout=15)
            error = None
        except NET_ERRORS as exc:
            error = exc
        if error is None and status < 500:
            break
        if attempt == 0:
            time.sleep(10)
    if error is not None:
        kind = _net_error_kind(error)
        code = {"dns": "PUBLIC_DNS_FAILED", "tls": "PUBLIC_TLS_FAILED",
                "timeout": "PUBLIC_TIMEOUT"}.get(kind, "PUBLIC_UNREACHABLE")
        return CheckResult("public_url", False, code, f"{host}: {_safe_error(error)}")
    body = _json_object(raw)
    if status != 200 or body.get("status") != "ok":
        hint = (" — the proxy cannot reach the launcher (bind address, security group, or "
                "the launcher is down)") if status in {502, 503, 504} else ""
        return CheckResult("public_url", False, "PUBLIC_HTTP_ERROR",
                           f"{host}/health answered HTTP {status}{hint}")
    problems: List[Problem] = []
    if config.expect_runner and body.get("runner") != config.expect_runner:
        problems.append((
            "critical", "PUBLIC_WRONG_RUNNER",
            f"Dify's URL reaches a {body.get('runner')!r} launcher, not "
            f"{config.expect_runner!r}",
        ))
    if local_body and body.get("process_started_at") != local_body.get("process_started_at"):
        problems.append((
            "warning", "PUBLIC_URL_OTHER_LAUNCHER",
            f"{host} is served by a different launcher process than this box's "
            "127.0.0.1 one (another box, or a second launcher)",
        ))
    metadata: Dict[str, Any] = {"host": host}
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        try:
            days = tls_days_left(parsed.hostname, parsed.port or 443)
            metadata["tls_days_left"] = round(days, 1)
            if config.tls_warn_days is not None and days < config.tls_warn_days:
                problems.append((
                    "warning", "PUBLIC_TLS_EXPIRING",
                    f"the TLS certificate for {parsed.hostname} expires in {days:.0f} days",
                ))
        except (OSError, ssl.SSLError, ValueError) as exc:
            metadata["tls_days_left"] = None
            metadata["tls_error"] = _safe_error(exc)
    return summarize("public_url", problems, "PUBLIC_URL_OK",
                     f"{host} reaches this launcher", metadata)


def check_launcher_env(
    config: Config, ctx: Context, local_body: Optional[Dict[str, Any]]
) -> CheckResult:
    problems: List[Problem] = []
    runner = (local_body or {}).get("runner") or config.expect_runner
    process = ctx.process
    metadata: Dict[str, Any] = {
        "launcher_pid": process.pid if process else None,
        "launcher_claude": ctx.launcher_claude,
    }
    # ffmpeg and Node on the launcher's PATH are covered by the render check, which runs them.
    if process is not None:
        overrides = sorted(key for key, present in process.overrides.items() if present)
        if overrides:
            problems.append((
                "critical", "LEGS_API_KEY_MODE",
                f"the running launcher has {', '.join(overrides)} set: its claude legs "
                "bypass the subscription login and lose the Claude.ai Higgsfield connector",
            ))
        if runner == "claude" and not ctx.launcher_claude:
            problems.append((
                "critical", "LAUNCHER_CLAUDE_MISSING",
                "claude is not on the running launcher's PATH: every real job fails at its "
                "first leg",
            ))
    if ctx.dotenv.found:
        live_overrides = process is not None and any(process.overrides.values())
        planned = sorted(key for key, present in ctx.dotenv.overrides.items() if present)
        if planned and not live_overrides:
            problems.append((
                "warning", "NEXT_RESTART_API_KEY_MODE",
                f".env sets {', '.join(planned)}: after the next restart the legs leave the "
                "subscription login and the Higgsfield connector",
            ))
        next_runner = ctx.dotenv.values.get("DIFY_RUNNER", "").strip()
        if config.expect_runner and next_runner != config.expect_runner:
            problems.append((
                "warning", "NEXT_RESTART_WRONG_RUNNER",
                f".env has DIFY_RUNNER={next_runner or '(unset, so mock)'}: the next restart "
                f"would not start the {config.expect_runner} runner",
            ))
    elif ctx.dotenv.error:
        problems.append(("warning", "LAUNCHER_ENV_UNREADABLE",
                         f"cannot read {config.launcher_env_file}: {ctx.dotenv.error}"))
    if (ctx.canary_claude and ctx.launcher_claude and process is not None
            and os.path.realpath(ctx.canary_claude) != os.path.realpath(ctx.launcher_claude)):
        problems.append((
            "warning", "HEALTHCHECK_CLAUDE_DIFFERS",
            f"this check runs {ctx.canary_claude} but the launcher's legs run "
            f"{ctx.launcher_claude}; remove PANDA_HEALTH_CLAUDE_BIN to test the real one",
        ))
    if process is None:
        detail = ("running launcher process not readable from /proc; checked .env only"
                  if ctx.dotenv.found else "running launcher process and .env not readable")
    else:
        detail = f"launcher pid {process.pid} resolves claude; legs use the subscription login"
    return summarize("launcher_env", problems, "LAUNCHER_ENV_OK", detail, metadata)


# ---------------------------------------------------------------------------
# Claude and Higgsfield
# ---------------------------------------------------------------------------

def check_claude_auth(config: Config, ctx: Context) -> CheckResult:
    binary = ctx.canary_claude
    if not binary or not Path(binary).is_file():
        return CheckResult(
            "claude_auth", False, "CLAUDE_BINARY_MISSING",
            f"no claude binary found ({ctx.canary_claude_source}: {binary or 'claude'})",
        )
    rc, stdout, stderr, timed_out = _run(
        [binary, "auth", "status", "--json"], cwd=config.repo, timeout_s=15, env=ctx.tool_env,
    )
    if timed_out:
        return CheckResult(
            "claude_auth", False, "CLAUDE_AUTH_TIMEOUT",
            "claude auth status exceeded 15 seconds", severity="warning",
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return CheckResult(
            "claude_auth", False, "CLAUDE_AUTH_CHECK_FAILED",
            _redacted_tail(stderr or stdout or f"exit code {rc}"),
        )
    if not isinstance(data, dict) or rc != 0 or not data.get("loggedIn"):
        return CheckResult(
            "claude_auth", False, "CLAUDE_LOGGED_OUT",
            "Claude Code reports that no authenticated account is available",
        )
    _vrc, version_out, _verr, _vto = _run(
        [binary, "--version"], cwd=config.repo, timeout_s=15, env=ctx.tool_env,
    )
    version = version_out.strip().split(" ")[0] if version_out.strip() else None
    return CheckResult(
        "claude_auth", True, "CLAUDE_AUTH_PRESENT",
        f"authenticated via {data.get('authMethod') or 'unknown method'}",
        severity="info",
        metadata={
            "auth_method": data.get("authMethod"),
            "api_provider": data.get("apiProvider"),
            "subscription_type": data.get("subscriptionType"),
            "claude_version": version,
            "claude_binary": binary,
            "claude_binary_source": ctx.canary_claude_source,
        },
    )


def _json_lines(text: str) -> Iterable[Dict[str, Any]]:
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _result_text(content: Any) -> Optional[str]:
    """Flatten a tool_result ``content`` field to text.

    The MCP transport may deliver it as a bare string or as a list of content
    blocks; treating only the string shape as valid would report a healthy
    Higgsfield as unavailable.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    return None


def _result_payload(content: Any) -> Optional[Dict[str, Any]]:
    """Return the JSON object a tool_result carries, in either content shape."""
    text = _result_text(content)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _readable_text(stdout: str, stderr: str, records: Sequence[Dict[str, Any]]) -> str:
    """stderr, non-JSON stdout lines, and the text fields of the stream-json records."""
    parts = [stderr]
    for line in stdout.splitlines():
        try:
            json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
    for record in records:
        for key in ("result", "error"):
            if isinstance(record.get(key), str):
                parts.append(record[key])
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "tool_result":
                parts.append(_result_text(block.get("content")) or "")
    return "\n".join(parts).lower()


def parse_canary_stream(stdout: str, stderr: str, timed_out: bool) -> CheckResult:
    """Classify the real MCP exchange from Claude's stream-json output."""
    records = list(_json_lines(stdout))
    balance_ids: List[str] = []
    result_by_id: Dict[str, Dict[str, Any]] = {}
    saw_assistant = False

    for record in records:
        if record.get("type") == "assistant":
            saw_assistant = True
            message = record.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "tool_use" and block.get("name") == BALANCE_TOOL:
                    if block.get("id"):
                        balance_ids.append(str(block["id"]))
        elif record.get("type") == "user":
            message = record.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "tool_result" and block.get("tool_use_id"):
                    result_by_id[str(block["tool_use_id"])] = block

    for tool_id in balance_ids:
        block = result_by_id.get(tool_id)
        if not block:
            continue
        content = block.get("content")
        parsed = _result_payload(content)
        if not block.get("is_error", False) and isinstance(parsed, dict):
            credits = parsed.get("credits")
            if isinstance(credits, (int, float)) and not isinstance(credits, bool):
                return CheckResult(
                    "higgsfield_mcp", True, "HIGGSFIELD_OK",
                    "real Higgsfield balance tool call succeeded", severity="info",
                    metadata={
                        "credits": credits,
                        "subscription_plan_type": parsed.get("subscription_plan_type"),
                    },
                )
        error_text = _content_text(content)
        low = error_text.lower()
        if any(word in low for word in ("auth", "oauth", "login", "unauthorized", "forbidden")):
            return CheckResult(
                "higgsfield_mcp", False, "HIGGSFIELD_AUTH_FAILED",
                _redacted_tail(error_text),
            )
        return CheckResult(
            "higgsfield_mcp", False, "HIGGSFIELD_UNAVAILABLE",
            _redacted_tail(error_text or "balance returned an invalid result"),
            severity="warning",
        )

    # Match error markers only in human-readable text: the raw JSON is full of ids, token
    # counts and durations, and "cache_creation_input_tokens": 25295 is not an HTTP 529.
    combined = _readable_text(stdout, stderr, records)
    if any(text in combined for text in (
        "oauth session expired", "failed to authenticate", "authentication_error",
    )):
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_OAUTH_EXPIRED",
            "Claude live request could not authenticate or refresh OAuth",
        )
    if any(text in combined for text in _USAGE_LIMIT_MARKERS):
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_USAGE_LIMIT",
            "the Claude subscription has hit its usage limit; claude legs stall until it "
            "resets",
        )
    if timed_out:
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_TIMEOUT",
            "Claude/Higgsfield canary exceeded its wall-time limit", severity="warning",
        )
    if _OVERLOAD_RE.search(combined):
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_OVERLOADED",
            "Claude returned a temporary overload response", severity="warning",
        )
    if _RATE_LIMIT_RE.search(combined):
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_RATE_LIMITED",
            "Claude returned a rate-limit response", severity="warning",
        )
    stopped = next((
        str(record.get("subtype")) for record in records
        if record.get("type") == "result" and str(record.get("subtype", "")).startswith("error")
    ), None)
    if stopped:
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_CANARY_STOPPED",
            f"the canary turn ended early ({stopped}) before a real balance result",
            severity="warning",
        )
    if saw_assistant and not balance_ids:
        return CheckResult(
            "higgsfield_mcp", False, "HIGGSFIELD_NOT_DISCOVERED",
            "Claude answered, but never obtained the Higgsfield balance tool",
        )
    return CheckResult(
        "higgsfield_mcp", False, "CLAUDE_API_UNREACHABLE",
        _redacted_tail(stderr or stdout or "Claude produced no usable response"),
        severity="warning",
    )


def check_higgsfield_once(config: Config, ctx: Context) -> CheckResult:
    prompt = (
        "Health canary only. First find the Higgsfield balance tool, then call balance "
        "exactly once. Never call a generation, shell, file, web, or write tool. After a "
        "successful real balance result, reply exactly HIGGSFIELD_OK. On any tool error, "
        "reply HIGGSFIELD_FAIL and the error."
    )
    if not ctx.canary_claude:
        return CheckResult("higgsfield_mcp", False, "CLAUDE_BINARY_MISSING",
                           "no claude binary found to run the canary")
    env = dict(ctx.tool_env)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    argv = [
        str(ctx.canary_claude), "-p", prompt,
        "--model", config.claude_model,
        "--max-turns", "4",
        "--max-budget-usd", str(config.claude_max_budget_usd),
        "--permission-mode", "dontAsk",
        "--allowedTools", f"ToolSearch,{BALANCE_TOOL}",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
    ]
    _rc, stdout, stderr, timed_out = _run(
        argv, cwd=config.repo, timeout_s=config.claude_timeout_s, env=env,
    )
    return parse_canary_stream(stdout, stderr, timed_out)


def check_higgsfield(config: Config, ctx: Context) -> CheckResult:
    attempts = config.retry_count + 1
    result = CheckResult(
        "higgsfield_mcp", False, "HIGGSFIELD_UNAVAILABLE", "not attempted"
    )
    no_retry = {"CLAUDE_OAUTH_EXPIRED", "HIGGSFIELD_AUTH_FAILED", "CLAUDE_USAGE_LIMIT"}
    attempt = 0
    for attempt in range(attempts):
        result = check_higgsfield_once(config, ctx)
        if result.ok or result.code in no_retry or attempt == attempts - 1:
            break
        if config.retry_delay_s:
            time.sleep(config.retry_delay_s)
    if result.metadata is None:
        result.metadata = {}
    result.metadata["attempts"] = attempt + 1
    if result.ok and config.low_credits is not None:
        credits = result.metadata.get("credits")
        if isinstance(credits, (int, float)) and credits < config.low_credits:
            return CheckResult(
                "higgsfield_mcp", False, "HIGGSFIELD_LOW_CREDITS",
                f"Higgsfield balance {credits:g} is below threshold {config.low_credits:g}",
                severity="warning", metadata=result.metadata,
            )
    return result


# ---------------------------------------------------------------------------
# ElevenLabs, render toolchain, disk, Dify
# ---------------------------------------------------------------------------

def check_elevenlabs(config: Config, ctx: Context) -> CheckResult:
    if not config.elevenlabs_enabled:
        return CheckResult("elevenlabs", True, "ELEVENLABS_NOT_CHECKED",
                           "disabled by PANDA_HEALTH_ELEVENLABS", severity="info")
    # Test the key the running launcher's legs send; .env only takes effect at a restart.
    live = ctx.process.elevenlabs_key if ctx.process else None
    planned = ctx.dotenv.elevenlabs_key
    key = os.environ.get("ELEVENLABS_API_KEY") or live or planned
    if not key:
        return CheckResult(
            "elevenlabs", False, "ELEVENLABS_KEY_MISSING",
            f"ELEVENLABS_API_KEY is empty in {config.launcher_env_file}: narration, voice "
            "matching and music would fail",
        )
    pending: List[Problem] = []
    if live and ctx.dotenv.found and not hmac.compare_digest(live, planned or ""):
        pending.append((
            "warning", "NEXT_RESTART_ELEVENLABS_KEY",
            ".env has a different ELEVENLABS_API_KEY than the running launcher; the legs use "
            "the running one until the launcher is restarted",
        ))
    try:
        status, raw = http_get(
            ELEVENLABS_SUBSCRIPTION_URL,
            headers={"xi-api-key": key, "Accept": "application/json"}, timeout=15,
        )
    except NET_ERRORS as exc:
        return CheckResult("elevenlabs", False, "ELEVENLABS_UNREACHABLE",
                           _safe_error(exc), severity="warning")
    data = _json_object(raw)
    detail = data.get("detail")
    detail_status = str(detail.get("status", "")) if isinstance(detail, dict) else ""
    detail_message = str(detail.get("message", "")) if isinstance(detail, dict) else str(
        detail or "")
    if status == 200:
        count, limit = data.get("character_count"), data.get("character_limit")
        metadata: Dict[str, Any] = {"tier": data.get("tier"), "status": data.get("status")}
        reset = data.get("next_character_count_reset_unix")
        if isinstance(reset, (int, float)) and not isinstance(reset, bool):
            metadata["resets_at"] = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        problems: List[Problem] = list(pending)
        summary = "key valid"
        if (isinstance(count, int) and isinstance(limit, int)
                and not isinstance(count, bool) and not isinstance(limit, bool)):
            remaining = max(limit - count, 0)
            metadata["characters_left"] = remaining
            summary = f"key valid; {remaining:,} characters left this period"
            if config.elevenlabs_low_chars is not None and remaining < config.elevenlabs_low_chars:
                problems.append((
                    "warning", "ELEVENLABS_LOW_CHARACTERS",
                    f"only {remaining:,} ElevenLabs characters left (threshold "
                    f"{config.elevenlabs_low_chars:,})",
                ))
        if str(data.get("status", "")).lower() in {"past_due", "unpaid", "incomplete_expired"}:
            problems.append((
                "warning", "ELEVENLABS_BILLING",
                f"the ElevenLabs subscription status is {data.get('status')!r}",
            ))
        return summarize("elevenlabs", problems, "ELEVENLABS_OK", summary, metadata)
    if status in {401, 403}:
        if "missing_permission" in detail_status.lower() or "permission" in (
            detail_message.lower()
        ):
            return summarize(
                "elevenlabs", pending, "ELEVENLABS_OK_NO_QUOTA",
                "key authenticates, but it lacks the User permission, so the quota cannot be "
                "read; grant User access on the key to track it",
            )
        return summarize("elevenlabs", [(
            "critical", "ELEVENLABS_AUTH_FAILED",
            f"ElevenLabs rejected the API key the launcher uses (HTTP {status}"
            f"{', ' + detail_status if detail_status else ''})",
        )] + pending, "", "")
    return CheckResult("elevenlabs", False, "ELEVENLABS_UNAVAILABLE",
                       f"ElevenLabs answered HTTP {status}", severity="warning")


def check_render(config: Config, ctx: Context) -> CheckResult:
    if not ctx.ffmpeg or not ctx.ffprobe:
        missing = " and ".join(n for n, p in (("ffmpeg", ctx.ffmpeg), ("ffprobe", ctx.ffprobe))
                               if not p)
        where = "the running launcher's PATH" if ctx.process else "PATH"
        return CheckResult("render", False, "RENDER_FFMPEG_MISSING",
                           f"{missing} not found on {where}: no video can be rendered")
    problems: List[Problem] = []
    with tempfile.TemporaryDirectory(prefix="panda-health-") as tmp:
        out = Path(tmp) / "smoke.mp4"
        rc, _stdout, stderr, timed_out = _run([
            ctx.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=24:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", "1", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-y", str(out),
        ], cwd=Path(tmp), timeout_s=60, env=ctx.tool_env)
        if rc != 0 or timed_out or not out.is_file():
            return CheckResult("render", False, "RENDER_FFMPEG_FAILED",
                               _redacted_tail(stderr or f"ffmpeg exit code {rc}"))
        rc, stdout, stderr, _timed_out = _run([
            ctx.ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(out),
        ], cwd=Path(tmp), timeout_s=30, env=ctx.tool_env)
        try:
            duration = float(stdout.strip())
        except ValueError:
            duration = -1.0
        if rc != 0 or not 0.5 <= duration <= 2.0:
            return CheckResult("render", False, "RENDER_PROBE_FAILED",
                               _redacted_tail(stderr or f"ffprobe read duration {stdout!r}"))
    metadata: Dict[str, Any] = {"ffmpeg": ctx.ffmpeg, "remotion_required": ctx.remotion_needed}
    if ctx.remotion_needed:
        node = _which("node", ctx.search_path)
        npx = _which("npx", ctx.search_path)
        major = _node_major(node, ctx.tool_env, config.repo) if node else None
        cli = config.repo / "remotion-composer" / "node_modules" / "@remotion" / "cli"
        if not node or not npx or major is None or major < MIN_NODE_MAJOR:
            problems.append((
                "warning", "RENDER_REMOTION_NOT_READY",
                f"screenshot jobs need Node {MIN_NODE_MAJOR}+ and npx; found Node "
                f"{major or 'none'}",
            ))
        elif not cli.exists():
            problems.append((
                "warning", "RENDER_REMOTION_NOT_READY",
                "remotion-composer is not installed (run npm ci in remotion-composer)",
            ))
    detail = "ffmpeg encoded and read back a 1 s test clip" + (
        "; Remotion ready" if ctx.remotion_needed else "")
    return summarize("render", problems, "RENDER_OK", detail, metadata)


def _existing_ancestor(path: Path) -> Optional[Path]:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def check_disk(config: Config, ctx: Context) -> CheckResult:
    volumes: Dict[int, Dict[str, Any]] = {}
    for label, path in (("jobs", ctx.data_dir), ("projects", ctx.projects_dir),
                        ("claude", Path.home() / ".claude"), ("repo", config.repo)):
        existing = _existing_ancestor(path.expanduser())
        if existing is None:
            continue
        try:
            device = existing.stat().st_dev
            if device in volumes:
                volumes[device]["labels"].append(label)
                continue
            usage = shutil.disk_usage(str(existing))
        except OSError:
            continue
        volumes[device] = {
            "labels": [label],
            "path": str(existing),
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "free_pct": round(100.0 * usage.free / usage.total, 1) if usage.total else 0.0,
        }
    if not volumes:
        return CheckResult("disk", False, "DISK_UNKNOWN", "could not read any volume",
                           severity="warning")
    problems: List[Problem] = []
    # Free space is useless if the launcher cannot create a job folder. It mkdirs with
    # parents=True, so the nearest existing folder must be writable (nothing is written here).
    for code, label, target in (("JOBS_DIR_NOT_WRITABLE", "jobs", ctx.data_dir / "jobs"),
                                ("PROJECTS_DIR_NOT_WRITABLE", "projects", ctx.projects_dir)):
        existing = _existing_ancestor(target.expanduser())
        if (existing is None or not existing.is_dir()
                or not os.access(str(existing), os.W_OK | os.X_OK)):
            problems.append(("critical", code,
                             f"{label} folder {target} is not writable by this user (at "
                             f"{existing}); every new job would fail"))
    lines = []
    for volume in volumes.values():
        where = f"{'/'.join(volume['labels'])} ({volume['path']})"
        free = f"{volume['free_gb']:g} GB free ({volume['free_pct']:g}%)"
        lines.append(f"{where}: {free}")
        if volume["free_gb"] < 1 or volume["free_pct"] < 3:
            problems.append(("critical", "DISK_CRITICAL",
                             f"{where}: only {free}; renders and checkpoints will fail"))
        elif ((config.disk_min_free_gb is not None
               and volume["free_gb"] < config.disk_min_free_gb)
              or (config.disk_min_free_pct is not None
                  and volume["free_pct"] < config.disk_min_free_pct)):
            problems.append(("warning", "DISK_LOW", f"{where}: {free}"))
    return summarize("disk", problems, "DISK_OK", "; ".join(lines),
                     {"volumes": list(volumes.values())})


def check_dify(config: Config, ctx: Context) -> CheckResult:
    targets: List[Tuple[str, str]] = []
    if ctx.dify_files_base:
        targets.append(("Dify file server", ctx.dify_files_base))
    targets.extend(config.extra_urls)
    if not targets:
        return CheckResult(
            "dify", True, "DIFY_NOT_CONFIGURED",
            "not tested: DIFY_FILES_BASE and PANDA_HEALTH_EXTRA_URLS are empty",
            severity="info",
        )
    problems: List[Problem] = []
    reached: List[str] = []
    for name, url in targets:
        host = _host_of(url)
        try:
            status, _raw = http_get(url, timeout=15)
        except NET_ERRORS as exc:
            problems.append(("warning", "DIFY_UNREACHABLE",
                             f"{name} ({host}): {_safe_error(exc)}"))
            continue
        if status >= 500:
            problems.append(("warning", "DIFY_SERVER_ERROR",
                             f"{name} ({host}) answered HTTP {status}"))
        else:
            reached.append(f"{name} ({host}) HTTP {status}")
    return summarize("dify", problems, "DIFY_OK", "reachable: " + ", ".join(reached))


# ---------------------------------------------------------------------------
# State, alerts and the run
# ---------------------------------------------------------------------------

def read_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def notification_kind(
    previous: Dict[str, Any], healthy: bool, now: datetime, reminder_hours: int,
    notify_success: bool, failed_codes: Optional[Sequence[str]] = None,
) -> Optional[str]:
    previous_healthy = previous.get("healthy")
    if not healthy:
        if previous_healthy is not False:
            return "failure"
        previous_codes = previous.get("failed_codes")
        if (failed_codes is not None and isinstance(previous_codes, list)
                and sorted(previous_codes) != sorted(failed_codes)):
            return "changed"
        last_alert = _parse_ts(previous.get("last_alerted_at"))
        due = reminder_hours * 3600 - REMINDER_SLACK_S
        if not last_alert or (now - last_alert).total_seconds() >= due:
            return "reminder"
        return None
    if previous_healthy is False:
        return "recovery"
    if notify_success:
        return "success"
    return None


_ACTIONS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("CLAUDE_LOGGED_OUT", "CLAUDE_OAUTH_EXPIRED"),
     "Run `claude auth login` as this Unix user, then rerun the check."),
    (("CLAUDE_USAGE_LIMIT",),
     "The Claude subscription is at its usage limit; jobs stall until it resets."),
    (("CLAUDE_BINARY_MISSING", "HEALTHCHECK_CLAUDE_DIFFERS"),
     "Remove PANDA_HEALTH_CLAUDE_BIN from ~/.config/panda-healthcheck.env (an older install "
     "pinned a Node path) so the check uses the launcher's claude."),
    (("CLAUDE_AUTH_CHECK_FAILED", "CLAUDE_AUTH_TIMEOUT"),
     "Run `claude auth status --json` as this user and fix the error it prints."),
    (("CLAUDE_TIMEOUT", "CLAUDE_OVERLOADED", "CLAUDE_RATE_LIMITED", "CLAUDE_API_UNREACHABLE",
      "CLAUDE_CANARY_STOPPED", "HIGGSFIELD_UNAVAILABLE"),
     "Likely a temporary Claude or Higgsfield incident; if it repeats tomorrow, run "
     "`claude -p hello` as this user and check status.anthropic.com."),
    (("HIGGSFIELD_AUTH_FAILED", "HIGGSFIELD_NOT_DISCOVERED"),
     "Reconnect Higgsfield in the Claude.ai connector settings, then rerun the check."),
    (("HIGGSFIELD_LOW_CREDITS",), "Top up Higgsfield credits."),
    (("LAUNCHER_DOWN", "LAUNCHER_UNHEALTHY"),
     "Inspect ~/launcher.log; start the launcher with the deploy/README.md start block."),
    (("LAUNCHER_WRONG_RUNNER", "LAUNCHER_LOOPBACK_ONLY", "LAUNCHER_CLAUDE_MISSING",
      "LEGS_API_KEY_MODE"),
     "Restart the launcher with the whole deploy/README.md start block (it sources .env, "
     "binds 0.0.0.0 and puts Node 22 and claude on PATH). Never copy flags from `ps`."),
    (("LAUNCHER_CODE_STALE", "LAUNCHER_RESTART_PENDING"),
     "Restart the launcher when no job is running so it serves the pulled code."),
    (("NEXT_RESTART_API_KEY_MODE", "NEXT_RESTART_WRONG_RUNNER"),
     "Fix the launcher's .env before the next restart (ANTHROPIC_* blank, "
     "DIFY_RUNNER=claude)."),
    (("LAUNCHER_ENV_UNREADABLE",),
     "Make the launcher's .env readable by this user, or set PANDA_HEALTH_LAUNCHER_ENV_FILE."),
    (("LAUNCHER_PRIVATE_UNREACHABLE", "LAUNCHER_PRIVATE_UNHEALTHY",
      "LAUNCHER_REACH_OTHER_PROCESS"),
     "Check which process listens on port 8501 (`sudo ss -lntp | grep 8501`); there should "
     "be one launcher, bound to 0.0.0.0."),
    (("PUBLIC_DNS_FAILED", "PUBLIC_TLS_FAILED", "PUBLIC_TIMEOUT", "PUBLIC_UNREACHABLE",
      "PUBLIC_HTTP_ERROR", "PUBLIC_URL_OTHER_LAUNCHER", "PUBLIC_WRONG_RUNNER"),
     "Dify cannot reach this launcher: check DNS, the reverse proxy and the security group."),
    (("PUBLIC_TLS_EXPIRING",), "Renew the TLS certificate for the public URL."),
    (("ELEVENLABS_KEY_MISSING", "ELEVENLABS_AUTH_FAILED"),
     "Put a valid ELEVENLABS_API_KEY in the launcher's .env, then restart the launcher."),
    (("ELEVENLABS_UNREACHABLE", "ELEVENLABS_UNAVAILABLE"),
     "ElevenLabs did not answer; if it repeats, check status.elevenlabs.io and this box's "
     "outbound network."),
    (("NEXT_RESTART_ELEVENLABS_KEY",),
     "Restart the launcher when no job is running so it picks up the new ElevenLabs key."),
    (("ELEVENLABS_LOW_CHARACTERS", "ELEVENLABS_BILLING"),
     "Top up or fix billing on the ElevenLabs account."),
    (("RENDER_FFMPEG_MISSING", "RENDER_FFMPEG_FAILED", "RENDER_PROBE_FAILED"),
     "Repair ffmpeg/ffprobe: on Amazon Linux reinstall the static build in /usr/local/bin "
     "(there is no dnf package); on Debian `sudo apt-get install ffmpeg`. If FFMPEG_BIN or "
     "FFPROBE_BIN is set in .env, point it at a working binary."),
    (("RENDER_REMOTION_NOT_READY",),
     "Run `npm ci && npx remotion browser ensure` in remotion-composer with Node 22."),
    (("DISK_LOW", "DISK_CRITICAL"),
     "Free disk space: old job folders, renders and ~/.claude transcripts."),
    (("JOBS_DIR_NOT_WRITABLE", "PROJECTS_DIR_NOT_WRITABLE"),
     "Give this user write access to the jobs/projects folder (check DIFY_DATA_DIR and "
     "OPENMONTAGE_PROJECTS_DIR in .env)."),
    (("DIFY_UNREACHABLE", "DIFY_SERVER_ERROR"),
     "Check that the Dify server is up and reachable from this box."),
)


def build_alert(
    kind: str, results: Sequence[CheckResult], now: datetime, box_name: Optional[str] = None,
) -> Tuple[str, str]:
    healthy = all(result.ok for result in results)
    host = box_name or socket.gethostname()
    if kind == "recovery":
        title, marker = "Panda pipeline health recovered", "RECOVERED"
    elif healthy:
        title, marker = "Panda pipeline daily check passed", "OK"
    elif kind == "changed":
        title, marker = "Panda pipeline health changed, still failing", "CHANGED"
    elif kind == "reminder":
        title, marker = "Panda pipeline health still failing", "STILL FAILING"
    else:
        title, marker = "Panda pipeline daily check failed", "FAILED"
    title = f"{title} ({host})"
    lines = [title, f"Status: {marker}", f"Host: {host}", f"Time: {now.isoformat()}", ""]
    for result in results:
        symbol = "OK" if result.ok else result.severity.upper()
        lines.append(f"{result.component}: {symbol} - {result.code} - {result.detail}")
    failed = {code for result in results if not result.ok for code in result_codes(result)}
    actions = [action for codes, action in _ACTIONS if failed.intersection(codes)]
    mapped = {code for codes, _action in _ACTIONS for code in codes}
    # HIGGSFIELD_NOT_TESTED only follows a claude_auth failure, whose action already applies.
    unmapped = failed - mapped - {"HIGGSFIELD_NOT_TESTED"}
    if failed:
        lines.extend(["", "Suggested action:"])
        lines.extend(f"- {action}" for action in actions)
        if unmapped:
            lines.append(f"- {', '.join(sorted(unmapped))}: see the detail lines above and "
                         "~/panda-healthcheck.log.")
    return title, "\n".join(lines)


def send_webhook(url: str, kind: str, message: str) -> None:
    if kind == "discord":
        payload = {"content": message[:2000]}
    elif kind in {"slack", "generic"}:
        payload = {"text": message}
    else:
        raise ValueError("PANDA_HEALTH_WEBHOOK_KIND must be slack, discord, or generic")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")


def send_sns(topic_arn: str, region: Optional[str], subject: str, message: str) -> None:
    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise RuntimeError("boto3 is required for SNS alerts") from exc
    client = boto3.client("sns", region_name=region)
    client.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message)


def send_alerts(config: Config, subject: str, message: str) -> Tuple[List[str], int]:
    errors: List[str] = []
    deliveries = 0
    if config.sns_topic_arn:
        try:
            send_sns(config.sns_topic_arn, config.aws_region, subject, message)
            deliveries += 1
        except Exception as exc:  # alert failures must remain visible in cron output
            errors.append(f"SNS alert failed: {_safe_error(exc)}")
    if config.webhook_url:
        try:
            send_webhook(config.webhook_url, config.webhook_kind, message)
            deliveries += 1
        except Exception as exc:
            errors.append(f"webhook alert failed: {_safe_error(exc)}")
    if not config.sns_topic_arn and not config.webhook_url:
        errors.append("no alert destination configured; failure is visible only in cron output")
    return errors, deliveries


def send_test_alert(config: Config) -> int:
    subject = f"Panda health check: test alert ({config.box_name})"
    message = (f"{subject}\nTime: {utc_now().isoformat()}\n"
               "If you can read this, this box's health alerts reach you.")
    errors, deliveries = send_alerts(config, subject, message)
    print(json.dumps({"test_alert": True, "alert_deliveries": deliveries,
                      "alert_errors": errors}, sort_keys=True))
    return 0 if deliveries and not errors else 1


def _safe_error(exc: BaseException) -> str:
    return _redacted_tail(f"{type(exc).__name__}: {exc}")


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _redacted_tail(text: str, limit: int = 500) -> str:
    """Keep alerts useful without copying large transcripts or likely credentials."""
    compact = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    compact = _SECRET_RE.sub(
        lambda match: (match.group(1) or "") + "[REDACTED]", compact
    )
    return compact[-limit:] or "unknown error"


def run(
    config: Config, *, no_alert: bool = False, skip: Iterable[str] = (),
    proc_root: Path = Path("/proc"),
) -> int:
    started = time.monotonic()
    now = utc_now()
    skipped = set(skip)
    ctx = gather_context(config, proc_root)
    results: List[CheckResult] = []
    local_body: Optional[Dict[str, Any]] = None

    def add(component: str, check: Callable[[], CheckResult]) -> CheckResult:
        if component in skipped:
            result = CheckResult(component, True, "SKIPPED", "skipped by --skip",
                                 severity="info")
        else:
            try:
                result = check()
            except Exception as exc:  # noqa: BLE001 — one broken check must not hide the rest
                result = CheckResult(component, False, "CHECK_CRASHED", _safe_error(exc),
                                     severity="warning")
        results.append(result)
        return result

    def launcher() -> CheckResult:
        nonlocal local_body
        result, local_body = check_launcher(config, ctx)
        return result

    add("launcher", launcher)
    add("launcher_reach", lambda: check_launcher_reach(config, ctx, local_body))
    add("public_url", lambda: check_public_url(config, local_body))
    add("launcher_env", lambda: check_launcher_env(config, ctx, local_body))
    auth = add("claude_auth", lambda: check_claude_auth(config, ctx))
    if auth.ok or auth.code == "SKIPPED":
        add("higgsfield_mcp", lambda: check_higgsfield(config, ctx))
    else:
        results.append(CheckResult(
            "higgsfield_mcp", False, "HIGGSFIELD_NOT_TESTED",
            "skipped because Claude authentication check failed",
        ))
    add("elevenlabs", lambda: check_elevenlabs(config, ctx))
    add("render", lambda: check_render(config, ctx))
    add("disk", lambda: check_disk(config, ctx))
    add("dify", lambda: check_dify(config, ctx))

    healthy = all(result.ok for result in results)
    failed_codes = sorted({
        code for result in results if not result.ok for code in result_codes(result)
    })
    previous = read_state(config.state_file)
    # A --skip run is a manual test: it neither alerts nor moves the alert state, so a skipped
    # layer that is still down cannot be announced as "recovered".
    partial = bool(skipped)
    kind = None if partial else notification_kind(
        previous, healthy, now, config.reminder_hours, config.notify_success,
        failed_codes=failed_codes,
    )
    alert_errors: List[str] = []
    alert_deliveries = 0
    if kind and not no_alert:
        subject, message = build_alert(kind, results, now, config.box_name)
        alert_errors, alert_deliveries = send_alerts(config, subject, message)

    first_failed_at = previous.get("first_failed_at")
    if not healthy and previous.get("healthy") is not False:
        first_failed_at = now.isoformat()
    if healthy:
        first_failed_at = None
    state = {
        "healthy": healthy,
        "checked_at": now.isoformat(),
        "first_failed_at": first_failed_at,
        "last_alerted_at": (
            now.isoformat() if alert_deliveries else previous.get("last_alerted_at")
        ),
        "codes": [result.code for result in results],
        "failed_codes": failed_codes,
    }
    if not partial:
        write_state(config.state_file, state)

    summary = {
        "healthy": healthy,
        "partial": partial,
        "box": config.box_name,
        "checked_at": now.isoformat(),
        "duration_s": round(time.monotonic() - started, 1),
        "notification": None if no_alert else kind,
        "alert_deliveries": alert_deliveries,
        "results": [asdict(result) for result in results],
        "alert_errors": alert_errors,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    for error in alert_errors:
        print(f"warning: {error}", file=sys.stderr)
    return 0 if healthy else 1


def parse_skip(raw: str) -> List[str]:
    names = [name.strip() for name in raw.split(",") if name.strip()]
    unknown = sorted(set(names) - set(COMPONENTS))
    if unknown:
        raise ValueError(f"unknown component(s) for --skip: {', '.join(unknown)}; "
                         f"choose from {', '.join(COMPONENTS)}")
    return names


# ---------------------------------------------------------------------------
# Settings and the daily schedule (one file: no installer, no separate config)
# ---------------------------------------------------------------------------

DEFAULT_ENV_FILE = Path("~/.config/panda-healthcheck.env").expanduser()
DEFAULT_SCHEDULE = "30 2"            # 02:30 UTC = 08:00 IST
CRON_MARKER = "panda_healthcheck.py"     # also matches a pre-installer line


def load_settings(env_file: Path) -> None:
    """Process env wins, then the optional private file, then PANDA_HEALTH_* in the launcher's .env.

    Keeping the settings in the .env the launcher already loads means the whole check is this
    one file. The PANDA_HEALTH_* keys do not change the launcher, so editing them needs no
    restart.
    """
    load_env_file(env_file)
    repo = Path(os.environ.get("PANDA_HEALTH_REPO", str(DEFAULT_REPO))).expanduser()
    launcher_env = Path(
        os.environ.get("PANDA_HEALTH_LAUNCHER_ENV_FILE", "").strip() or str(repo / ".env")
    ).expanduser()
    try:
        parsed = parse_env_text(launcher_env.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return
    for key, value in parsed.items():
        if key.startswith("PANDA_HEALTH_") or key == "AWS_REGION":
            os.environ.setdefault(key, value)


def _crontab_read() -> str:
    """The user's crontab. Bytes that are not UTF-8 survive the round trip unchanged."""
    proc = subprocess.run(["crontab", "-l"], capture_output=True, timeout=15, check=False)
    stdout = proc.stdout.decode("utf-8", "surrogateescape")
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode == 0:
        return stdout
    if "no crontab" in (stderr + stdout).lower():
        return ""
    raise OSError("could not read the current crontab, so it was not changed: "
                  + _redacted_tail(stderr or f"exit code {proc.returncode}"))


def _crontab_write(text: str) -> None:
    proc = subprocess.run(["crontab", "-"], input=text.encode("utf-8", "surrogateescape"),
                          capture_output=True, timeout=15, check=False)
    if proc.returncode != 0:
        raise OSError("crontab rejected the update: " + _redacted_tail(
            proc.stderr.decode("utf-8", "replace") or f"exit code {proc.returncode}"))


def _fallback_path() -> str:
    """cron's PATH: the start block's order (Node 22 from nvm, user-local claude, ffmpeg)."""
    def version(path: Path) -> Tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", path.parent.name))

    home = Path.home()
    node22 = sorted(home.glob(".nvm/versions/node/v22*/bin"), key=version)
    parts = [str(node22[-1])] if node22 else []
    parts += [str(home / ".npm-global" / "bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(parts)


def _system_python() -> str:
    """The OS python3, not a venv one: a venv can vanish, and only the OS one has boto3."""
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.access(candidate, os.X_OK):
            return candidate
    return sys.executable


def cron_line(schedule: str, env_file: Path, state_dir: Path) -> str:
    script = Path(__file__).resolve()
    log = Path.home() / "panda-healthcheck.log"
    flock = shutil.which("flock", path="/usr/bin:/bin:/usr/local/bin") or shutil.which("flock")
    # The lock lives in this user's own state folder: a /tmp path could be taken by any account.
    lock = f"{flock} -n {state_dir / 'run.lock'} " if flock else ""
    extra = "" if env_file == DEFAULT_ENV_FILE else f" --env-file {env_file}"
    return (f"{schedule} * * * PATH={_fallback_path()} {lock}{_system_python()} {script}{extra}"
            f" >> {log} 2>&1")


def install_schedule(config: Config, schedule: Optional[str], env_file: Path) -> int:
    """Add (or with schedule=None remove) this check's own crontab line; nothing else changes."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("install the schedule as the user that owns the Claude login, not root")
    if schedule is not None:
        match = re.fullmatch(r"(\d{1,2}) (\d{1,2})", schedule.strip())
        if not match or int(match.group(1)) > 59 or int(match.group(2)) > 23:
            raise ValueError('the schedule must be "MINUTE HOUR" in UTC, e.g. "30 2"')
        schedule = f"{int(match.group(1))} {int(match.group(2))}"
    for path in (Path(__file__).resolve(), Path.home(), Path(_system_python()),
                 config.state_file.parent):
        if " " in str(path):
            raise ValueError(f"a cron line cannot hold a path with spaces: {path}")
    current = _crontab_read()
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    backup = config.state_file.parent / (
        "crontab.before." + utc_now().strftime("%Y%m%dT%H%M%SZ"))
    backup.write_text(current, encoding="utf-8", errors="surrogateescape")
    os.chmod(backup, 0o600)
    kept = [line for line in current.splitlines() if CRON_MARKER not in line]
    line = None if schedule is None else cron_line(schedule, env_file,
                                                   config.state_file.parent)
    if line:
        kept.append(line)
    _crontab_write("\n".join(kept) + "\n" if kept else "")
    print(json.dumps({
        "installed": line,
        "replaced_previous_line": any(CRON_MARKER in old for old in current.splitlines()),
        "crontab_backup": str(backup),
    }, indent=2))
    if line:
        print("\nNext: put PANDA_HEALTH_WEBHOOK_URL (or PANDA_HEALTH_SNS_TOPIC_ARN), "
              "PANDA_HEALTH_PUBLIC_URL and PANDA_HEALTH_BOX_NAME in "
              f"{config.launcher_env_file}, then run:\n"
              f"  {_system_python()} {Path(__file__).resolve()} --test-alert\n"
              f"  {_system_python()} {Path(__file__).resolve()} --no-alert")
        _warn_about_old_settings(env_file)
    return 0


def _warn_about_old_settings(env_file: Path) -> None:
    """The first canary's template set values that now fight the new defaults."""
    try:
        old = parse_env_text(env_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return
    stale = []
    if old.get("PANDA_HEALTH_CLAUDE_BIN"):
        stale.append("PANDA_HEALTH_CLAUDE_BIN (tests a pinned claude, not the launcher's)")
    if old.get("PANDA_HEALTH_NOTIFY_SUCCESS", "").strip().lower() in {"false", "0", "no", "off"}:
        stale.append("PANDA_HEALTH_NOTIFY_SUCCESS=false (no daily OK message)")
    if stale:
        print(f"\nNote: {env_file} still sets {'; '.join(stale)}. That file wins over the "
              "launcher's .env; delete those lines, or the file once its settings are in .env.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env-file", type=Path, default=DEFAULT_ENV_FILE,
        help="optional private KEY=VALUE settings that win over the launcher's .env "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--install", nargs="?", const=DEFAULT_SCHEDULE, metavar='"MINUTE HOUR"',
        help="run this check every day from the user's crontab at MINUTE HOUR UTC "
             f'(default "{DEFAULT_SCHEDULE}", 08:00 IST), then exit',
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="remove this check's crontab line, then exit",
    )
    parser.add_argument(
        "--no-alert", action="store_true",
        help="run checks and update state without publishing notifications",
    )
    parser.add_argument(
        "--skip", default="",
        help="comma-separated components to skip, e.g. higgsfield_mcp to test without "
             "the Claude turn; a skipped run neither alerts nor updates the saved state",
    )
    parser.add_argument(
        "--test-alert", action="store_true",
        help="send one test notification to the configured destinations, then exit",
    )
    args = parser.parse_args(argv)
    try:
        env_file = args.env_file.expanduser()
        load_settings(env_file)
        config = config_from_env()
        if args.install is not None or args.uninstall:
            return install_schedule(config, None if args.uninstall else args.install, env_file)
        if args.test_alert:
            return send_test_alert(config)
        return run(config, no_alert=args.no_alert, skip=parse_skip(args.skip))
    except (ValueError, OSError) as exc:
        detail = _safe_error(exc)
        print(json.dumps({
            "healthy": False,
            "code": "HEALTHCHECK_CONFIGURATION_ERROR",
            "detail": detail,
        }), file=sys.stderr)
        webhook = os.environ.get("PANDA_HEALTH_WEBHOOK_URL", "").strip()
        if webhook and args.install is None and not args.uninstall:
            try:   # best effort: one broken setting must not silence every alert
                send_webhook(
                    webhook, os.environ.get("PANDA_HEALTH_WEBHOOK_KIND", "slack").strip().lower(),
                    f"Panda health check cannot run ({socket.gethostname()}): {detail}",
                )
            except Exception:  # noqa: BLE001 — the stderr line above is the fallback
                pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
