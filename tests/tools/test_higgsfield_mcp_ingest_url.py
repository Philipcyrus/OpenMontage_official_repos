"""Ingesting a Higgsfield clip hands back its CDN link for the asset row's original_url.

The opt-in Kling customer lip-sync pass sends Kling that link; Kling cannot read a local file.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tools.video.higgsfield_mcp_video import HiggsFieldMCPVideo

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def _clip(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=12", "-t", "1",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True)
    return path


def test_ingest_returns_the_cdn_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _clip(tmp_path / "src.mp4").read_bytes()
    url = "https://d8j0ntlcm91z4.cloudfront.net/user_x/hf_clip.mp4"
    seen: list[str] = []

    def fake_get(u: str, timeout: int = 0) -> object:
        seen.append(u)
        return types.SimpleNamespace(content=body, raise_for_status=lambda: None)

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=fake_get))
    out = HiggsFieldMCPVideo().execute({"video_url": url, "output_path": str(tmp_path / "s01.mp4")})
    assert out.success, out.error
    assert out.data["original_url"] == url and seen == [url]


def test_a_local_ingest_has_no_link(tmp_path: Path) -> None:
    src = _clip(tmp_path / "src.mp4")
    out = HiggsFieldMCPVideo().execute({"source_path": str(src),
                                        "output_path": str(tmp_path / "s01.mp4")})
    assert out.success, out.error
    assert out.data["original_url"] is None
