#!/usr/bin/env python3
"""Daily, zero-generation health canary for the Panda launcher stack.

The check deliberately exercises the real Claude.ai -> Higgsfield connector path:

1. GET the local launcher's /health endpoint.
2. Inspect ``claude auth status --json``.
3. Run one small Claude Haiku turn that discovers and calls Higgsfield ``balance``.
4. Verify the actual MCP tool-use/result records (never trust the model's prose alone).

No generation tool is allowed, Claude session persistence and auto-memory are disabled,
and the subprocess has bounded turns, budget, and wall time.  On a state transition the
script can publish to AWS SNS and/or a Slack/Discord webhook.  It always emits one JSON
summary to stdout and exits non-zero when unhealthy, making it suitable for cron.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BALANCE_TOOL = "mcp__claude_ai_Higgsfield__balance"
DEFAULT_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CLAUDE = Path(
    "/home/ec2-user/.nvm/versions/node/v22.23.2/bin/claude"
)


@dataclass
class CheckResult:
    component: str
    ok: bool
    code: str
    detail: str
    severity: str = "critical"
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class Config:
    repo: Path
    claude_bin: Path
    launcher_url: str
    claude_model: str
    claude_timeout_s: int
    claude_max_budget_usd: float
    retry_count: int
    retry_delay_s: int
    low_credits: Optional[float]
    state_file: Path
    reminder_hours: int
    webhook_url: str
    webhook_kind: str
    sns_topic_arn: str
    aws_region: Optional[str]
    notify_success: bool


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def load_env_file(path: Path) -> None:
    """Load a small KEY=VALUE file without executing shell code.

    Existing process variables win. Quotes around a whole value are removed. This is
    intentionally a minimal parser for the documented health-check settings, not a full
    dotenv implementation.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def config_from_env() -> Config:
    low_raw = os.environ.get("PANDA_HEALTH_LOW_CREDITS", "500").strip()
    low_credits = None if low_raw.lower() in {"", "off", "none"} else float(low_raw)
    return Config(
        repo=Path(os.environ.get("PANDA_HEALTH_REPO", str(DEFAULT_REPO))).expanduser(),
        claude_bin=Path(
            os.environ.get("PANDA_HEALTH_CLAUDE_BIN", str(DEFAULT_CLAUDE))
        ).expanduser(),
        launcher_url=os.environ.get(
            "PANDA_HEALTH_LAUNCHER_URL", "http://127.0.0.1:8501/health"
        ),
        claude_model=os.environ.get("PANDA_HEALTH_CLAUDE_MODEL", "haiku"),
        claude_timeout_s=_env_int("PANDA_HEALTH_CLAUDE_TIMEOUT_S", 90, 10),
        claude_max_budget_usd=_env_float(
            "PANDA_HEALTH_MAX_BUDGET_USD", 0.10, 0.01
        ),
        retry_count=_env_int("PANDA_HEALTH_RETRY_COUNT", 1, 0),
        retry_delay_s=_env_int("PANDA_HEALTH_RETRY_DELAY_S", 60, 0),
        low_credits=low_credits,
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
        notify_success=_env_bool("PANDA_HEALTH_NOTIFY_SUCCESS", False),
    )


def check_launcher(config: Config) -> CheckResult:
    request = urllib.request.Request(
        config.launcher_url,
        headers={"User-Agent": "panda-healthcheck/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            body = json.loads(response.read(64 * 1024).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return CheckResult("launcher", False, "LAUNCHER_DOWN", _safe_error(exc))
    if status != 200 or body.get("status") != "ok":
        return CheckResult(
            "launcher", False, "LAUNCHER_UNHEALTHY",
            f"HTTP {status}; status={body.get('status')!r}",
        )
    return CheckResult(
        "launcher", True, "LAUNCHER_OK", "launcher /health returned ok",
        severity="info",
        metadata={
            "runner": body.get("runner"),
            "async": body.get("async"),
            "montage_door": body.get("montage_door"),
        },
    )


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


def check_claude_auth(config: Config) -> CheckResult:
    if not config.claude_bin.is_file():
        return CheckResult(
            "claude_auth", False, "CLAUDE_BINARY_MISSING",
            f"Claude binary not found at {config.claude_bin}",
        )
    rc, stdout, stderr, timed_out = _run(
        [str(config.claude_bin), "auth", "status", "--json"],
        cwd=config.repo, timeout_s=15,
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
    if rc != 0 or not data.get("loggedIn"):
        return CheckResult(
            "claude_auth", False, "CLAUDE_LOGGED_OUT",
            "Claude Code reports that no authenticated account is available",
        )
    return CheckResult(
        "claude_auth", True, "CLAUDE_AUTH_PRESENT",
        f"authenticated via {data.get('authMethod') or 'unknown method'}",
        severity="info",
        metadata={
            "auth_method": data.get("authMethod"),
            "api_provider": data.get("apiProvider"),
            "subscription_type": data.get("subscriptionType"),
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
        parsed: Any = None
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = None
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

    combined = (stdout + "\n" + stderr).lower()
    if any(text in combined for text in (
        "oauth session expired", "failed to authenticate", "authentication_error",
    )):
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_OAUTH_EXPIRED",
            "Claude live request could not authenticate or refresh OAuth",
        )
    if timed_out:
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_TIMEOUT",
            "Claude/Higgsfield canary exceeded its wall-time limit", severity="warning",
        )
    if "529" in combined or "overloaded" in combined:
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_OVERLOADED",
            "Claude returned a temporary overload response", severity="warning",
        )
    if "429" in combined or "rate limit" in combined:
        return CheckResult(
            "higgsfield_mcp", False, "CLAUDE_RATE_LIMITED",
            "Claude returned a rate-limit response", severity="warning",
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


def check_higgsfield_once(config: Config) -> CheckResult:
    prompt = (
        "Health canary only. First find the Higgsfield balance tool, then call balance "
        "exactly once. Never call a generation, shell, file, web, or write tool. After a "
        "successful real balance result, reply exactly HIGGSFIELD_OK. On any tool error, "
        "reply HIGGSFIELD_FAIL and the error."
    )
    env = dict(os.environ)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    argv = [
        str(config.claude_bin), "-p", prompt,
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


def check_higgsfield(config: Config) -> CheckResult:
    attempts = config.retry_count + 1
    result = CheckResult(
        "higgsfield_mcp", False, "HIGGSFIELD_UNAVAILABLE", "not attempted"
    )
    no_retry = {"CLAUDE_OAUTH_EXPIRED", "HIGGSFIELD_AUTH_FAILED"}
    for attempt in range(attempts):
        result = check_higgsfield_once(config)
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
    notify_success: bool,
) -> Optional[str]:
    previous_healthy = previous.get("healthy")
    if not healthy:
        if previous_healthy is not False:
            return "failure"
        last_alert = _parse_ts(previous.get("last_alerted_at"))
        if not last_alert or (now - last_alert).total_seconds() >= reminder_hours * 3600:
            return "reminder"
        return None
    if previous_healthy is False:
        return "recovery"
    if notify_success:
        return "success"
    return None


def build_alert(kind: str, results: Sequence[CheckResult], now: datetime) -> Tuple[str, str]:
    healthy = all(result.ok for result in results)
    host = os.uname().nodename
    if kind == "recovery":
        title = "Panda pipeline health recovered"
        marker = "RECOVERED"
    elif healthy:
        title = "Panda pipeline morning check passed"
        marker = "OK"
    elif kind == "reminder":
        title = "Panda pipeline health still failing"
        marker = "STILL FAILING"
    else:
        title = "Panda pipeline morning check failed"
        marker = "FAILED"
    lines = [
        f"{title}", f"Status: {marker}", f"Host: {host}",
        f"Time: {now.isoformat()}", "",
    ]
    for result in results:
        symbol = "OK" if result.ok else result.severity.upper()
        lines.append(f"{result.component}: {symbol} — {result.code} — {result.detail}")
    failed_codes = {result.code for result in results if not result.ok}
    if failed_codes:
        lines.extend(["", "Suggested action:"])
        if failed_codes & {"CLAUDE_LOGGED_OUT", "CLAUDE_OAUTH_EXPIRED"}:
            lines.append("Run `claude auth login`, then rerun the checker manually.")
        elif failed_codes & {"HIGGSFIELD_AUTH_FAILED", "HIGGSFIELD_NOT_DISCOVERED"}:
            lines.append("Reconnect the Higgsfield Claude.ai connector, then rerun the checker.")
        elif "LAUNCHER_DOWN" in failed_codes or "LAUNCHER_UNHEALTHY" in failed_codes:
            lines.append("Inspect ~/launcher.log and the uvicorn process on port 8501.")
        else:
            lines.append("Inspect the cron log; this may be a temporary provider/network incident.")
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
        headers={"Content-Type": "application/json", "User-Agent": "panda-healthcheck/1.0"},
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
    for marker in ("sk-ant-", "sk-or-", "xoxb-", "xoxp-"):
        if marker in compact:
            before, _sep, _after = compact.partition(marker)
            compact = before + marker + "[REDACTED]"
    return compact[-limit:] or "unknown error"


def run(config: Config, *, no_alert: bool = False) -> int:
    now = utc_now()
    results = [check_launcher(config), check_claude_auth(config)]
    if results[-1].ok:
        results.append(check_higgsfield(config))
    else:
        results.append(CheckResult(
            "higgsfield_mcp", False, "HIGGSFIELD_NOT_TESTED",
            "skipped because Claude authentication check failed",
        ))

    healthy = all(result.ok for result in results)
    previous = read_state(config.state_file)
    kind = notification_kind(
        previous, healthy, now, config.reminder_hours, config.notify_success
    )
    alert_errors: List[str] = []
    alert_deliveries = 0
    if kind and not no_alert:
        subject, message = build_alert(kind, results, now)
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
    }
    write_state(config.state_file, state)

    summary = {
        "healthy": healthy,
        "checked_at": now.isoformat(),
        "notification": None if no_alert else kind,
        "alert_deliveries": alert_deliveries,
        "results": [asdict(result) for result in results],
        "alert_errors": alert_errors,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    for error in alert_errors:
        print(f"warning: {error}", file=sys.stderr)
    return 0 if healthy else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path,
        default=Path("~/.config/panda-healthcheck.env").expanduser(),
        help="optional KEY=VALUE configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--no-alert", action="store_true",
        help="run checks and update state without publishing notifications",
    )
    args = parser.parse_args(argv)
    try:
        load_env_file(args.env_file.expanduser())
        config = config_from_env()
        return run(config, no_alert=args.no_alert)
    except (ValueError, OSError) as exc:
        print(json.dumps({
            "healthy": False,
            "code": "HEALTHCHECK_CONFIGURATION_ERROR",
            "detail": _safe_error(exc),
        }), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
