"""Unit tests for TTS → Higgsfield duration snapping."""

from __future__ import annotations

from lib.i2v_duration import snap_i2v_duration


def test_vo_under_min_uses_min_no_hold():
    out = snap_i2v_duration(2.3, min_s=5, max_s=10)
    assert out["i2v_duration"] == 5
    assert out["hold_extend_seconds"] == 0.0


def test_vo_between_allowed_picks_ceil_cover():
    out = snap_i2v_duration(6.1, allowed=[5, 10])
    assert out["i2v_duration"] == 10
    assert out["hold_extend_seconds"] == 0.0


def test_vo_exact_allowed():
    out = snap_i2v_duration(5.0, allowed=[5, 10])
    assert out["i2v_duration"] == 5
    assert out["hold_extend_seconds"] == 0.0


def test_vo_over_max_extends_hold():
    out = snap_i2v_duration(12.4, allowed=[5, 10])
    assert out["i2v_duration"] == 10
    assert out["hold_extend_seconds"] == 2.4


def test_contiguous_range_ceils():
    out = snap_i2v_duration(6.1, min_s=5, max_s=10)
    assert out["i2v_duration"] == 7
    assert out["hold_extend_seconds"] == 0.0


def test_zero_vo_uses_min():
    out = snap_i2v_duration(0.0, min_s=5, max_s=10)
    assert out["i2v_duration"] == 5
    assert out["hold_extend_seconds"] == 0.0
