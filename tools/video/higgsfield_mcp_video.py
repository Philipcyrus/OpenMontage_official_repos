"""Higgsfield video generation via the agent's Higgsfield MCP connection.

This is the **MCP-bridge** provider. Unlike `higgsfield_video.py` (which calls
the Higgsfield Cloud REST API with HIGGSFIELD_API_KEY/SECRET), this tool relies
on the orchestrating agent's existing Higgsfield MCP connection — no REST keys
required. The agent performs the actual generation by calling the Higgsfield
MCP tools (generate_video -> job_status -> reveal_generation), then hands the
resulting clip back to this tool for download, probing, and provenance.

Why a Python tool can't call MCP itself: MCP tools are invoked by the agent,
not from inside this process. So this tool operates in two modes:

  1. **ingest** (normal path): the agent has already generated the clip via MCP
     and passes either a `video_url` (Higgsfield CDN URL from reveal_generation)
     or a local `source_path`. This tool downloads/copies it to `output_path`,
     probes it, and returns a standard ToolResult with full provenance.

  2. **handshake** (no media yet): if neither `video_url` nor `source_path` is
     given, this tool returns success=False with an `agent_action_required`
     payload spelling out exactly which MCP calls to make. This keeps the
     bridge protocol self-documenting even from code.

See `skills/meta/higgsfield-mcp-bridge.md` for the full agent-side protocol.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

# Default underlying Higgsfield model, per the MCP server's own guidance:
# seedance_2_0 for identity/cinematic; kling3_0 for multi-shot/audio/motion
# transfer; kling3_0_turbo for fast text-to-video / single start-frame.
_DEFAULT_MODEL = "seedance_2_0"

# Rough credit cost per 5s clip by model (Higgsfield credits, NOT USD). Used to
# warn against the budget. The agent confirms true cost via get_cost:true on the
# MCP generate_video call before committing.
_CREDITS_PER_5S = {
    "seedance_2_0": 70,
    "seedance_2_0_fast": 45,
    "kling3_0": 40,
    "kling3_0_turbo": 20,
    "veo_3.1": 50,
    "sora_2": 50,
}
# Approximate USD value of a Higgsfield credit (Ultra-tier blended estimate).
_USD_PER_CREDIT = 0.01


class HiggsFieldMCPVideo(BaseTool):
    name = "higgsfield_mcp_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "higgsfield_mcp"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    # Available whenever the operator has opted the agent's MCP bridge in.
    # Set HIGGSFIELD_MCP_BRIDGE=1 in .env to signal "this agent has a working
    # Higgsfield MCP connection."
    dependencies = ["env:HIGGSFIELD_MCP_BRIDGE"]
    install_instructions = (
        "This provider uses the orchestrating agent's Higgsfield MCP connection — "
        "no REST API key needed.\n"
        "  1. Ensure the agent has the Higgsfield MCP server connected.\n"
        "  2. Set HIGGSFIELD_MCP_BRIDGE=1 in .env to enable this provider.\n"
        "  See skills/meta/higgsfield-mcp-bridge.md for the generation protocol."
    )
    agent_skills = ["seedance-2-0", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "character_consistency": True,
        "multi_model_routing": True,
        "native_audio": True,
        "cinematic_quality": True,
        "camera_direction": True,
        "lip_sync": True,
        "multi_shot": True,
    }
    best_for = [
        "premium video gen through the agent's Higgsfield MCP (no API keys, uses credits)",
        "cinematic trailers, teasers, and high-fidelity clips with native synced audio",
        "character-consistent generation (Seedance 2.0 identity, Soul ID)",
        "multi-shot edits and director-level camera control in a single generation",
        "image-to-video animation from a generated still or uploaded reference",
    ]
    not_good_for = ["offline generation", "deterministic output", "zero-credit/free projects"]
    fallback_tools = ["higgsfield_video", "seedance_video", "kling_video", "veo_video"]
    quality_score = 0.9

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "model": {
                "type": "string",
                "description": (
                    "Higgsfield MCP model id. Default seedance_2_0. Common: "
                    "seedance_2_0, kling3_0 (multi-shot/audio/motion transfer), "
                    "kling3_0_turbo (fast text-to-video). Confirm exact ids with "
                    "models_explore via MCP."
                ),
                "default": _DEFAULT_MODEL,
            },
            "duration": {"type": "integer", "default": 5, "description": "Clip length in seconds."},
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1", "21:9"],
                "default": "16:9",
            },
            # --- Bridge ingest fields (the agent fills ONE of these after MCP gen) ---
            "video_url": {
                "type": "string",
                "description": "Higgsfield CDN URL of the finished clip (from reveal_generation).",
            },
            "source_path": {
                "type": "string",
                "description": "Local path to a clip the agent already downloaded from MCP.",
            },
            "job_id": {"type": "string", "description": "Higgsfield MCP job id, for provenance."},
            "image_url": {
                "type": "string",
                "description": "Reference image URL/media_id for image_to_video (provenance only).",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout", "network"])
    idempotency_key_fields = ["prompt", "model", "operation", "duration", "job_id"]
    side_effects = ["writes video file to output_path", "downloads from Higgsfield CDN"]
    user_visible_verification = ["Watch generated clip for motion coherence and visual quality"]

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        model = inputs.get("model", _DEFAULT_MODEL)
        duration = int(inputs.get("duration", 5) or 5)
        credits = _CREDITS_PER_5S.get(model, 50) * (duration / 5)
        return round(credits * _USD_PER_CREDIT, 2)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        model = inputs.get("model", _DEFAULT_MODEL)
        if "turbo" in model or "fast" in model:
            return 60.0
        return 150.0

    def _agent_action_required(self, inputs: dict[str, Any]) -> ToolResult:
        """No media yet — tell the agent exactly which MCP calls to make."""
        model = inputs.get("model", _DEFAULT_MODEL)
        operation = inputs.get("operation", "text_to_video")
        est_credits = _CREDITS_PER_5S.get(model, 50) * (int(inputs.get("duration", 5) or 5) / 5)
        return ToolResult(
            success=False,
            error=(
                "higgsfield_mcp_video is an MCP-bridge tool: it cannot call MCP from "
                "inside Python. The agent must generate the clip via the Higgsfield MCP "
                "tools, then re-invoke this tool with `video_url` (or `source_path`)."
            ),
            data={
                "agent_action_required": {
                    "step_1": "mcp__...__generate_video with params {model, prompt, duration, aspect_ratio, count:1, get_cost:true} to preflight credits",
                    "step_2": "mcp__...__generate_video (get_cost omitted) to submit the job; capture job_id",
                    "step_3": "poll mcp__...__job_status (or show_generations) until completed",
                    "step_4": "mcp__...__reveal_generation / job_display to obtain the clip CDN url",
                    "step_5": f"re-invoke higgsfield_mcp_video with the same params PLUS video_url=<cdn url>, job_id=<id>, output_path=<project assets path>",
                    "suggested_model": model,
                    "operation": operation,
                    "estimated_credits": round(est_credits, 1),
                    "protocol_skill": "skills/meta/higgsfield-mcp-bridge.md",
                }
            },
        )

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        model = inputs.get("model", _DEFAULT_MODEL)
        operation = inputs.get("operation", "text_to_video")
        video_url = inputs.get("video_url")
        source_path = inputs.get("source_path")

        if not video_url and not source_path:
            return self._agent_action_required(inputs)

        output_path = Path(inputs.get("output_path", "higgsfield_mcp_output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if source_path:
                src = Path(source_path)
                if not src.is_file():
                    return ToolResult(success=False, error=f"source_path not found: {source_path}")
                if src.resolve() != output_path.resolve():
                    shutil.copyfile(src, output_path)
            else:
                import requests

                resp = requests.get(video_url, timeout=180)
                resp.raise_for_status()
                output_path.write_bytes(resp.content)
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"Failed to materialize Higgsfield clip: {e}")

        if not output_path.is_file() or output_path.stat().st_size == 0:
            return ToolResult(success=False, error="Ingested clip is empty or missing.")

        from tools.video._shared import probe_output

        probed = probe_output(output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield_mcp",
                "model": model,
                "prompt": inputs.get("prompt", ""),
                "operation": operation,
                "aspect_ratio": inputs.get("aspect_ratio", "16:9"),
                "job_id": inputs.get("job_id"),
                "source": "video_url" if video_url else "source_path",
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )
