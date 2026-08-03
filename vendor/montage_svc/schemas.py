"""Request/response models. Every path the renderer touches is derived from
these validated ids — no raw paths, no raw ffmpeg args, no raw filter strings.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from montage_svc.config import GRADE_PRESETS

SafeId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
Resolution = Annotated[str, Field(pattern=r"^\d{2,5}x\d{2,5}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# shared pieces
# --------------------------------------------------------------------------

class Captions(Strict):
    zh: Optional[str] = Field(None, max_length=300)
    en: Optional[str] = Field(None, max_length=300)


class Overlay(Strict):
    type: Literal["bubble", "callout"]
    text: str = Field(..., max_length=300)
    subtext: Optional[str] = Field(None, max_length=300)
    position: Literal["top", "middle", "bottom"] = "top"
    accent: Optional[Literal["red", "green", "yellow", "black"]] = None


class Sfx(Strict):
    media_id: SafeId
    at_s: float = Field(..., ge=0, le=3600)
    db: float = Field(0.0, ge=-60, le=12)


class AudioSpec(Strict):
    music_media_id: Optional[SafeId] = None
    voice_media_id: Optional[SafeId] = None
    sfx: list[Sfx] = Field(default_factory=list, max_length=64)
    music_db: float = Field(-18.0, ge=-60, le=12)
    voice_db: float = Field(-6.0, ge=-60, le=12)

    def media_ids(self) -> list[str]:
        ids = [m for m in (self.music_media_id, self.voice_media_id) if m]
        ids += [s.media_id for s in self.sfx]
        return ids

    def is_empty(self) -> bool:
        return not self.media_ids()


class Transition(Strict):
    type: Literal["xfade", "cut"] = "xfade"
    duration_s: float = Field(0.5, ge=0.0, le=3.0)


class Card(Strict):
    title: Optional[str] = Field(None, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    cta: Optional[str] = Field(None, max_length=120)
    bullets: list[str] = Field(default_factory=list, max_length=6)


class Cards(Strict):
    intro: Optional[Card] = None
    outro: Optional[Card] = None


# --------------------------------------------------------------------------
# /compose
# --------------------------------------------------------------------------

class Scene(Strict):
    media_id: SafeId
    duration_s: float = Field(..., gt=0.1, le=600)
    captions: Optional[Captions] = None
    overlays: list[Overlay] = Field(default_factory=list, max_length=6)


class ComposeRequest(Strict):
    run_id: SafeId
    version: int = Field(..., ge=1, le=9999)
    profile: SafeId = "bgc"
    fps: int = Field(60, ge=12, le=120)
    resolution: Resolution = "1080x1920"
    scenes: list[Scene] = Field(..., min_length=1, max_length=60)
    transition: Transition = Field(default_factory=Transition)
    cards: Optional[Cards] = None
    audio: AudioSpec = Field(default_factory=AudioSpec)
    grade: str = "warm"
    output_label: SafeId = "final"

    @field_validator("grade")
    @classmethod
    def _known_grade(cls, v: str) -> str:
        if v not in GRADE_PRESETS:
            raise ValueError(
                f"unknown grade {v!r}; available: {sorted(GRADE_PRESETS)}"
            )
        return v

    def media_ids(self) -> list[str]:
        return [s.media_id for s in self.scenes] + self.audio.media_ids()

    def wh(self) -> tuple[int, int]:
        w, h = self.resolution.split("x")
        return int(w), int(h)


# --------------------------------------------------------------------------
# /overlay — re-draw overlays on an already-rendered video
# --------------------------------------------------------------------------

class TimedScene(Strict):
    """Same caption/overlay shape as compose, but positioned by time range
    over an existing video rather than by clip."""

    start_s: float = Field(..., ge=0, le=3600)
    end_s: float = Field(..., gt=0, le=3600)
    captions: Optional[Captions] = None
    overlays: list[Overlay] = Field(default_factory=list, max_length=6)

    @field_validator("end_s")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        start = info.data.get("start_s")
        if start is not None and v <= start:
            raise ValueError("end_s must be greater than start_s")
        return v


class OverlayRequest(Strict):
    run_id: SafeId
    version: int = Field(..., ge=1, le=9999)
    source: str = Field(..., max_length=200)
    profile: SafeId = "bgc"
    scenes: list[TimedScene] = Field(..., min_length=1, max_length=60)
    output_label: SafeId = "final"


# --------------------------------------------------------------------------
# /mix-audio
# --------------------------------------------------------------------------

class MixRequest(Strict):
    run_id: SafeId
    version: int = Field(..., ge=1, le=9999)
    source: str = Field(..., max_length=200)
    audio: AudioSpec
    output_label: SafeId = "final"


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    ffmpeg: bool
    fonts: bool
    data_dir: bool
    profiles: list[str] = []
    warnings: list[str] = []


class ImportResponse(BaseModel):
    media_id: str
    local_url: str
    bytes: int
    kind: str


class JobAccepted(BaseModel):
    job_id: str


class JobStatus(BaseModel):
    job_id: str
    kind: str
    run_id: str
    status: Literal["queued", "running", "done", "failed"]
    progress: float = 0.0
    output_media_url: Optional[str] = None
    error: Optional[str] = None


class ImportRequest(Strict):
    run_id: SafeId
    url: str = Field(..., max_length=2000)
    label: SafeId

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return v


class LedgerRow(BaseModel):
    run_id: str
    ts: str
    kind: str
    detail: dict[str, Any]
