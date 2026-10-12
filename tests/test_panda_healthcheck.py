"""Unit tests for the daily all-layers health check (no network, no real subprocesses)."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import socket
import sys
import urllib.error
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "panda_healthcheck.py"
SPEC = importlib.util.spec_from_file_location("panda_healthcheck", MODULE_PATH)
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = health
SPEC.loader.exec_module(health)


def _line(value):
    return json.dumps(value)


def test_parse_requires_real_balance_tool_result():
    stdout = "\n".join([
        _line({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "tool-1", "name": health.BALANCE_TOOL,
                "input": {},
            }]},
        }),
        _line({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-1",
                "content": '{"credits":5648,"subscription_plan_type":"ultra"}',
            }]},
        }),
        # The prose is intentionally irrelevant: the parser trusts the tool exchange.
        _line({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "anything"}]},
        }),
    ])
    result = health.parse_canary_stream(stdout, "", False)
    assert result.ok
    assert result.code == "HIGGSFIELD_OK"
    assert result.metadata["credits"] == 5648


def test_block_list_tool_result_is_accepted():
    """A tool_result may carry content as blocks rather than a bare string.

    Treating only the string shape as valid reported a healthy Higgsfield as
    unavailable, which alerted every morning on a working system.
    """
    stdout = "\n".join([
        _line({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "tool-3", "name": health.BALANCE_TOOL,
            }]},
        }),
        _line({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-3",
                "content": [{
                    "type": "text",
                    "text": '{"credits":5648,"subscription_plan_type":"ultra"}',
                }],
            }]},
        }),
    ])
    result = health.parse_canary_stream(stdout, "", False)
    assert result.ok
    assert result.code == "HIGGSFIELD_OK"
    assert result.metadata["credits"] == 5648


def test_block_list_prose_still_cannot_fake_success():
    """The block-list shape must not become a way to pass without real credits."""
    stdout = "\n".join([
        _line({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "tool-4", "name": health.BALANCE_TOOL,
            }]},
        }),
        _line({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-4",
                "content": [{"type": "text", "text": "HIGGSFIELD_OK"}],
            }]},
        }),
    ])
    result = health.parse_canary_stream(stdout, "", False)
    assert not result.ok


def test_prose_alone_cannot_fake_success():
    stdout = _line({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "HIGGSFIELD_OK"}]},
    })
    result = health.parse_canary_stream(stdout, "", False)
    assert not result.ok
    assert result.code == "HIGGSFIELD_NOT_DISCOVERED"


def test_claude_oauth_expiry_is_distinct_from_mcp_failure():
    result = health.parse_canary_stream(
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "", False,
    )
    assert not result.ok
    assert result.code == "CLAUDE_OAUTH_EXPIRED"


def test_mcp_auth_error_is_classified():
    stdout = "\n".join([
        _line({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "tool-2", "name": health.BALANCE_TOOL,
            }]},
        }),
        _line({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-2", "is_error": True,
                "content": "Connector authorization required; please login",
            }]},
        }),
    ])
    result = health.parse_canary_stream(stdout, "", False)
    assert not result.ok
    assert result.code == "HIGGSFIELD_AUTH_FAILED"


def test_notification_transitions_and_reminders():
    now = datetime(2026, 9, 9, 6, tzinfo=timezone.utc)
    assert health.notification_kind({}, False, now, 24, False) == "failure"
    assert health.notification_kind({"healthy": False, "last_alerted_at": now.isoformat()},
                                    False, now, 24, False) is None
    old = (now - timedelta(hours=25)).isoformat()
    assert health.notification_kind({"healthy": False, "last_alerted_at": old},
                                    False, now, 24, False) == "reminder"
    assert health.notification_kind({"healthy": False}, True, now, 24, False) == "recovery"
    assert health.notification_kind({"healthy": True}, True, now, 24, False) is None


# ---------------------------------------------------------------------------
# Shared fixtures for the all-layers checks
# ---------------------------------------------------------------------------

HEALTH_BODY = {
    "status": "ok", "runner": "claude", "async": True, "montage_door": True,
    "process_started_at": "2026-10-12T00:00:00+00:00", "build_revision": "a" * 40,
    "code_stale": False,
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("PANDA_HEALTH_") or key == "ELEVENLABS_API_KEY":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PANDA_HEALTH_REPO", str(tmp_path))
    monkeypatch.setenv("PANDA_HEALTH_STATE_FILE", str(tmp_path / "state" / "status.json"))
    monkeypatch.setenv("PANDA_HEALTH_BOX_NAME", "box-test")
    return health.config_from_env()


def make_ctx(tmp_path, **overrides):
    values = dict(
        process=None,
        dotenv=health.DotEnv(True, {"DIFY_RUNNER": "claude"},
                             {key: False for key in health.ANTHROPIC_OVERRIDE_KEYS},
                             elevenlabs_key="el-test-key"),
        search_path="",
        tool_env={},
        launcher_claude="/usr/local/bin/claude",
        canary_claude="/usr/local/bin/claude",
        canary_claude_source="launcher PATH",
        ffmpeg="/usr/local/bin/ffmpeg",
        ffprobe="/usr/local/bin/ffprobe",
        remotion_needed=False,
        data_dir=tmp_path,
        projects_dir=tmp_path,
        expected_revision="a" * 40,
        dify_files_base="",
    )
    values.update(overrides)
    return health.Context(**values)


def launcher_process(**overrides):
    values = dict(
        pid=4242, host="0.0.0.0", port=8501, env={"PATH": "/usr/bin"},
        overrides={key: False for key in health.ANTHROPIC_OVERRIDE_KEYS},
    )
    values.update(overrides)
    return health.LauncherProcess(**values)


def http_router(routes):
    """A stand-in for health.http_get: route by URL to (status, body) or an exception."""
    calls = []

    def fake(url, headers=None, timeout=10.0, use_proxy=True, max_bytes=0):
        calls.append({"url": url, "headers": dict(headers or {}), "use_proxy": use_proxy})
        answer = routes[url]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, raw

    fake.calls = calls
    return fake


Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Env files and the running launcher's environment
# ---------------------------------------------------------------------------

def test_parse_env_text_follows_shell_assignment_rules():
    text = "\n".join([
        "export A=1",
        'B="two words"',
        "C='x # y'",
        "D=val # trailing comment",
        "E=val#kept",
        "# F=commented",
        "G=",
        "A=2",
    ])
    assert health.parse_env_text(text) == {
        "A": "2", "B": "two words", "C": "x # y", "D": "val", "E": "val#kept", "G": "",
    }


def test_launcher_dotenv_keeps_override_presence_but_never_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=sk-ant-api03-SECRET\nELEVENLABS_API_KEY=el-SECRET\n"
        "DIFY_RUNNER=claude\nDIFY_TOKEN=tok-SECRET\nANTHROPIC_BASE_URL=\n",
        encoding="utf-8",
    )
    dotenv = health.read_launcher_dotenv(env_file)
    assert dotenv.found
    assert dotenv.values == {"DIFY_RUNNER": "claude"}
    assert dotenv.overrides["ANTHROPIC_API_KEY"] is True
    assert dotenv.overrides["ANTHROPIC_BASE_URL"] is False
    assert dotenv.elevenlabs_key == "el-SECRET"
    assert "SECRET" not in repr(dotenv)


def test_missing_launcher_dotenv_is_not_an_error(tmp_path):
    dotenv = health.read_launcher_dotenv(tmp_path / "absent.env")
    assert not dotenv.found and not dotenv.error


def test_uvicorn_bind_parses_both_flag_forms():
    assert health._uvicorn_bind(["--host=0.0.0.0", "--port=9000"]) == ("0.0.0.0", 9000)
    assert health._uvicorn_bind(["--host", "127.0.0.1", "--port", "8501"]) == ("127.0.0.1", 8501)
    assert health._uvicorn_bind([]) == ("127.0.0.1", 8000)


def test_find_launcher_process_reads_only_allowed_keys(tmp_path):
    def fake_proc(pid, argv, environ=b""):
        entry = tmp_path / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
        (entry / "environ").write_bytes(environ)

    fake_proc(50, [b"grep", b"dify_launcher.app"])                       # not uvicorn
    fake_proc(60, [b"python", b"-m", b"uvicorn", b"dify_launcher.app:app", b"--port", b"8601"])
    fake_proc(100, [b"python", b"-m", b"uvicorn", b"dify_launcher.app:app",
                    b"--host", b"127.0.0.1", b"--port", b"8501"],
              b"PATH=/opt/node22/bin:/usr/bin\0ANTHROPIC_API_KEY=sk-ant-x\0"
              b"DIFY_TOKEN=tok-SECRET\0CLAUDE_BIN=/opt/claude\0")
    (tmp_path / "self").mkdir()                                         # non-numeric entry

    process = health.find_launcher_process(8501, tmp_path)
    assert process is not None
    assert (process.pid, process.host, process.port) == (100, "127.0.0.1", 8501)
    assert process.env == {"PATH": "/opt/node22/bin:/usr/bin", "CLAUDE_BIN": "/opt/claude"}
    assert process.overrides["ANTHROPIC_API_KEY"] is True
    assert "SECRET" not in repr(process)
    assert health.find_launcher_process(8501, tmp_path / "missing") is None


# ---------------------------------------------------------------------------
# Launcher layers
# ---------------------------------------------------------------------------

def test_launcher_reports_mock_runner_stale_code_and_pending_restart(tmp_path, config, monkeypatch):
    body = dict(HEALTH_BODY, runner="mock", code_stale=True, build_revision="b" * 40)
    monkeypatch.setattr(health, "http_get", http_router({config.launcher_url: (200, body)}))
    result, local = health.check_launcher(config, make_ctx(tmp_path))
    assert not result.ok
    assert result.code == "LAUNCHER_WRONG_RUNNER"
    assert result.metadata["codes"] == [
        "LAUNCHER_WRONG_RUNNER", "LAUNCHER_CODE_STALE", "LAUNCHER_RESTART_PENDING",
    ]
    assert local["runner"] == "mock"


def test_launcher_healthy_and_down(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "http_get", http_router({config.launcher_url: (200, HEALTH_BODY)}))
    result, _ = health.check_launcher(config, make_ctx(tmp_path))
    assert result.ok and result.code == "LAUNCHER_OK"

    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(health, "http_get", http_router({config.launcher_url: refused}))
    result, local = health.check_launcher(config, make_ctx(tmp_path))
    assert result.code == "LAUNCHER_DOWN" and local is None


def test_reach_detects_a_loopback_only_bind(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "primary_ipv4", lambda: "10.0.10.177")
    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    router = http_router({"http://10.0.10.177:8501/health": refused})
    monkeypatch.setattr(health, "http_get", router)
    ctx = make_ctx(tmp_path, process=launcher_process(host="127.0.0.1"))
    result = health.check_launcher_reach(config, ctx, HEALTH_BODY)
    assert not result.ok
    assert result.code == "LAUNCHER_LOOPBACK_ONLY" and result.severity == "critical"
    assert router.calls[0]["use_proxy"] is False


def test_reach_passes_when_the_private_ip_serves_the_same_process(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "primary_ipv4", lambda: "10.0.10.177")
    monkeypatch.setattr(health, "http_get", http_router({
        "http://10.0.10.177:8501/health": (200, HEALTH_BODY),
    }))
    result = health.check_launcher_reach(config, make_ctx(tmp_path), HEALTH_BODY)
    assert result.ok and result.code == "LAUNCHER_REACHABLE"


def test_reach_is_not_tested_while_the_launcher_is_down(tmp_path, config):
    result = health.check_launcher_reach(config, make_ctx(tmp_path), None)
    assert result.ok and result.code == "REACH_NOT_TESTED"


def test_public_health_url_forms():
    assert health._public_health_url("https://dev.om.mvnoc.ai") == "https://dev.om.mvnoc.ai/health"
    assert health._public_health_url("https://x.ai/") == "https://x.ai/health"
    assert health._public_health_url("https://x.ai/panda/") == "https://x.ai/panda/health"
    assert health._public_health_url("https://x.ai/health?a=1") == "https://x.ai/health"


def test_public_url_not_configured_is_visible_but_not_a_failure(config):
    result = health.check_public_url(config, HEALTH_BODY)
    assert result.ok and result.code == "PUBLIC_URL_NOT_CONFIGURED"


def test_public_url_flags_other_launcher_and_expiring_tls(config, monkeypatch):
    config = dataclasses.replace(config, public_url="https://panda.example.com")
    other = dict(HEALTH_BODY, process_started_at="2026-10-01T00:00:00+00:00")
    monkeypatch.setattr(health, "http_get", http_router({
        "https://panda.example.com/health": (200, other),
    }))
    monkeypatch.setattr(health, "tls_days_left", lambda host, port=443, timeout=10.0: 5.0)
    result = health.check_public_url(config, HEALTH_BODY)
    assert not result.ok and result.severity == "warning"
    assert set(result.metadata["codes"]) == {"PUBLIC_URL_OTHER_LAUNCHER", "PUBLIC_TLS_EXPIRING"}
    assert result.metadata["tls_days_left"] == 5.0


def test_public_url_502_is_critical_after_one_retry(config, monkeypatch):
    config = dataclasses.replace(config, public_url="https://panda.example.com")
    router = http_router({"https://panda.example.com/health": (502, b"Bad Gateway")})
    monkeypatch.setattr(health, "http_get", router)
    monkeypatch.setattr(health.time, "sleep", lambda seconds: None)
    result = health.check_public_url(config, HEALTH_BODY)
    assert result.code == "PUBLIC_HTTP_ERROR" and result.severity == "critical"
    assert "proxy cannot reach the launcher" in result.detail
    assert len(router.calls) == 2


def test_public_url_dns_failure(config, monkeypatch):
    config = dataclasses.replace(config, public_url="https://panda.example.com")
    dns = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
    monkeypatch.setattr(health, "http_get", http_router({"https://panda.example.com/health": dns}))
    monkeypatch.setattr(health.time, "sleep", lambda seconds: None)
    assert health.check_public_url(config, HEALTH_BODY).code == "PUBLIC_DNS_FAILED"


def test_launcher_env_flags_api_key_mode_and_missing_claude(tmp_path, config):
    overrides = {key: key == "ANTHROPIC_API_KEY" for key in health.ANTHROPIC_OVERRIDE_KEYS}
    ctx = make_ctx(tmp_path, process=launcher_process(overrides=overrides),
                   launcher_claude=None, canary_claude=None)
    result = health.check_launcher_env(config, ctx, HEALTH_BODY)
    assert result.code == "LEGS_API_KEY_MODE" and result.severity == "critical"
    assert "LAUNCHER_CLAUDE_MISSING" in result.metadata["codes"]
    assert "ANTHROPIC_API_KEY" in result.detail


def test_launcher_env_warns_about_the_next_restart(tmp_path, config):
    dotenv = health.DotEnv(True, {}, {key: key == "ANTHROPIC_BASE_URL"
                                      for key in health.ANTHROPIC_OVERRIDE_KEYS})
    ctx = make_ctx(tmp_path, process=launcher_process(), dotenv=dotenv)
    result = health.check_launcher_env(config, ctx, HEALTH_BODY)
    assert result.severity == "warning"
    assert set(result.metadata["codes"]) == {"NEXT_RESTART_API_KEY_MODE",
                                             "NEXT_RESTART_WRONG_RUNNER"}


def test_launcher_env_warns_when_the_check_tests_another_claude(tmp_path, config):
    config = dataclasses.replace(config, claude_bin=Path("/home/u/.nvm/bin/claude"))
    ctx = make_ctx(tmp_path, process=launcher_process(),
                   canary_claude="/home/u/.nvm/bin/claude",
                   launcher_claude="/home/u/.npm-global/bin/claude")
    result = health.check_launcher_env(config, ctx, HEALTH_BODY)
    assert result.code == "HEALTHCHECK_CLAUDE_DIFFERS"


def test_launcher_env_ok(tmp_path, config):
    result = health.check_launcher_env(config, make_ctx(tmp_path, process=launcher_process()),
                                       HEALTH_BODY)
    assert result.ok and result.code == "LAUNCHER_ENV_OK"


# ---------------------------------------------------------------------------
# Claude canary classification
# ---------------------------------------------------------------------------

def test_usage_limit_is_classified_and_not_retried(tmp_path, config, monkeypatch):
    stdout = _line({"type": "result", "subtype": "success", "is_error": True,
                    "result": "Claude AI usage limit reached|1760000000"})
    assert health.parse_canary_stream(stdout, "", False).code == "CLAUDE_USAGE_LIMIT"

    calls = []

    def fake_once(cfg, ctx):
        calls.append(1)
        return health.parse_canary_stream(stdout, "", False)

    monkeypatch.setattr(health, "check_higgsfield_once", fake_once)
    config = dataclasses.replace(config, retry_count=2, retry_delay_s=0)
    assert health.check_higgsfield(config, make_ctx(tmp_path)).code == "CLAUDE_USAGE_LIMIT"
    assert len(calls) == 1


def test_canary_that_stops_early_is_not_reported_as_a_missing_connector():
    stdout = "\n".join([
        _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "…"}]}}),
        _line({"type": "result", "subtype": "error_max_turns", "is_error": True}),
    ])
    result = health.parse_canary_stream(stdout, "", False)
    assert result.code == "CLAUDE_CANARY_STOPPED" and "error_max_turns" in result.detail


def test_claude_auth_uses_the_resolved_binary_and_records_version(tmp_path, config, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    seen = []

    def fake_run(argv, *, cwd, timeout_s, env=None):
        seen.append(list(argv))
        if argv[1:3] == ["auth", "status"]:
            return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                  "subscriptionType": "max"}), "", False
        return 0, "2.1.197 (Claude Code)\n", "", False

    monkeypatch.setattr(health, "_run", fake_run)
    result = health.check_claude_auth(config, make_ctx(tmp_path, canary_claude=str(binary)))
    assert result.ok
    assert result.metadata["claude_version"] == "2.1.197"
    assert all(argv[0] == str(binary) for argv in seen)


def test_claude_auth_reports_a_missing_binary(tmp_path, config):
    result = health.check_claude_auth(config, make_ctx(tmp_path, canary_claude=None,
                                                       canary_claude_source="not found"))
    assert result.code == "CLAUDE_BINARY_MISSING"


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

def test_elevenlabs_low_quota_warns_and_never_echoes_the_key(tmp_path, config, monkeypatch):
    router = http_router({health.ELEVENLABS_SUBSCRIPTION_URL: (200, {
        "character_count": 99_000, "character_limit": 100_000, "status": "active",
        "tier": "creator", "next_character_count_reset_unix": 1_760_000_000,
    })})
    monkeypatch.setattr(health, "http_get", router)
    result = health.check_elevenlabs(config, make_ctx(tmp_path))
    assert result.code == "ELEVENLABS_LOW_CHARACTERS" and result.severity == "warning"
    assert result.metadata["characters_left"] == 1_000
    assert router.calls[0]["headers"]["xi-api-key"] == "el-test-key"
    assert "el-test-key" not in json.dumps(dataclasses.asdict(result))


def test_elevenlabs_key_without_user_permission_still_counts_as_valid(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "http_get", http_router({health.ELEVENLABS_SUBSCRIPTION_URL: (
        401, {"detail": {"status": "missing_permissions",
                         "message": "The API key you used is missing the permission "
                                    "user_read to execute this operation."}})}))
    result = health.check_elevenlabs(config, make_ctx(tmp_path))
    assert result.ok and result.code == "ELEVENLABS_OK_NO_QUOTA"


def test_elevenlabs_rejected_or_missing_key_is_critical(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "http_get", http_router({health.ELEVENLABS_SUBSCRIPTION_URL: (
        401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}})}))
    result = health.check_elevenlabs(config, make_ctx(tmp_path))
    assert result.code == "ELEVENLABS_AUTH_FAILED" and result.severity == "critical"

    no_key = health.DotEnv(True, {"DIFY_RUNNER": "claude"}, {})
    result = health.check_elevenlabs(config, make_ctx(tmp_path, dotenv=no_key))
    assert result.code == "ELEVENLABS_KEY_MISSING"


# ---------------------------------------------------------------------------
# Render toolchain, disk, Dify
# ---------------------------------------------------------------------------

def fake_render_run(node_version="v22.23.2", ffmpeg_rc=0, ffmpeg_err=""):
    def fake(argv, *, cwd, timeout_s, env=None):
        name = Path(argv[0]).name
        if name == "ffmpeg":
            if ffmpeg_rc == 0:
                Path(argv[-1]).write_bytes(b"\0" * 64)
            return ffmpeg_rc, "", ffmpeg_err, False
        if name == "ffprobe":
            return 0, "1.000000\n", "", False
        if name == "node":
            return 0, node_version + "\n", "", False
        raise AssertionError(f"unexpected command {argv}")
    return fake


def test_render_encodes_and_probes_a_test_clip(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "_run", fake_render_run())
    result = health.check_render(config, make_ctx(tmp_path))
    assert result.ok and result.code == "RENDER_OK"


def test_render_reports_a_broken_encoder(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "_run", fake_render_run(ffmpeg_rc=1,
                                                        ffmpeg_err="Unknown encoder 'libx264'"))
    result = health.check_render(config, make_ctx(tmp_path))
    assert result.code == "RENDER_FFMPEG_FAILED" and "libx264" in result.detail


def test_render_reports_missing_ffmpeg(tmp_path, config):
    result = health.check_render(config, make_ctx(tmp_path, ffmpeg=None))
    assert result.code == "RENDER_FFMPEG_MISSING"


def test_render_checks_remotion_only_when_screenshots_need_it(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health, "_run", fake_render_run(node_version="v18.20.8"))
    monkeypatch.setattr(health, "_which", lambda name, path: f"/fake/{name}")
    result = health.check_render(config, make_ctx(tmp_path, remotion_needed=True))
    assert result.code == "RENDER_REMOTION_NOT_READY" and "18" in result.detail

    monkeypatch.setattr(health, "_run", fake_render_run(node_version="v22.23.2"))
    (tmp_path / "remotion-composer" / "node_modules" / "@remotion" / "cli").mkdir(parents=True)
    result = health.check_render(config, make_ctx(tmp_path, remotion_needed=True))
    assert result.ok and "Remotion ready" in result.detail


def test_disk_thresholds(tmp_path, config, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage",
                        lambda path: Usage(100 * GB, 96 * GB, 4 * GB))
    result = health.check_disk(config, make_ctx(tmp_path))
    assert result.code == "DISK_LOW" and result.severity == "warning"

    monkeypatch.setattr(health.shutil, "disk_usage",
                        lambda path: Usage(1000 * GB, 980 * GB, 20 * GB))
    assert health.check_disk(config, make_ctx(tmp_path)).code == "DISK_CRITICAL"

    monkeypatch.setattr(health.shutil, "disk_usage",
                        lambda path: Usage(100 * GB, 50 * GB, 50 * GB))
    assert health.check_disk(config, make_ctx(tmp_path)).ok


def test_dify_check_reports_hosts_but_never_signed_paths(tmp_path, config, monkeypatch):
    assert health.check_dify(config, make_ctx(tmp_path)).code == "DIFY_NOT_CONFIGURED"
    url = "https://files.example.com/files/abc?sign=SECRET"
    dns = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
    monkeypatch.setattr(health, "http_get", http_router({url: dns}))
    result = health.check_dify(config, make_ctx(tmp_path, dify_files_base=url))
    assert result.code == "DIFY_UNREACHABLE"
    assert "files.example.com" in result.detail and "SECRET" not in result.detail


def test_extra_urls_parse_and_validate():
    assert health.parse_extra_urls("dify=https://dify.example.com/health, https://x.io") == [
        ("dify", "https://dify.example.com/health"), ("x.io", "https://x.io"),
    ]
    with pytest.raises(ValueError):
        health.parse_extra_urls("dify=ftp://nope")


# ---------------------------------------------------------------------------
# Alerts, state and the whole run
# ---------------------------------------------------------------------------

def test_changed_failures_alert_without_waiting_for_the_reminder():
    now = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)
    previous = {"healthy": False, "failed_codes": ["DISK_LOW"], "last_alerted_at": now.isoformat()}
    assert health.notification_kind(previous, False, now, 24, False,
                                    failed_codes=["DISK_LOW", "LAUNCHER_DOWN"]) == "changed"
    assert health.notification_kind(previous, False, now, 24, False,
                                    failed_codes=["DISK_LOW"]) is None


def test_daily_reminder_tolerates_a_slightly_early_cron_start():
    now = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)
    almost_a_day = (now - timedelta(hours=24) + timedelta(seconds=30)).isoformat()
    previous = {"healthy": False, "failed_codes": ["DISK_LOW"], "last_alerted_at": almost_a_day}
    assert health.notification_kind(previous, False, now, 24, False,
                                    failed_codes=["DISK_LOW"]) == "reminder"


def test_redaction_masks_tokens_but_keeps_ordinary_words():
    text = ("Authorization: Bearer abcdefghijklmnop0123 key sk-ant-api03-SECRETVALUE "
            "task_id=7 blob " + "A" * 45)
    redacted = health._redacted_tail(text)
    assert "SECRETVALUE" not in redacted and "abcdefghijklmnop0123" not in redacted
    assert "A" * 45 not in redacted
    assert "task_id=7" in redacted and "sk-ant-[REDACTED]" in redacted


def test_alert_names_the_box_and_suggests_each_fix():
    results = [
        health.CheckResult("launcher_reach", False, "LAUNCHER_LOOPBACK_ONLY", "bound to loopback"),
        health.CheckResult("elevenlabs", False, "ELEVENLABS_LOW_CHARACTERS", "low",
                           severity="warning"),
        health.CheckResult("disk", True, "DISK_OK", "fine", severity="info"),
    ]
    now = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)
    title, message = health.build_alert("failure", results, now, "box1-prod")
    assert "box1-prod" in title
    assert "Never copy flags from `ps`" in message
    assert "Top up or fix billing on the ElevenLabs account." in message
    assert "disk: OK - DISK_OK" in message


def test_parse_skip_rejects_unknown_components():
    assert health.parse_skip("higgsfield_mcp, elevenlabs") == ["higgsfield_mcp", "elevenlabs"]
    with pytest.raises(ValueError):
        health.parse_skip("higgsfeld")


def test_config_defaults_and_off_switches(config, monkeypatch):
    assert config.expect_runner == "claude"
    assert config.claude_bin is None
    assert config.low_credits == 500.0 and config.elevenlabs_low_chars == 10_000
    assert config.require_remotion is None and config.notify_success is False
    monkeypatch.setenv("PANDA_HEALTH_EXPECT_RUNNER", "off")
    monkeypatch.setenv("PANDA_HEALTH_LOW_CREDITS", "off")
    monkeypatch.setenv("PANDA_HEALTH_REQUIRE_REMOTION", "true")
    changed = health.config_from_env()
    assert changed.expect_runner == "" and changed.low_credits is None
    assert changed.require_remotion is True


def wire_healthy_box(tmp_path, config, monkeypatch, body=None):
    """Route every external touch point of run() to a healthy simulated box."""
    body = body or HEALTH_BODY
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DIFY_RUNNER=claude\nELEVENLABS_API_KEY=el-k\n",
                                   encoding="utf-8")
    monkeypatch.setattr(health, "_which", lambda name, path: {
        "claude": str(claude), "ffmpeg": "/x/ffmpeg", "ffprobe": "/x/ffprobe",
    }.get(Path(name).name))
    monkeypatch.setattr(health, "primary_ipv4", lambda: "10.1.2.3")
    monkeypatch.setattr(health, "tls_days_left", lambda host, port=443, timeout=10.0: 60.0)
    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: Usage(100 * GB, 40 * GB, 60 * GB))
    monkeypatch.setattr(health, "http_get", http_router({
        config.launcher_url: (200, body),
        "http://10.1.2.3:8501/health": (200, body),
        "https://panda.example.com/health": (200, body),
        health.ELEVENLABS_SUBSCRIPTION_URL: (200, {"character_count": 10,
                                                   "character_limit": 100_000,
                                                   "status": "active"}),
    }))
    canary = "\n".join([
        _line({"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "t1", "name": health.BALANCE_TOOL}]}}),
        _line({"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "t1",
            "content": '{"credits": 5000, "subscription_plan_type": "ultra"}'}]}}),
    ])
    render = fake_render_run()

    def fake_run(argv, *, cwd, timeout_s, env=None):
        if argv[0] == "git":
            return 0, "a" * 40 + "\n", "", False
        if argv[1:3] == ["auth", "status"]:
            return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), "", False
        if argv[1:2] == ["--version"]:
            return 0, "2.1.197 (Claude Code)\n", "", False
        if "-p" in argv:
            assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
            assert not any(key.startswith("PANDA_HEALTH_") for key in env)
            return 0, canary, "", False
        return render(argv, cwd=cwd, timeout_s=timeout_s, env=env)

    monkeypatch.setattr(health, "_run", fake_run)
    return dataclasses.replace(config, public_url="https://panda.example.com")


def test_run_covers_every_layer_on_a_healthy_box(tmp_path, config, monkeypatch, capsys):
    config = wire_healthy_box(tmp_path, config, monkeypatch)
    code = health.run(config, no_alert=True, proc_root=tmp_path / "no-proc")
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["healthy"] is True
    assert [r["component"] for r in summary["results"]] == list(health.COMPONENTS)
    assert summary["box"] == "box-test"
    state = json.loads(config.state_file.read_text(encoding="utf-8"))
    assert state["healthy"] is True and state["failed_codes"] == []


def test_run_alerts_once_when_the_mock_runner_is_serving(tmp_path, config, monkeypatch, capsys):
    config = wire_healthy_box(tmp_path, config, monkeypatch,
                              body=dict(HEALTH_BODY, runner="mock"))
    config = dataclasses.replace(config, webhook_url="https://hooks.example.com/T")
    sent = []
    monkeypatch.setattr(health, "send_webhook", lambda url, kind, message: sent.append(message))
    assert health.run(config, proc_root=tmp_path / "no-proc") == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["notification"] == "failure" and summary["alert_deliveries"] == 1
    assert "LAUNCHER_WRONG_RUNNER" in sent[0] and "PUBLIC_WRONG_RUNNER" in sent[0]
    # The same failure the next day is a reminder, not a new "failure".
    assert health.run(config, proc_root=tmp_path / "no-proc") == 1
    assert json.loads(capsys.readouterr().out)["notification"] is None


def test_one_crashing_check_does_not_hide_the_others(tmp_path, config, monkeypatch, capsys):
    config = wire_healthy_box(tmp_path, config, monkeypatch)

    def boom(cfg, ctx):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(health, "check_disk", boom)
    assert health.run(config, no_alert=True, proc_root=tmp_path / "no-proc") == 1
    results = {r["component"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    assert results["disk"]["code"] == "CHECK_CRASHED"
    assert results["dify"]["ok"] is True and results["render"]["ok"] is True
