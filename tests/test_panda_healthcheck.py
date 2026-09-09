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
