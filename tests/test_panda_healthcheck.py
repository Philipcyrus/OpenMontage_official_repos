"""Unit tests for the daily Claude/Higgsfield health canary (no network calls)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


# --- GET /health/canary: the surface Dify polls each morning -----------------


def _client_and_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from dify_launcher import app as app_module
    from dify_launcher import canary as canary_mod

    state = tmp_path / "status.json"
    monkeypatch.setattr(canary_mod, "state_path", lambda: state)
    return TestClient(app_module.app), canary_mod, state


def _write(state, payload):
    state.write_text(json.dumps(payload), encoding="utf-8")


def test_canary_endpoint_reports_never_run_without_a_result(tmp_path, monkeypatch):
    """A missing state file is a reportable state, not a 500 for Dify to puzzle over."""
    client, _mod, _state = _client_and_app(tmp_path, monkeypatch)
    body = client.get("/health/canary").json()
    assert body["status"] == "never_run"
    assert body["healthy"] is None
    assert body["stale"] is True


def test_canary_endpoint_reports_corrupt_state_as_never_run(tmp_path, monkeypatch):
    client, _mod, state = _client_and_app(tmp_path, monkeypatch)
    state.write_text("{not json", encoding="utf-8")
    assert client.get("/health/canary").json()["status"] == "never_run"


def test_canary_endpoint_serves_a_fresh_result(tmp_path, monkeypatch):
    client, _mod, state = _client_and_app(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    _write(state, {"healthy": True, "checked_at": now, "codes": ["HIGGSFIELD_OK"],
                   "results": [{"component": "higgsfield_mcp", "ok": True}]})
    body = client.get("/health/canary").json()
    assert body["status"] == "ok"
    assert body["healthy"] is True
    assert body["stale"] is False
    assert body["age_seconds"] < 60
    assert body["results"][0]["component"] == "higgsfield_mcp"


def test_canary_endpoint_reports_a_fresh_failure(tmp_path, monkeypatch):
    client, _mod, state = _client_and_app(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    _write(state, {"healthy": False, "checked_at": now, "codes": ["LAUNCHER_DOWN"]})
    body = client.get("/health/canary").json()
    assert body["status"] == "failed"
    assert body["healthy"] is False


def test_stale_result_is_not_reported_as_healthy(tmp_path, monkeypatch):
    """The failure mode that matters: cron dies, and yesterday's PASS keeps being served.

    A stored healthy verdict older than the max age must surface as stale, or Dify goes on
    reporting a green morning long after the box stopped checking anything.
    """
    client, mod, state = _client_and_app(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "max_age_s", lambda: 26 * 3600)
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    _write(state, {"healthy": True, "checked_at": old, "codes": ["HIGGSFIELD_OK"]})
    body = client.get("/health/canary").json()
    assert body["status"] == "stale"
    assert body["stale"] is True
    assert body["healthy"] is True          # the stored verdict is preserved...
    assert body["status"] != "ok"           # ...but it must not read as a passing check


def test_canary_writes_what_the_endpoint_reads(tmp_path, monkeypatch):
    """Round-trip contract: the file panda_healthcheck.py writes is the file app.py serves.

    These two live in different directories and are edited independently; without this the
    state format can drift and Dify silently renders an empty report.
    """
    client, _mod, state = _client_and_app(tmp_path, monkeypatch)
    monkeypatch.setattr(health, "check_launcher",
                        lambda c: health.CheckResult("launcher", True, "LAUNCHER_OK", "up"))
    monkeypatch.setattr(health, "check_claude_auth",
                        lambda c: health.CheckResult("claude_auth", True, "CLAUDE_AUTH_PRESENT", "ok"))
    monkeypatch.setattr(health, "check_higgsfield",
                        lambda c: health.CheckResult("higgsfield_mcp", True, "HIGGSFIELD_OK",
                                                     "balance ok", metadata={"credits": 5648}))
    config = health.Config(
        repo=tmp_path, claude_bin=tmp_path / "claude", launcher_url="http://127.0.0.1:8501/health",
        claude_model="haiku", claude_timeout_s=90, claude_max_budget_usd=0.10,
        retry_count=0, retry_delay_s=0, low_credits=None, state_file=state,
        reminder_hours=24, webhook_url="", webhook_kind="slack", sns_topic_arn="",
        aws_region=None, notify_success=False,
    )
    assert health.run(config, no_alert=True) == 0

    body = client.get("/health/canary").json()
    assert body["status"] == "ok"
    assert body["healthy"] is True
    components = [r["component"] for r in body["results"]]
    assert components == ["launcher", "claude_auth", "higgsfield_mcp"]
    credits = [r for r in body["results"] if r["component"] == "higgsfield_mcp"][0]
    assert credits["metadata"]["credits"] == 5648


# --- the standalone health service (deploy/panda_health_service.py) ---------
#
# The reason this process exists: a monitor served BY the launcher cannot report the
# launcher being down. These tests pin that it can.


def _health_service(tmp_path, monkeypatch, launcher_probe):
    from fastapi.testclient import TestClient

    from deploy import panda_health_service as svc
    from dify_launcher import canary as canary_mod

    state = tmp_path / "status.json"
    monkeypatch.setattr(canary_mod, "state_path", lambda: state)
    monkeypatch.setattr(svc, "probe_launcher", lambda *a, **k: launcher_probe)
    return TestClient(svc.app), state


def test_service_liveness_does_no_io():
    """Its own /health must not depend on anything it reports on."""
    from fastapi.testclient import TestClient

    from deploy import panda_health_service as svc

    body = TestClient(svc.app).get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "panda-health"


def test_service_reports_launcher_down_with_a_healthy_stored_canary(tmp_path, monkeypatch):
    """The case the main launcher structurally cannot report about itself.

    Cron's stored verdict is a fresh PASS, but the launcher is down right now. Overall
    status must be `failed` — a report that said `ok` here would be describing a machine
    that is not currently serving anything.
    """
    down = {"ok": False, "code": "LAUNCHER_DOWN", "detail": "ConnectionRefusedError"}
    client, state = _health_service(tmp_path, monkeypatch, down)
    state.write_text(json.dumps({
        "healthy": True, "checked_at": datetime.now(timezone.utc).isoformat(),
        "codes": ["HIGGSFIELD_OK"], "results": [],
    }), encoding="utf-8")

    body = client.get("/health/canary").json()
    assert body["status"] == "failed"
    assert body["launcher_live"]["code"] == "LAUNCHER_DOWN"
    assert body["canary_status"] == "ok"      # the deep check itself was fine...
    assert body["healthy"] is True            # ...and its verdict is preserved


def test_service_reports_ok_only_when_live_and_fresh(tmp_path, monkeypatch):
    up = {"ok": True, "code": "LAUNCHER_OK", "runner": "claude"}
    client, state = _health_service(tmp_path, monkeypatch, up)
    state.write_text(json.dumps({
        "healthy": True, "checked_at": datetime.now(timezone.utc).isoformat(),
        "codes": ["HIGGSFIELD_OK"], "results": [],
    }), encoding="utf-8")
    body = client.get("/health/canary").json()
    assert body["status"] == "ok"
    assert body["launcher_live"]["runner"] == "claude"


def test_service_reports_stale_even_while_the_launcher_is_up(tmp_path, monkeypatch):
    """A live launcher must not launder a dead cron job into a green report."""
    up = {"ok": True, "code": "LAUNCHER_OK", "runner": "claude"}
    client, state = _health_service(tmp_path, monkeypatch, up)
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    state.write_text(json.dumps({"healthy": True, "checked_at": old, "codes": []}),
                     encoding="utf-8")
    body = client.get("/health/canary").json()
    assert body["status"] == "stale"


def test_service_and_launcher_agree_on_the_same_file(tmp_path, monkeypatch):
    """Both ports must give the same verdict; they share dify_launcher/canary.py.

    If these ever diverge, the same box reports two different answers depending on which
    port Dify happened to ask.
    """
    from fastapi.testclient import TestClient

    from deploy import panda_health_service as svc
    from dify_launcher import app as app_module
    from dify_launcher import canary as canary_mod

    state = tmp_path / "status.json"
    state.write_text(json.dumps({
        "healthy": False, "checked_at": datetime.now(timezone.utc).isoformat(),
        "codes": ["HIGGSFIELD_AUTH_FAILED"], "results": [{"component": "higgsfield_mcp"}],
    }), encoding="utf-8")
    monkeypatch.setattr(canary_mod, "state_path", lambda: state)
    monkeypatch.setattr(svc, "probe_launcher",
                        lambda *a, **k: {"ok": True, "code": "LAUNCHER_OK"})

    from_launcher = TestClient(app_module.app).get("/health/canary").json()
    from_service = TestClient(svc.app).get("/health/canary").json()
    for field in ("status", "healthy", "checked_at", "codes", "results", "stale"):
        assert from_launcher[field] == from_service[field], field


def test_probe_launcher_never_raises_on_a_dead_port():
    """Real socket call, nothing listening: it must return a verdict, not an exception."""
    from deploy import panda_health_service as svc

    result = svc.probe_launcher("http://127.0.0.1:1/health", timeout=1)
    assert result["ok"] is False
    assert result["code"] == "LAUNCHER_DOWN"
