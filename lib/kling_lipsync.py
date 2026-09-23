"""Kling lip-sync for customer speaking clips (panda-video, opt-in).

Higgsfield / Seedance still makes every clip. For a scene where the customer is the only character
who speaks on screen, this re-drives the customer's mouth with Kling's advanced lip-sync, using the
exact ElevenLabs line(s) at their original offsets in the clip. The panda never goes to Kling
(Kling does not support animal characters) and narrator lines never drive a mouth.

Everything that spends money or touches files lives here, so the agent runs one command and a
restart can never pay twice:

- ledger: ``<project>/assets/video/kling/ledger.json``. The Kling task id is written the moment
  Kling returns it, before any polling. A re-run polls that task; it never submits it again. A
  paid or ambiguous state (sent, maybe sent, failed, done) is never overwritten by a later request
  problem or a skip, and a submission that may have reached Kling is resent only on an explicit
  per-scene ``--resend-unknown``, under the same external task id.
- ``video_url`` must be this clip: its bytes are downloaded (free) and compared with the clip's
  before anything is paid, so a stale link to an older take can never be lip-synced over a new one.
- ``<project>/assets/video/kling/<clip>.original.mp4``: byte-identical copy of the clip.
- ``<project>/assets/video/kling/<clip>.kling-lipsync.mp4``: Kling's result.
- The clip's own path holds the SELECTED version (Kling's once it passes its checks), so the asset
  manifest, the launcher, edit and compose need no change. ``select`` switches back.
- The launcher never shows ``assets/video/kling/`` files as clips.
- One pass waits at most ``KLING_LIPSYNC_TIMEOUT_S`` (default 360 s, under the agent's 10-minute
  command limit). Anything still processing is collected by running the same command again.

The final soundtrack never comes from Kling: compose strips every clip's audio and lays the same
ElevenLabs files once at their script times.

CLI::

    python -m lib.kling_lipsync run <project_dir> [--request FILE] [--default-speaker panda]
                                    [--max-usd 5] [--retry-failed] [--resend-unknown SCENE_ID]
    python -m lib.kling_lipsync select <project_dir> <scene_id> original|kling
    python -m lib.kling_lipsync status <project_dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional
from urllib.parse import urlparse

KLING_DIR = Path("assets") / "video" / "kling"
LEDGER_FILE = "ledger.json"
REQUEST_FILE = "request.json"
LOCK_FILE = "run.lock"
PROVIDER = "kling_official"
LIPSYNC_PATH = "/v1/videos/advanced-lip-sync"
MIN_AUDIO_MS = 2000               # Kling refuses less than 2 s of cropped audio
DURATION_TOLERANCE_S = 0.25       # Kling's output must keep the clip's length
COVER_TOLERANCE_MS = 40           # ...and must not end before the customer stops speaking
ASPECT_TOLERANCE = 0.01
MAX_ATTEMPTS = 2                  # lip-sync submissions per take
DEFAULT_MAX_USD = 5.0
DEFAULT_WAIT_S = 360.0            # one pass returns well inside the agent's 10-minute limit
START_MARGIN_S = 90.0             # no new clip is started this close to the pass deadline
STALE_LOCK_S = 3 * 3600
BUSY_CODES = {"1302", "1303"}     # Kling's rate / parallel-task limits: refused, nothing created
BUSY_BACKOFF_S = (20.0, 40.0)
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac")
EPS = 0.05

# Estimates from tools.avatar.kling_lip_sync (low confidence; Kling bills from prepaid packs and
# does not report a per-task charge). Budget checks use them conservatively.
ESTIMATE_IDENTIFY_USD = 0.02
ESTIMATE_LIPSYNC_USD = 0.32

DONE = "done"
SUBMITTING = "submitting"
SUBMITTED = "submitted"
UNKNOWN_SUBMISSION = "unknown_submission"
FAILED = "failed"
REJECTED_OUTPUT = "rejected_output"
NEEDS_FACE_CHOICE = "needs_face_choice"
NO_FACE = "no_face"
INELIGIBLE = "ineligible"
SKIPPED = "skipped"
BLOCKED_BUDGET = "blocked_budget"
QUEUED = "queued"
RETRYABLE = {FAILED, REJECTED_OUTPUT, NO_FACE}
# States that record money spent or possibly spent. A later request problem, skip, budget block
# or missing key only adds a note to them; it never replaces them (that would reopen a paid take).
PROTECTED = {DONE, SUBMITTING, SUBMITTED, UNKNOWN_SUBMISSION, FAILED, REJECTED_OUTPUT, NO_FACE,
             NEEDS_FACE_CHOICE}

# Set by SIGTERM/SIGHUP (and cleared per pass): workers stop polling and start nothing new, so the
# lock is released and every task id already saved stays collectable.
_STOP = threading.Event()


class LedgerError(RuntimeError):
    """ledger.json exists but cannot be read — never treated as empty (that would resend)."""


class Ineligible(ValueError):
    """This clip cannot go to Kling; nothing was sent."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe(scene_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in scene_id)[:64] or "scene"


def _inside(project_dir: Path, rel: str) -> Path:
    """Resolve a project-relative (or absolute) path and refuse anything outside the project."""
    p = Path(rel)
    p = (p if p.is_absolute() else project_dir / p).resolve()
    p.relative_to(project_dir.resolve())          # ValueError when it escapes
    return p


def _probe(path: Path) -> dict[str, Any]:
    """Container length and the VIDEO stream: whether there is one, its length and frame size.

    ``video_s`` is 0 when there is no video stream; it never falls back to the container length
    (an audio-only file would otherwise look like a video of the right length).
    """
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = {}
    try:
        container = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        container = 0.0
    out: dict[str, Any] = {"duration_s": container, "has_video": False, "video_s": 0.0}
    for s in data.get("streams") or []:
        try:
            w, h = float(s.get("width") or 0), float(s.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if s.get("codec_type") == "video" and w > 0 and h > 0:
            out.update(has_video=True, width=w, height=h)
            try:
                out["video_s"] = float(s.get("duration"))
            except (TypeError, ValueError):
                out["video_s"] = container      # some containers carry no per-stream length
            break
    return out


def _video_decodes(path: Path) -> bool:
    """The whole video stream decodes. A truncated file still reports its full length and frame
    size from its header, so only decoding it proves it plays."""
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# ---------------------------------------------------------------------------
# which scenes may go to Kling (pure, deterministic)
# ---------------------------------------------------------------------------

def _latest_artifact(project_dir: Path, name: str, stage: Optional[str] = None
                     ) -> Optional[dict[str, Any]]:
    """The ``name`` artifact from its checkpoint or artifacts/<name>.json, whichever is newer."""
    cp_path = project_dir / f"checkpoint_{stage or name}.json"
    art_path = project_dir / "artifacts" / f"{name}.json"
    cp = _read_json(cp_path)
    from_cp = ((cp or {}).get("artifacts") or {}).get(name) if isinstance(cp, dict) else None
    from_cp = from_cp if isinstance(from_cp, dict) else None
    from_art = _read_json(art_path)
    from_art = from_art if isinstance(from_art, dict) else None
    if from_cp is not None and from_art is not None:
        try:
            return from_art if art_path.stat().st_mtime > cp_path.stat().st_mtime else from_cp
        except OSError:
            return from_cp
    return from_cp if from_cp is not None else from_art


def _section_owners(plan: dict[str, Any]) -> dict[str, set[str]]:
    """section id -> every scene that lists it (narration ``script_section_id`` entries)."""
    owners: dict[str, set[str]] = {}
    for sc in plan.get("scenes") or []:
        if not isinstance(sc, dict) or sc.get("id") is None:
            continue
        for ra in sc.get("required_assets") or []:
            if isinstance(ra, dict) and ra.get("script_section_id"):
                owners.setdefault(str(ra["script_section_id"]), set()).add(str(sc["id"]))
    return owners


def scene_lines(script: dict[str, Any], plan: dict[str, Any], scene_id: str,
                default_speaker: str = "panda") -> list[dict[str, Any]]:
    """Script lines that belong to ``scene_id``, with their offset in the clip.

    A line belongs to the scene the scene plan ties it to (``script_section_id``); a line the plan
    does not tie to any scene belongs to the scene whose window it starts in. A line without
    ``speaker`` belongs to the job's default voice (options.narrator), exactly as the voice cast
    resolves it.
    """
    scene = next((s for s in plan.get("scenes") or []
                  if isinstance(s, dict) and str(s.get("id")) == scene_id), None)
    if scene is None:
        raise LookupError(f"scene {scene_id} is not in the scene plan")
    start, end = float(scene["start_seconds"]), float(scene["end_seconds"])
    owners = _section_owners(plan)
    lines: list[dict[str, Any]] = []
    for sec in script.get("sections") or []:
        if not isinstance(sec, dict) or sec.get("start_seconds") is None:
            continue
        s0 = float(sec["start_seconds"])
        owner = owners.get(str(sec.get("id")))
        if owner is not None:
            if scene_id not in owner:
                continue
        elif s0 < start - EPS or s0 >= end - EPS:
            continue
        speaker = str(sec.get("speaker") or default_speaker or "panda").strip().lower()
        line = {"section_id": str(sec.get("id")), "speaker": speaker,
                "offset_s": round(max(0.0, s0 - start), 3)}
        if s0 < start - EPS:
            line["starts_before_s"] = round(start - s0, 3)   # compose lays it before the clip
        lines.append(line)
    return lines


def eligibility(lines: list[dict[str, Any]]) -> tuple[bool, str]:
    ok, why = _speakers_ok(lines)
    if ok and any(ln.get("starts_before_s") for ln in lines if ln["speaker"] == "customer"):
        return False, ("the customer's line starts before this clip does, so Kling cannot place "
                       "it where the final mix plays it")
    return ok, why


def _speakers_ok(lines: list[dict[str, Any]]) -> tuple[bool, str]:
    """The customer speaks on screen and nobody Kling cannot handle does."""
    speakers = {ln["speaker"] for ln in lines}
    if "customer" not in speakers:
        return False, "the customer has no line in this scene"
    if "panda" in speakers:
        return False, ("the panda also speaks in this clip; Kling does not support animal "
                       "characters, so this scene stays on Seedance")
    other = speakers - {"customer", "narrator"}
    if other:
        return False, f"unrecognised speaker(s) {sorted(other)}"
    return True, ""


def customer_scenes(project_dir: Path, default_speaker: str = "panda") -> list[str]:
    """Scene ids where the customer speaks and the panda does not (what the gate reports on,
    including a scene Kling then refuses for timing)."""
    script = _latest_artifact(Path(project_dir), "script") or {}
    plan = _latest_artifact(Path(project_dir), "scene_plan") or {}
    out: list[str] = []
    for sc in plan.get("scenes") or []:
        if not isinstance(sc, dict) or sc.get("id") is None:
            continue
        try:
            ok, _ = _speakers_ok(scene_lines(script, plan, str(sc["id"]), default_speaker))
        except (LookupError, KeyError, TypeError, ValueError):
            continue
        if ok:
            out.append(str(sc["id"]))
    return out


# ---------------------------------------------------------------------------
# ledger + single-run lock
# ---------------------------------------------------------------------------

class Ledger:
    def __init__(self, project_dir: Path) -> None:
        self.path = project_dir / KLING_DIR / LEDGER_FILE
        self._lock = threading.Lock()
        if self.path.exists():
            data = _read_json(self.path)
            if not isinstance(data, dict) or not isinstance(data.get("scenes"), dict):
                # Starting empty would resend every scene and forget the spend: refuse instead.
                raise LedgerError(f"{KLING_DIR / LEDGER_FILE} exists but cannot be read; "
                                  "nothing was sent")
        else:
            data = {"version": 1, "provider": PROVIDER, "scenes": {}}
        self.data = data

    def get(self, scene_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.data["scenes"].get(scene_id) or {})

    def update(self, scene_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            rec = self.data["scenes"].setdefault(scene_id, {})
            rec.update(fields)
            rec["updated_at"] = _now()
            self._save()
            return dict(rec)

    def add_cost(self, scene_id: str, usd: float) -> None:
        with self._lock:
            rec = self.data["scenes"].setdefault(scene_id, {})
            rec["estimated_usd"] = round(max(0.0, float(rec.get("estimated_usd") or 0.0) + usd), 4)
            self._save()

    def restart(self, scene_id: str, **why: Any) -> None:
        """Archive the scene's record (its spend stays counted) and start a fresh one."""
        with self._lock:
            old = self.data["scenes"].get(scene_id) or {}
            history = list(old.get("history") or [])
            if old:
                history.append({**{k: v for k, v in old.items() if k != "history"}, **why})
            self.data["scenes"][scene_id] = {"history": history, "updated_at": _now()}
            self._save()

    def spent_usd(self) -> float:
        with self._lock:
            total = 0.0
            for rec in self.data["scenes"].values():
                total += float(rec.get("estimated_usd") or 0.0)
                total += sum(float(h.get("estimated_usd") or 0.0) for h in rec.get("history") or [])
            return round(total, 4)

    def _save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def _park(ledger: Ledger, scene_id: str, status: str, reason: str, **fields: Any) -> None:
    """Record why a scene was not sent. A paid or ambiguous state only gains a note."""
    if ledger.get(scene_id).get("status") in PROTECTED:
        ledger.update(scene_id, note=reason[:300])
    else:
        ledger.update(scene_id, status=status, reason=reason[:300], **fields)


def _pid_alive(pid: int) -> Optional[bool]:
    """True/False when it can be told; None when it cannot (then the lock's age decides)."""
    if pid == os.getpid():
        return True
    try:
        import psutil  # type: ignore[import-not-found]
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return None       # never os.kill on Windows: signal 0 there is CTRL_C_EVENT


def _lock_is_stale(lock: Path) -> bool:
    try:
        age = time.time() - lock.stat().st_mtime
        parts = lock.read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return False
    alive: Optional[bool] = None
    try:
        if len(parts) >= 2 and parts[1] == socket.gethostname():
            alive = _pid_alive(int(parts[0]))
    except ValueError:
        alive = None
    # A real pass lasts minutes, so a lock older than STALE_LOCK_S is stale even when its pid is
    # alive (after a reboot the pid can belong to an unrelated process).
    return alive is False or age > STALE_LOCK_S


@contextmanager
def _run_lock(kdir: Path) -> Iterator[bool]:
    """One Kling pass (or select) per job at a time. A lock whose process is gone is taken over."""
    kdir.mkdir(parents=True, exist_ok=True)
    lock = kdir / LOCK_FILE
    if lock.exists() and _lock_is_stale(lock):
        try:
            lock.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        os.write(fd, f"{os.getpid()} {socket.gethostname()} {_now()}".encode())
        os.close(fd)
        yield True
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------

def _default_client_factory(*, submit: bool = False) -> Any:
    from tools._kling.client import KlingClient
    # Paid POSTs (identify, lip-sync) are never auto-retried: a retry after a lost response
    # could pay twice. Polls and downloads are free GETs and keep the client's retries.
    return KlingClient(max_retries=0) if submit else KlingClient()


def _manifest_voice_rows(project_dir: Path) -> dict[str, list[str]]:
    """section id -> narration paths recorded in the asset manifest (voice_performance)."""
    manifest = _latest_artifact(project_dir, "asset_manifest", stage="assets") or {}
    rows: dict[str, list[str]] = {}
    for a in manifest.get("assets") or []:
        if not isinstance(a, dict) or a.get("type") not in ("narration", "audio") or not a.get("path"):
            continue
        sec = (a.get("voice_performance") or {}).get("source_section_id")
        if sec:
            rows.setdefault(str(sec), []).append(str(a["path"]))
    return rows


def voice_files(project_dir: Path, lines: list[dict[str, Any]], audio_map: dict[str, str],
                manifest_rows: dict[str, list[str]]) -> list[Path]:
    """The exact file for each customer line: request ``audio`` map, then the asset manifest, then
    ``assets/audio/vo-<section>-customer.*``. More than one candidate is refused, never guessed."""
    out: list[Path] = []
    for ln in lines:
        sid = ln["section_id"]
        if audio_map.get(sid):
            out.append(_inside(project_dir, str(audio_map[sid])))
            continue
        rows = sorted(set(manifest_rows.get(sid) or []))
        if len(rows) > 1:
            raise Ineligible(f"several voice files are recorded for line {sid}; list the one "
                             "compose uses in request.json \"audio\"")
        if rows:
            out.append(_inside(project_dir, rows[0]))
            continue
        adir = project_dir / "assets" / "audio"
        # Any take of this line (e.g. a faster retake saved as vo-<sec>-customer.speed108.wav)
        # counts, so two takes are refused instead of the old one being picked.
        hits = sorted(p for p in adir.glob(f"vo-{sid}-customer*")
                      if p.suffix.lower() in AUDIO_EXTS)
        if len(hits) > 1:
            raise Ineligible(f"several voice files match line {sid}; list the one compose uses in "
                             "request.json \"audio\"")
        if not hits:
            raise Ineligible(f"no voice file found for line {sid}; list it in request.json "
                             "\"audio\"")
        out.append(hits[0])
    for p in out:
        if not p.is_file():
            raise Ineligible(f"voice file not found: {p.relative_to(project_dir.resolve()).as_posix()}")
    return out


def _same_audio(a: Any, b: Any) -> bool:
    """Same words at the same times: section, offset and bytes (a file's name does not count)."""
    def key(sig: Any) -> list[tuple]:
        return [(x.get("section_id"), x.get("offset_s"), x.get("sha256"))
                for x in sig or [] if isinstance(x, dict)]
    return key(a) == key(b)


def _audio_sig(project_dir: Path, lines: list[dict[str, Any]], files: list[Path]) -> list[dict]:
    root = project_dir.resolve()
    return [{"section_id": ln["section_id"], "offset_s": ln["offset_s"],
             "path": f.resolve().relative_to(root).as_posix(), "sha256": _sha256(f)}
            for ln, f in zip(lines, files)]


class _Pass:
    def __init__(self, project_dir: Path, ledger: Ledger, client_factory: Callable[..., Any],
                 poll_interval: float, wait_s: float) -> None:
        self.project_dir = project_dir
        self.kdir = project_dir / KLING_DIR
        self.ledger = ledger
        self.client_factory = client_factory
        self.poll_interval = poll_interval
        self.wait_s = wait_s
        self.start_clock()
        # No new clip starts this close to the deadline (a quarter of a short wait).
        self.margin = min(START_MARGIN_S, max(0.0, wait_s) * 0.25)

    def start_clock(self) -> None:
        """The wait counts from when the workers start (local audio prep is not Kling time)."""
        self.deadline = time.time() + self.wait_s

    def time_left(self) -> float:
        return self.deadline - time.time()

    # -- audio --------------------------------------------------------------
    def build_audio(self, scene_id: str, lines: list[dict[str, Any]], files: list[Path],
                    clip_video_s: float) -> dict[str, Any]:
        """The customer's line(s) exactly as the final mix plays them, where they start, and a
        clip-local copy for lip-sync QA (so QA never needs an offset)."""
        from tools.video.panda_render import _premix_voice_tracks

        first = min(ln["offset_s"] for ln in lines)
        lens = [_probe(f)["duration_s"] for f in files]
        speech_end_ms = int(round(max(ln["offset_s"] + d for ln, d in zip(lines, lens)) * 1000))
        src = files[0]
        if len(lines) > 1:
            src = _premix_voice_tracks(
                [{"path": str(f), "at_s": ln["offset_s"] - first} for f, ln in zip(files, lines)],
                self.kdir / f"{_safe(scene_id)}.kling-audio.wav")
        insert_ms = int(round(first * 1000))
        audio_ms = int(round(_probe(src)["duration_s"] * 1000))
        clip_ms = int(round(clip_video_s * 1000))
        if audio_ms < MIN_AUDIO_MS:
            # Kling needs 2 s of audio: pad with silence after the line, or before it when the
            # line sits near the end of the clip (the insert time moves back by the same amount).
            need = MIN_AUDIO_MS - audio_ms
            post = max(0, min(need, clip_ms - (insert_ms + audio_ms)))
            pre = need - post
            if pre > insert_ms:
                raise Ineligible("Kling needs at least 2 s of audio and the clip is too short "
                                 "around the customer's line")
            padded = self.kdir / f"{_safe(scene_id)}.kling-audio-padded.wav"
            af = (f"adelay=delays={pre}:all=1," if pre else "") + "apad"
            # -t bounds the pad: a bare apad never ends.
            r = subprocess.run(["ffmpeg", "-y", "-i", str(src), "-af", af,
                                "-t", f"{MIN_AUDIO_MS / 1000:.3f}", str(padded)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not padded.is_file():
                raise RuntimeError("could not pad a short line to Kling's 2 s minimum")
            src, insert_ms = padded, insert_ms - pre
            audio_ms = int(round(_probe(padded)["duration_s"] * 1000))
        if insert_ms + audio_ms > clip_ms + 5:
            raise Ineligible("the customer's line runs past the end of the clip")
        qa = _premix_voice_tracks([{"path": str(f), "at_s": ln["offset_s"]}
                                   for f, ln in zip(files, lines)],
                                  self.kdir / f"{_safe(scene_id)}.qa-audio.wav")
        root = self.project_dir.resolve()
        return {"audio_path": Path(src).resolve().relative_to(root).as_posix(),
                "insert_ms": insert_ms, "audio_ms": audio_ms, "speech_end_ms": speech_end_ms,
                "qa_audio_path": Path(qa).resolve().relative_to(root).as_posix()}

    # -- one scene ------------------------------------------------------------
    def process(self, scene_id: str, job: dict[str, Any]) -> None:
        try:
            rec = self.ledger.get(scene_id)
            if rec.get("status") == SUBMITTED and rec.get("task_id"):
                self._poll_and_apply(scene_id, job)
                return
            if _STOP.is_set() or self.time_left() < self.margin:
                self._not_now(scene_id)
                return
            self._identify_and_submit(scene_id, job)
        except Exception as exc:  # noqa: BLE001 — one scene never stops the others
            rec = self.ledger.get(scene_id)
            if rec.get("status") in (SUBMITTED, SUBMITTING, UNKNOWN_SUBMISSION):
                self.ledger.update(scene_id, note=f"interrupted: {type(exc).__name__}: {exc}"[:300])
            else:
                self.ledger.update(scene_id, status=FAILED,
                                   reason=f"{type(exc).__name__}: {exc}"[:300])

    def _not_now(self, scene_id: str, why: str = ("the pass ran out of time before this clip was "
                                                  "sent; run the same command again")) -> None:
        """This run meant to send the clip but did not start it: pending for the next run."""
        _park(self.ledger, scene_id, QUEUED, why)
        self.ledger.update(scene_id, waiting=why)

    def _settle(self, scene_id: str, job: dict[str, Any], status: str, reason: str) -> None:
        """An outcome that sent no lip-sync request. A resend keeps its 'may have been sent' state,
        so the next attempt still goes out under the same external id."""
        if job.get("resend_external_id"):
            self.ledger.update(scene_id, note=f"the resend was not sent: {reason}"[:300])
        else:
            self.ledger.update(scene_id, status=status, reason=reason[:300])

    def _url_is_this_clip(self, scene_id: str, job: dict[str, Any]) -> bool:
        """Kling reads video_url, not our file: prove it is the same bytes before paying (free)."""
        tmp = self.kdir / f"_{_safe(scene_id)}.url-check.part"
        try:
            self.client_factory().download(job["video_url"], tmp)
            same = _sha256(tmp) == job["original_sha256"]
        except Exception as exc:  # noqa: BLE001 — nothing was paid; the next run tries again
            _park(self.ledger, scene_id, INELIGIBLE,
                  f"could not download video_url to check it: {type(exc).__name__}: {exc}")
            return False
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        if not same:
            _park(self.ledger, scene_id, INELIGIBLE,
                  "video_url is not this clip (it points at another take); use the clip's own "
                  "original_url")
        return same

    def _identify_and_submit(self, scene_id: str, job: dict[str, Any]) -> None:
        from tools._kling.errors import KlingAPIError
        from tools.avatar.kling_lip_sync import KlingLipSync

        # While waiting on a busy Kling the scene goes back to the state it came in with when that
        # state gates a paid retry (failed / face choice); a fresh scene waits as "queued".
        prior = self.ledger.get(scene_id).get("status")
        parked = prior if prior in (RETRYABLE | {NEEDS_FACE_CHOICE}) else QUEUED

        if not self._url_is_this_clip(scene_id, job):
            return
        if _STOP.is_set() or self.time_left() < self.margin:      # the url check can be slow
            self._not_now(scene_id)
            return
        tool = KlingLipSync()
        client = self.client_factory(submit=True)
        self.ledger.add_cost(scene_id, ESTIMATE_IDENTIFY_USD)
        try:
            identify = tool._identify_faces(client, {"video_url": job["video_url"]})
        except KlingAPIError as exc:
            if not _definitely_not_created(exc):
                raise                                   # may have been billed: a plain failure
            self.ledger.add_cost(scene_id, -ESTIMATE_IDENTIFY_USD)     # refused: not billed
            if _is_busy(exc):
                self._not_now(scene_id, "Kling is busy (rate limit); nothing was sent. Run the "
                                        "same command again")
                return
            self._settle(scene_id, job, FAILED, f"Kling refused the face check: {exc}")
            return
        except ValueError as exc:
            if "no faces" in str(exc):
                self._settle(scene_id, job, NO_FACE,
                             "Kling found no face it can lip-sync in this clip")
                return
            raise
        faces = identify["faces"]
        summary = [{"face_id": str(f.get("face_id") or f.get("id") or ""),
                    **{k: f[k] for k in ("start_time", "end_time", "bbox", "box") if k in f}}
                   for f in faces]
        (self.kdir / f"{_safe(scene_id)}.faces.json").write_text(
            json.dumps({"session_id": identify["session_id"], "faces": faces}, indent=1),
            encoding="utf-8")
        self.ledger.update(scene_id, session_id=identify["session_id"],
                           face_count=len(faces), faces=summary)
        wanted = str(job.get("face_id") or "").strip()
        ids = [s["face_id"] for s in summary]
        if wanted and wanted not in ids:
            self._settle(scene_id, job, NEEDS_FACE_CHOICE,
                         f"face {wanted} is not one Kling found ({', '.join(ids)})")
            return
        if not wanted and len(ids) != 1:
            self._settle(scene_id, job, NEEDS_FACE_CHOICE,
                         f"Kling found {len(ids)} faces; one must be chosen explicitly (never "
                         "guessed)")
            return
        face_id = wanted or ids[0]

        rec = self.ledger.get(scene_id)
        before = int(rec.get("attempts") or 0)
        resend = job.get("resend_external_id")
        if resend:
            # The same attempt, under the same id: Kling refuses a duplicate id, so a resend of a
            # request that did reach Kling cannot be billed twice.
            attempt, external_id = before, str(resend)
        else:
            attempt = before + 1
            take = len(rec.get("history") or []) + 1
            external_id = (f"{self.project_dir.name}-{_safe(scene_id)[:36]}"
                           f"-t{take}-a{attempt}")[:64]
        inputs = {
            "session_id": identify["session_id"],
            "face_id": face_id,
            "sound_file_path": str(_inside(self.project_dir, job["audio_path"])),
            "sound_start_time": 0,
            "sound_end_time": job["audio_ms"],
            # Set explicitly: letting face detection pick the insert time can move the mouth
            # away from where the voice lands in the final mix.
            "sound_insert_time": job["insert_ms"],
            "sound_volume": 1.0,
            "original_audio_volume": 0.0,
            "external_task_id": external_id,
        }
        request = tool._build_advanced_request(inputs)
        if _STOP.is_set():
            self._not_now(scene_id)
            return
        for n in range(len(BUSY_BACKOFF_S) + 1):
            self.ledger.update(scene_id, status=SUBMITTING, face_id=face_id, attempts=attempt,
                               external_task_id=external_id, submitted_at=_now())
            self.ledger.add_cost(scene_id, ESTIMATE_LIPSYNC_USD)
            try:
                task_id = client.create_classic_task(request["path"], request["payload"])
                break
            except KlingAPIError as exc:
                if not _definitely_not_created(exc):
                    self.ledger.update(scene_id, status=UNKNOWN_SUBMISSION, reason=(
                        "the request may have reached Kling before the connection failed; it was "
                        "not resent so it cannot be paid twice"))
                    return
                self.ledger.add_cost(scene_id, -ESTIMATE_LIPSYNC_USD)   # refused: not billed
                if resend:
                    if _is_busy(exc):
                        self.ledger.update(scene_id, status=UNKNOWN_SUBMISSION, waiting=(
                            "Kling was busy, so the resend was not sent; run the same command "
                            "again"))
                    else:
                        self.ledger.update(scene_id, status=UNKNOWN_SUBMISSION, reason=(
                            "Kling refused the resend (it may already hold the first request): "
                            f"{exc}")[:300])
                    return
                if _is_busy(exc):
                    wait = BUSY_BACKOFF_S[n] if n < len(BUSY_BACKOFF_S) else None
                    if wait is not None and self.time_left() > wait + self.margin:
                        # Nothing was created: not "submitting" while waiting to try again.
                        self.ledger.update(scene_id, status=parked, attempts=before, note=(
                            "Kling is at its parallel-task limit; retrying"))
                        if not _STOP.wait(wait):
                            continue        # the same request is safe to send again
                    why = ("Kling is at its parallel-task limit; nothing was sent. Run the same "
                           "command again")
                    self.ledger.update(scene_id, status=parked, attempts=before, waiting=why,
                                       **({"reason": why} if parked == QUEUED else {"note": why}))
                    return
                self.ledger.update(scene_id, status=FAILED,
                                   reason=f"Kling refused the request: {exc}"[:300])
                return
        self.ledger.update(scene_id, status=SUBMITTED, task_id=str(task_id), reason=None,
                           submitted_epoch=time.time())
        self._poll_and_apply(scene_id)

    def _poll_and_apply(self, scene_id: str, job: Optional[dict[str, Any]] = None) -> None:
        from tools._kling.schemas import (CLASSIC_FAILURE_STATUS, CLASSIC_PENDING_STATUSES,
                                          CLASSIC_SUCCESS_STATUS)
        from tools.avatar.kling_lip_sync import KlingLipSync

        rec = self.ledger.get(scene_id)
        task_id = rec["task_id"]
        client = self.client_factory()
        while True:
            data = client.get(f"{LIPSYNC_PATH}/{task_id}")
            payload = data.get("data") or {}
            status = payload.get("task_status") or payload.get("status")
            if status == CLASSIC_SUCCESS_STATUS:
                videos = (payload.get("task_result") or {}).get("videos") or []
                break
            if status == CLASSIC_FAILURE_STATUS:
                msg = payload.get("task_status_msg") or payload.get("message") or "task failed"
                self.ledger.update(scene_id, status=FAILED, reason=f"Kling: {msg}"[:300])
                return
            if status not in CLASSIC_PENDING_STATUSES:
                self.ledger.update(scene_id, note=f"unexpected Kling status {status!r}")
                return
            if self.time_left() <= 0 or _STOP.wait(max(0.0, min(self.poll_interval,
                                                                 self.time_left()))):
                self.ledger.update(scene_id, note=(
                    "still processing; run the same command again to collect it (it is not "
                    "resubmitted)"))
                return
        if not videos:
            self.ledger.update(scene_id, status=FAILED, reason="Kling returned no video")
            return
        out = self.kdir / f"{Path(rec['clip_path']).stem}.kling-lipsync.mp4"
        tmp = out.with_name("_" + out.name + ".part")
        client.download(KlingLipSync._output_url(videos[0]), tmp)
        os.replace(tmp, out)
        latency = None
        if rec.get("submitted_epoch"):
            latency = round(time.time() - float(rec["submitted_epoch"]), 1)
        self.ledger.update(scene_id, kling_path=out.relative_to(self.project_dir).as_posix(),
                           kling_sha256=_sha256(out), finished_at=_now(), latency_s=latency)
        self._check_and_select(scene_id, job or {})

    def _check_and_select(self, scene_id: str, job: Optional[dict[str, Any]] = None) -> None:
        job = job or {}
        rec = self.ledger.get(scene_id)
        clip = _inside(self.project_dir, rec["clip_path"])
        kling = _inside(self.project_dir, rec["kling_path"])
        # The clip must still be the take Kling was given (or Kling's version of it, after an
        # interrupted apply). A regenerated or removed clip is never overwritten; the next run
        # starts a fresh take for it.
        if _clip_state(self.project_dir, rec) not in ("original", "kling"):
            self.ledger.update(scene_id, status=REJECTED_OUTPUT, reason=(
                "the clip changed or was removed while Kling was working"))
            return
        # A task collected on a later run must still match the customer's line: a line re-voiced
        # or re-timed while Kling was working would put a mouth for the old words on the clip.
        now = job.get("audio_now")
        if now is not None and rec.get("audio_sig") and not _same_audio(now, rec["audio_sig"]):
            self._set_aside_stale(scene_id, rec)
            return
        got = _probe(kling)
        self.ledger.update(scene_id, output={k: got.get(k) for k in (
            "has_video", "video_s", "duration_s", "width", "height")})
        # A file with no video stream, or one that does not decode, never replaces the clip.
        if not got["has_video"] or got["video_s"] <= 0 or not _video_decodes(kling):
            self.ledger.update(scene_id, status=REJECTED_OUTPUT, reason=(
                "Kling's file has no playable video stream"))
            return
        want = rec.get("clip_probe") or _probe(clip)
        got_s = got["video_s"]
        want_s = want.get("video_s") or want["duration_s"]
        if abs(got_s - want_s) > DURATION_TOLERANCE_S:
            self.ledger.update(scene_id, status=REJECTED_OUTPUT, reason=(
                f"Kling returned {got_s:.2f} s of video for a {want_s:.2f} s clip"))
            return
        if rec.get("speech_end_ms") and got_s * 1000 + COVER_TOLERANCE_MS < rec["speech_end_ms"]:
            self.ledger.update(scene_id, status=REJECTED_OUTPUT, reason=(
                "Kling's video ends before the customer finishes speaking"))
            return
        if want.get("width") and got.get("width"):
            a, b = want["width"] / want["height"], got["width"] / got["height"]
            if abs(a - b) / a > ASPECT_TOLERANCE:
                self.ledger.update(scene_id, status=REJECTED_OUTPUT,
                                   reason="Kling returned a different frame shape")
                return
        backup = self.kdir / f"{clip.stem}.original.mp4"
        if not backup.exists():
            if _sha256(clip) != rec.get("original_sha256"):
                self.ledger.update(scene_id, status=REJECTED_OUTPUT,
                                   reason="the clip changed while Kling was working")
                return
            shutil.copy2(clip, backup)
        if _sha256(backup) != rec.get("original_sha256"):
            self.ledger.update(scene_id, status=REJECTED_OUTPUT,
                               reason="the saved original does not match the clip Kling was given")
            return
        _replace(kling, clip)
        unconfirmed = job.get("audio_error")
        self.ledger.update(scene_id, status=DONE, selected="kling", reason=None,
                           original_backup=backup.relative_to(self.project_dir).as_posix(),
                           check_error=(f"it could not be confirmed that the customer's line is "
                                        f"unchanged: {unconfirmed}"[:300] if unconfirmed else None))

    def _set_aside_stale(self, scene_id: str, rec: dict[str, Any]) -> None:
        """Kling finished a take for words the customer no longer says: keep that result in
        history, keep (or put back) the original clip, and sync the new words on the next run."""
        if not _put_original_back(self.project_dir, rec):
            self.ledger.update(scene_id, status=DONE, selected="kling", check_error=(
                "the customer's line changed while Kling was working, and the saved original "
                "does not match, so nothing was changed"))
            return
        _supersede(self.project_dir, self.ledger, scene_id, rec,
                   "the customer's line changed while Kling was working")
        why = ("the customer's line changed while Kling was working, so that result was set "
               "aside; the new words are sent on the next run")
        self.ledger.update(scene_id, status=QUEUED, reason=why, waiting=why)


def _definitely_not_created(exc: Any) -> bool:
    """True only when Kling answered and refused: a 4xx, or a non-server business code on a 200.

    A 5xx, a server-side business code (5xxx) or a lost connection is ambiguous — the task may
    exist and be billed — so the caller records it as unknown and never resends it by itself.
    """
    status = getattr(exc, "http_status", None)
    code = "" if getattr(exc, "code", None) is None else str(exc.code)
    if status is not None:
        return 400 <= int(status) < 500
    return bool(code) and not code.startswith("5")


def _is_busy(exc: Any) -> bool:
    code = "" if getattr(exc, "code", None) is None else str(exc.code)
    return code in BUSY_CODES or getattr(exc, "http_status", None) == 429


def _archive(project_dir: Path, stem: str, tag: str) -> list[str]:
    """Move a superseded take's backup and Kling result into kling/history/ (never deleted)."""
    kdir = project_dir / KLING_DIR
    moved: list[str] = []
    for name in (f"{stem}.original.mp4", f"{stem}.kling-lipsync.mp4"):
        src = kdir / name
        if src.exists():
            dest = kdir / "history" / f"{tag}.{name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
            moved.append(dest.relative_to(project_dir).as_posix())
    return moved


def _supersede(project_dir: Path, ledger: Ledger, scene_id: str, rec: dict[str, Any],
               why: str) -> None:
    tag = f"{len(rec.get('history') or []) + 1:02d}-{str(rec.get('original_sha256') or 'old')[:12]}"
    stem = Path(str(rec.get("clip_path") or scene_id)).stem
    ledger.restart(scene_id, archived=_archive(project_dir, stem, tag), superseded_because=why)


def _clip_state(project_dir: Path, rec: dict[str, Any]) -> Optional[str]:
    """Which version the clip file holds right now: 'original', 'kling', 'replaced' or 'missing'."""
    try:
        clip = _inside(project_dir, str(rec.get("clip_path") or ""))
        if not clip.is_file():
            return "missing"
        sha = _sha256(clip)
    except (OSError, ValueError):
        return "missing"
    if rec.get("kling_sha256") and sha == rec["kling_sha256"]:
        return "kling"
    if sha == rec.get("original_sha256"):
        return "original"
    return "replaced"


def _put_original_back(project_dir: Path, rec: dict[str, Any]) -> bool:
    """If the clip holds Kling's version, copy the verified original back over it.
    True when the clip now holds the original; False when the saved original cannot be trusted."""
    if _clip_state(project_dir, rec) != "kling":
        return True
    clip = _inside(project_dir, str(rec["clip_path"]))
    backup = _inside(project_dir, str(rec.get("original_backup")
                                      or (KLING_DIR / f"{clip.stem}.original.mp4").as_posix()))
    if not backup.is_file() or _sha256(backup) != rec.get("original_sha256"):
        return False
    _replace(backup, clip)
    return True


def _replace(src: Path, dest: Path) -> None:
    tmp = dest.with_name("_" + dest.name + ".swap")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def run(project_dir: Path | str, *, request: Optional[dict[str, Any]] = None,
        default_speaker: str = "panda", max_usd: float = DEFAULT_MAX_USD,
        retry_failed: bool = False, resend_unknown: Iterable[str] = (),
        client_factory: Optional[Callable[..., Any]] = None,
        concurrency: Optional[int] = None, poll_interval: Optional[float] = None,
        timeout_s: Optional[float] = None) -> dict[str, Any]:
    """Run (or resume) the Kling pass for the scenes in the request. Returns the summary.

    ``timeout_s`` is how long this pass waits for Kling in total; what is still processing then is
    collected by the next run (never resubmitted).
    """
    project_dir = Path(project_dir).resolve()
    kdir = project_dir / KLING_DIR
    if request is None:
        request = _read_json(kdir / REQUEST_FILE)
    items = (request or {}).get("scenes") if isinstance(request, dict) else None
    if not isinstance(items, list):
        return {"status": "error", "error": f"no request: write {KLING_DIR / REQUEST_FILE} first"}
    factory = client_factory or _default_client_factory
    workers = int(concurrency or _env_float("KLING_LIPSYNC_CONCURRENCY", 2))
    workers = max(1, min(4, workers))
    poll = _env_float("KLING_LIPSYNC_POLL_S", 5.0) if poll_interval is None else poll_interval
    wait = _env_float("KLING_LIPSYNC_TIMEOUT_S", DEFAULT_WAIT_S) if timeout_s is None else timeout_s
    resend = {str(s).strip() for s in resend_unknown if str(s).strip()}

    with _run_lock(kdir) as mine:
        if not mine:
            return {"status": "busy",
                    "error": "another Kling pass is running for this job; nothing was sent"}
        try:
            ledger = Ledger(project_dir)
        except LedgerError as exc:
            return {"status": "error", "error": str(exc)}
        _STOP.clear()
        for sid, rec in list(ledger.data["scenes"].items()):
            if rec.get("waiting"):
                ledger.update(sid, waiting=None)
        script = _latest_artifact(project_dir, "script")
        plan = _latest_artifact(project_dir, "scene_plan")
        manifest_rows = _manifest_voice_rows(project_dir)
        worker = _Pass(project_dir, ledger, factory, poll, wait)
        resume: list[tuple[str, dict[str, Any]]] = []
        fresh: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        claimed: dict[str, str] = {}
        for _sid, _rec in ledger.data["scenes"].items():
            if _rec.get("clip_path") and _rec.get("status") in (DONE, SUBMITTED, SUBMITTING,
                                                                 UNKNOWN_SUBMISSION):
                claimed.setdefault(str(_rec["clip_path"]), _sid)

        def _lines_and_files(sid: str, item: dict[str, Any]) -> tuple[list[dict], list[Path], list]:
            if not (isinstance(script, dict) and isinstance(plan, dict)):
                raise Ineligible("the script or scene plan could not be read")
            try:
                lines = scene_lines(script, plan, sid, default_speaker)
            except (LookupError, KeyError, TypeError, ValueError) as exc:
                raise Ineligible(str(exc)[:200]) from exc
            ok, why = eligibility(lines)
            if not ok:
                raise Ineligible(why)
            cust = [ln for ln in lines if ln["speaker"] == "customer"]
            amap = item.get("audio") if isinstance(item.get("audio"), dict) else {}
            files = voice_files(project_dir, cust, amap, manifest_rows)
            return lines, files, _audio_sig(project_dir, cust, files)

        def _collect(sid: str, item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            """A task to collect, with the customer's line as it is now (checked before applying)."""
            try:
                _, _, now = _lines_and_files(sid, item)
                return sid, {"audio_now": now, "audio_error": None}
            except (Ineligible, OSError, ValueError) as exc:
                return sid, {"audio_now": None, "audio_error": str(exc)[:200]}

        for item in items:
            if not isinstance(item, dict) or not str(item.get("scene_id") or "").strip():
                continue
            sid = str(item["scene_id"]).strip()
            if sid in seen:
                ledger.update(sid, note="listed more than once in request.json; only the first "
                                        "entry was used")
                continue
            seen.add(sid)
            rec = ledger.get(sid)
            status = rec.get("status")

            clip: Optional[Path] = None
            clip_rel = ""
            try:
                clip = _inside(project_dir, str(item.get("clip_path") or ""))
                problem = "" if clip.is_file() else f"clip not found: {item.get('clip_path')}"
            except ValueError:
                problem = "clip_path is outside the project"
            if not problem and clip is not None:
                clip_rel = clip.relative_to(project_dir).as_posix()
                owner = claimed.setdefault(clip_rel, sid)
                if owner != sid:
                    problem = f"{clip_rel} is already requested for {owner}"

            if status == SUBMITTED and rec.get("task_id"):
                # Collect what was paid for, on the clip Kling was given, whatever the request
                # says now; a changed clip is handled on the run after it is collected. The same
                # take under a new name is followed first, so its paid result is not lost.
                if (not problem and clip is not None and clip_rel != rec.get("clip_path")
                        and _sha256(clip) == rec.get("original_sha256")):
                    ledger.update(sid, clip_path=clip_rel,
                                  note=f"the clip moved from {rec.get('clip_path')} to {clip_rel}")
                elif problem or clip_rel != rec.get("clip_path"):
                    ledger.update(sid, note=(problem or "clip_path differs from the clip Kling is "
                                             "working on; it is collected first"))
                resume.append(_collect(sid, item))
                continue
            if problem or clip is None:
                _park(ledger, sid, INELIGIBLE, problem or "clip_path is missing")
                continue

            sha = _sha256(clip)
            if rec.get("original_sha256") and sha not in (rec.get("original_sha256"),
                                                          rec.get("kling_sha256")):
                # A new take: the old take's files and record become history (spend stays counted).
                _supersede(project_dir, ledger, sid, rec, "the clip was regenerated")
                rec, status = ledger.get(sid), None
            elif rec.get("original_sha256") and clip_rel != rec.get("clip_path"):
                # The same take under a new name: follow the file and keep the record — a rename
                # must never reopen a paid, failed or maybe-sent take.
                ledger.update(sid, clip_path=clip_rel,
                              note=f"the clip moved from {rec.get('clip_path')} to {clip_rel}")
                rec = ledger.get(sid)

            if status == DONE:
                try:
                    _, _, sig = _lines_and_files(sid, item)
                except (Ineligible, OSError, ValueError) as exc:
                    ledger.update(sid, check_error=(
                        f"it could not be confirmed that the customer's line is unchanged: {exc}"
                    )[:300])
                    continue
                if not rec.get("audio_sig") or _same_audio(sig, rec.get("audio_sig")):
                    if rec.get("check_error"):
                        ledger.update(sid, check_error=None)
                    continue
                # The customer's line changed after Kling ran: put the untouched original back and
                # start a new take from it (the old Kling version goes to history).
                if not _put_original_back(project_dir, rec):
                    ledger.update(sid, check_error=(
                        "the customer's line changed after Kling ran, but the saved original "
                        "does not match, so nothing was changed"))
                    continue
                sha = _sha256(clip)
                _supersede(project_dir, ledger, sid, rec, "the customer's line changed")
                rec, status = ledger.get(sid), None

            if status == SUBMITTING:
                ledger.update(sid, status=UNKNOWN_SUBMISSION, reason=(
                    "a request may have been sent just before a restart; it was not resent so it "
                    "cannot be paid twice"))
                status = UNKNOWN_SUBMISSION
            resend_id = None
            if status == UNKNOWN_SUBMISSION:
                if sid not in resend or not rec.get("external_task_id"):
                    continue
                resend_id = rec["external_task_id"]
            if item.get("skip"):
                _park(ledger, sid, SKIPPED, f"not sent to Kling: {str(item['skip'])[:200]}",
                      clip_path=clip_rel)
                continue
            if status in RETRYABLE and not retry_failed:
                continue
            if status == NEEDS_FACE_CHOICE and not str(item.get("face_id") or "").strip():
                continue          # re-identifying costs money and would find the same faces
            attempts = int(rec.get("attempts") or 0)
            if resend_id is None and attempts >= MAX_ATTEMPTS:
                ledger.update(sid, note=f"already sent to Kling {attempts} times for this take")
                continue
            url = str(item.get("video_url") or "")
            if urlparse(url).scheme != "https":
                _park(ledger, sid, INELIGIBLE, "needs the clip's https Higgsfield link (video_url)",
                      clip_path=clip_rel)
                continue
            clip_probe = _probe(clip)
            if not clip_probe["has_video"] or clip_probe["video_s"] <= 0:
                _park(ledger, sid, INELIGIBLE, "the clip has no video stream", clip_path=clip_rel)
                continue
            try:
                lines, files, sig = _lines_and_files(sid, item)
                cust = [ln for ln in lines if ln["speaker"] == "customer"]
                audio = worker.build_audio(sid, cust, files, clip_probe["video_s"])
            except (Ineligible, OSError, RuntimeError, ValueError,
                    subprocess.SubprocessError) as exc:
                _park(ledger, sid, INELIGIBLE, str(exc)[:200], clip_path=clip_rel)
                continue
            ledger.update(sid, clip_path=clip_rel, video_url=url, original_sha256=sha,
                          clip_probe=clip_probe, audio_sig=sig, qa_offset_s=0.0, **audio,
                          lines=cust,
                          excluded_lines=[ln for ln in lines if ln["speaker"] != "customer"])
            fresh.append((sid, {"video_url": url, "face_id": item.get("face_id"),
                                "original_sha256": sha, "resend_external_id": resend_id,
                                **audio}))

        # A paid task still in flight is always collected, even if the request no longer lists it.
        for sid, rec in sorted(ledger.data["scenes"].items()):
            if sid not in seen and rec.get("status") == SUBMITTED and rec.get("task_id"):
                resume.append(_collect(sid, {}))

        if fresh and not os.environ.get("KLING_API_KEY"):
            for sid, _ in fresh:
                _park(ledger, sid, SKIPPED, "KLING_API_KEY is not set on the server")
            fresh = []
        wave = len(fresh) * (ESTIMATE_IDENTIFY_USD + ESTIMATE_LIPSYNC_USD)
        if fresh and ledger.spent_usd() + wave > max_usd + 1e-9:
            for sid, _ in fresh:
                _park(ledger, sid, BLOCKED_BUDGET, (
                    f"sending {len(fresh)} clip(s) (~${wave:.2f}) would pass the "
                    f"${max_usd:.2f} Kling budget (already ~${ledger.spent_usd():.2f})"))
            fresh = []

        jobs: list[tuple[str, dict[str, Any]]] = resume + fresh
        if jobs:
            worker.start_clock()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda j: worker.process(*j), jobs))
        return summary(project_dir, ledger=ledger, max_usd=max_usd,
                       default_speaker=default_speaker)


def summary(project_dir: Path | str, *, ledger: Optional[Ledger] = None,
            max_usd: Optional[float] = None, default_speaker: str = "panda") -> dict[str, Any]:
    project_dir = Path(project_dir).resolve()
    ledger = ledger or Ledger(project_dir)
    scenes = {}
    for sid, rec in sorted(ledger.data["scenes"].items()):
        scenes[sid] = {k: rec.get(k) for k in (
            "status", "reason", "note", "selected", "clip_path", "original_backup", "kling_path",
            "task_id", "face_id", "face_count", "attempts", "qa_audio_path", "qa_offset_s",
            "latency_s", "output", "check_error", "waiting") if rec.get(k) is not None}
        if rec.get("status") == DONE:
            # What the clip file holds now, not what the ledger last wrote.
            scenes[sid]["selected"] = _clip_state(project_dir, rec)
    candidates = customer_scenes(project_dir, default_speaker)
    pending = [sid for sid, rec in scenes.items()
               if rec.get("status") in (SUBMITTED, QUEUED) or rec.get("waiting")]
    return {
        "status": "ok",
        "provider": PROVIDER,
        "estimated_usd_spent": ledger.spent_usd(),
        "max_usd": max_usd,
        "run_again": bool(pending),
        "pending": pending,
        "scenes": scenes,
        "customer_scenes_not_requested": [s for s in candidates if s not in scenes],
    }


def select(project_dir: Path | str, scene_id: str, which: str) -> dict[str, Any]:
    """Put the original or Kling version back at the clip's own path (both are kept)."""
    project_dir = Path(project_dir).resolve()
    if which not in ("original", "kling"):
        return {"status": "error", "error": "choose original or kling"}
    with _run_lock(project_dir / KLING_DIR) as mine:
        if not mine:
            return {"status": "busy", "error": "a Kling pass is running for this job"}
        try:
            ledger = Ledger(project_dir)
        except LedgerError as exc:
            return {"status": "error", "error": str(exc)}
        rec = ledger.get(scene_id)
        if rec.get("status") != DONE:
            return {"status": "error", "error": f"{scene_id} has no finished Kling version"}
        clip = _inside(project_dir, rec["clip_path"])
        if not clip.is_file() or _sha256(clip) not in (rec.get("original_sha256"),
                                                        rec.get("kling_sha256")):
            return {"status": "error", "error": (
                f"{rec['clip_path']} is neither the original nor Kling's version (a new take?); "
                "nothing was changed — run the pass first")}
        src = _inside(project_dir, rec["original_backup" if which == "original" else "kling_path"])
        want = rec["original_sha256" if which == "original" else "kling_sha256"]
        if not src.is_file() or _sha256(src) != want:
            return {"status": "error", "error": f"the saved {which} file does not match the ledger"}
        _replace(src, clip)
        ledger.update(scene_id, selected=which)
        return {"status": "ok", "scene_id": scene_id, "selected": which}


def gate_notes(project_dir: Path | str, default_speaker: str = "panda") -> list[str]:
    """Plain-language outcome for the approve_assets question. Never raises."""
    try:
        project_dir = Path(project_dir)
        try:
            ledger = Ledger(project_dir)
        except LedgerError:
            return ["Kling's record (assets/video/kling/ledger.json) could not be read, so the "
                    "Kling outcome is unknown; check it before approving."]
        recs = ledger.data["scenes"]
        candidates = customer_scenes(project_dir, default_speaker)
        if not recs:
            return (["Kling lip-sync was switched on but did not run, so every clip keeps its "
                     "Seedance lip-sync."] if candidates else [])
        notes: list[str] = []
        for sid, rec in sorted(recs.items()):
            st, why = rec.get("status"), rec.get("reason") or rec.get("note") or ""
            on_disk = _clip_state(project_dir, rec) if st == DONE else None
            if on_disk == "kling" and rec.get("check_error"):
                notes.append(f"{sid}: Kling's version is in use, but {rec['check_error']}.")
            elif on_disk == "kling":
                notes.append(f"{sid}: the customer's lip-sync was redone by Kling "
                             "(the original clip is kept).")
            elif on_disk == "original":
                notes.append(f"{sid}: the original clip is used (Kling's version is kept but not "
                             "selected).")
            elif st == DONE:
                notes.append(f"{sid}: the clip was replaced after Kling ran, so Kling is not "
                             "applied to this take.")
            elif st == SUBMITTED:
                notes.append(f"{sid}: Kling is still processing; the original clip is shown.")
            elif st in PROTECTED and rec.get("waiting"):
                again = "resend" if st == UNKNOWN_SUBMISSION else "retry"
                notes.append(f"{sid}: original clip kept — {why or st}. The requested {again} was "
                             f"not sent yet ({rec['waiting']}).")
            elif st == QUEUED or rec.get("waiting"):
                notes.append(f"{sid}: not sent to Kling yet ({rec.get('waiting') or why}); the "
                             "original clip is shown.")
            elif st is None:
                if sid in candidates:
                    notes.append(f"{sid}: the customer speaks on screen but the clip was not "
                                 "sent to Kling.")
            elif st != INELIGIBLE or sid in candidates:
                notes.append(f"{sid}: original clip kept — {why or st}.")
        for sid in candidates:
            if sid not in recs:
                notes.append(f"{sid}: the customer speaks on screen but the clip was not sent "
                             "to Kling.")
        return notes[:12]
    except Exception:  # noqa: BLE001 — a note must never break the gate
        return []


def _stop_on_signal() -> None:
    """SIGTERM/SIGHUP (a timed-out command): stop polling, release the lock, keep every task id."""
    def _handler(signum: int, _frame: Any) -> None:
        if _STOP.is_set():
            return      # already stopping: a repeated signal must not cut the shutdown short
        _STOP.set()
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lib.kling_lipsync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("project_dir")
    r.add_argument("--request")
    r.add_argument("--default-speaker", default="panda")
    r.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    r.add_argument("--retry-failed", action="store_true")
    r.add_argument("--resend-unknown", action="append", default=[], metavar="SCENE_ID")
    s = sub.add_parser("select")
    s.add_argument("project_dir")
    s.add_argument("scene_id")
    s.add_argument("which", choices=["original", "kling"])
    st = sub.add_parser("status")
    st.add_argument("project_dir")
    st.add_argument("--default-speaker", default="panda")
    a = ap.parse_args(argv)
    _stop_on_signal()
    try:
        if a.cmd == "run":
            req = _read_json(Path(a.request)) if a.request else None
            out = run(a.project_dir, request=req, default_speaker=a.default_speaker,
                      max_usd=a.max_usd, retry_failed=a.retry_failed,
                      resend_unknown=a.resend_unknown)
        elif a.cmd == "select":
            out = select(a.project_dir, a.scene_id, a.which)
        else:
            out = summary(a.project_dir, default_speaker=a.default_speaker)
    except LedgerError as exc:
        out = {"status": "error", "error": str(exc)}
    # ASCII-safe: a non-UTF-8 stdout must never lose the summary the agent copies.
    print(json.dumps(out, ensure_ascii=True, indent=1))
    return 0 if out.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
