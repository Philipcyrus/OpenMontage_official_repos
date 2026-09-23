#!/usr/bin/env python3
"""One real Kling customer lip-sync trial on a COPY of an existing panda-video job.

It runs this repo's own ``python -m lib.kling_lipsync run`` on one scene and records what the unit
tests cannot: whether the real Kling API accepts what the lib sends (API compatibility), how long
it takes (timing), what the result looks like next to the original (visual quality), and what the
Kling account actually consumed (usage).

Safety:
- the job folder you name is only READ; everything runs on a copy under --work;
- nothing is sent unless the scene passes the lib's own checks; --max-usd caps the spend
  (default 1, one scene is ~$0.34 estimated);
- the Kling key is read from the environment or --env-file (KLING_* lines only) and never printed;
- --dry-run does every free step (copy, clip + link check, eligibility, voice file, account read)
  and sends nothing to lip-sync.

Usage (run from a checkout of the branch under test, never from the production checkout)::

    python scripts/kling_lipsync_trial.py --job-dir <projects>/<job_id> --scene scene-04 \\
        --env-file <production>/.env --default-speaker panda --dry-run
    python scripts/kling_lipsync_trial.py --job-dir <projects>/<job_id> --scene scene-04 \\
        --env-file <production>/.env --default-speaker panda
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KLING_ENV_KEYS = ("KLING_API_KEY", "KLING_API_BASE_URL")
URL_RE = re.compile(r"https://[^\s\"'<>\\)]+?\.mp4(?:\?[^\s\"'<>\\)]*)?")
TEXT_SUFFIXES = (".json", ".jsonl", ".log", ".md", ".txt")


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def read_kling_env(env_file: Optional[Path]) -> dict[str, str]:
    """KLING_* values from the environment, else from a .env file. Nothing else is read."""
    found = {k: os.environ[k] for k in KLING_ENV_KEYS if os.environ.get(k)}
    if env_file and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*)$", line)
            if not m or m.group(1) not in KLING_ENV_KEYS or m.group(1) in found:
                continue
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].strip()
            if value:
                found[m.group(1)] = value
    return found


def _manifest(job_dir: Path) -> dict[str, Any]:
    from lib.kling_lipsync import _latest_artifact
    return _latest_artifact(job_dir, "asset_manifest", stage="assets") or {}


def find_clip(job_dir: Path, scene: str) -> tuple[Path, Optional[str]]:
    """The scene's clip (and its recorded CDN link, if the manifest has one)."""
    rows = [a for a in _manifest(job_dir).get("assets") or []
            if isinstance(a, dict) and a.get("type") == "video" and str(a.get("scene_id")) == scene
            and a.get("path") and (job_dir / str(a["path"])).is_file()]
    if len(rows) == 1:
        return job_dir / str(rows[0]["path"]), rows[0].get("original_url") or None
    if len(rows) > 1:
        raise SystemExit(f"{len(rows)} video rows for {scene} in the asset manifest "
                         f"({[r['path'] for r in rows]}); pass --clip")
    guess = job_dir / "assets" / "video" / f"{scene}.mp4"
    if guess.is_file():
        return guess, None
    raise SystemExit(f"no clip found for {scene}; pass --clip")


def candidate_urls(job_dir: Path, limit: int = 80) -> list[str]:
    """Every https .mp4 link written anywhere in the job's text files, first seen first."""
    seen: dict[str, None] = {}
    for p in sorted(job_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if p.stat().st_size > 20 * 1024 * 1024:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for url in URL_RE.findall(text):
            seen.setdefault(url.rstrip(".,;"), None)
            if len(seen) >= limit:
                return list(seen)
    return list(seen)


def match_url(urls: Iterable[str], clip_sha: str,
              fetch_sha: Callable[[str], Optional[str]]) -> tuple[Optional[str], int]:
    """The first link whose bytes are exactly the clip (free downloads). Returns (url, tried)."""
    tried = 0
    for url in urls:
        tried += 1
        if fetch_sha(url) == clip_sha:
            return url, tried
    return None, tried


def usage_delta(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Remaining quantity per Kling resource pack, before and after."""
    def packs(u: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out = {}
        for p in (u or {}).get("resource_pack_subscribe_infos") or []:
            if isinstance(p, dict):
                key = str(p.get("resource_pack_id") or p.get("resource_pack_name") or len(out))
                out[key] = p
        return out
    b, a = packs(before), packs(after)
    rows = []
    for key in sorted(set(b) | set(a)):
        rb, ra = b.get(key) or {}, a.get(key) or {}
        before_q, after_q = rb.get("remaining_quantity"), ra.get("remaining_quantity")
        used = None
        try:
            used = round(float(before_q) - float(after_q), 4)
        except (TypeError, ValueError):
            pass
        rows.append({"pack": ra.get("resource_pack_name") or rb.get("resource_pack_name") or key,
                     "type": ra.get("resource_pack_type") or rb.get("resource_pack_type"),
                     "remaining_before": before_q, "remaining_after": after_q, "used": used})
    return rows


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_sha(url: str) -> Optional[str]:
    import requests
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            if r.status_code != 200:
                return None
            h, size = hashlib.sha256(), 0
            for chunk in r.iter_content(1 << 20):
                h.update(chunk)
                size += len(chunk)
                if size > 500 * 1024 * 1024:
                    return None
            return h.hexdigest()
    except requests.RequestException:
        return None


def _short(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    from urllib.parse import urlparse
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}/…/{u.path.rsplit('/', 1)[-1]}"


def _account_usage() -> dict[str, Any]:
    from tools._kling.account import get_account_costs, reset_account_usage_cache
    reset_account_usage_cache()
    end = int(time.time() * 1000)
    start = end - 30 * 24 * 3600 * 1000
    try:
        u = get_account_costs(start_time=str(start), end_time=str(end))
    except Exception as exc:  # noqa: BLE001 — a read-only diagnostic; report it, never crash
        try:
            reset_account_usage_cache()
            u = get_account_costs()
        except Exception as exc2:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc} / {type(exc2).__name__}: {exc2}"[:500]}
    return {"resource_pack_subscribe_infos": u.get("resource_pack_subscribe_infos") or [],
            "throttle_status": u.get("throttle_status")}


def _probe_full(path: Path) -> dict[str, Any]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration,bit_rate:stream=codec_type,codec_name,width,height,"
                        "r_frame_rate,nb_frames,pix_fmt,duration,bit_rate",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


def _similarity(kling: Path, original: Path) -> dict[str, Any]:
    """SSIM / PSNR of Kling's frames against the original's (1.0 / high dB = untouched)."""
    info = _probe_full(original)
    v = next((s for s in info.get("streams") or [] if s.get("codec_type") == "video"), {})
    w, h = v.get("width"), v.get("height")
    out: dict[str, Any] = {}
    for name, flt, pat in (("ssim", "ssim", r"All:([0-9.]+)"),
                           ("psnr_db", "psnr", r"average:([0-9.inf]+)")):
        graph = f"[0:v]scale={w}:{h},setsar=1[a];[1:v]setsar=1[b];[a][b]{flt}"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(kling), "-i",
                            str(original), "-lavfi", graph, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=600)
        m = re.findall(pat, r.stderr or "")
        out[name] = m[-1] if m else None
    return out


def _qa(video: Path, bed: Path, out_dir: Path, scene: str) -> dict[str, Any]:
    from tools.analysis.lipsync_qa import LipSyncQA
    res = LipSyncQA().execute({"video_path": str(video), "audio_path": str(bed),
                               "scene_id": scene, "output_dir": str(out_dir),
                               "expected_audio_offset_seconds": 0, "max_samples": 12})
    return res.data if res.success else {"error": res.error}


def _contact_sheet(original: Path, kling: Path, times: list[float], out: Path) -> Optional[Path]:
    """One row per sampled speech time: original on the left, Kling on the right."""
    if not times:
        return None
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times[:10]):
            row = Path(td) / f"row_{i:02d}.png"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(original),
                                "-ss", f"{t:.3f}", "-i", str(kling), "-filter_complex",
                                "[0:v]scale=-2:480,setsar=1[a];[1:v]scale=-2:480,setsar=1[b];"
                                "[a][b]hstack=inputs=2", "-frames:v", "1", str(row)],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and row.is_file():
                rows.append(row)
        if not rows:
            return None
        cmd = ["ffmpeg", "-v", "error", "-y"]
        for row in rows:
            cmd += ["-i", str(row)]
        cmd += ["-filter_complex", f"vstack=inputs={len(rows)}" if len(rows) > 1 else "null",
                str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return out if r.returncode == 0 and out.is_file() else None


def _side_by_side(original: Path, kling: Path, bed: Path, seconds: float, out: Path) -> Optional[Path]:
    """Original | Kling, with the customer's line laid at its clip time."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(original), "-i", str(kling),
                        "-i", str(bed), "-filter_complex",
                        "[0:v]scale=-2:720,setsar=1[a];[1:v]scale=-2:720,setsar=1[b];"
                        "[a][b]hstack=inputs=2[v];[2:a]apad[s]", "-map", "[v]", "-map", "[s]",
                        "-t", f"{seconds:.3f}", "-c:v", "libx264", "-crf", "23", "-preset",
                        "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
                       capture_output=True, text=True, timeout=600)
    return out if r.returncode == 0 and out.is_file() else None


def _run_pass(copy: Path, request: Path, speaker: str, max_usd: float,
              env: dict[str, str]) -> tuple[dict[str, Any], float, int]:
    started = time.time()
    r = subprocess.run([sys.executable, "-m", "lib.kling_lipsync", "run", str(copy),
                        "--request", str(request), "--default-speaker", speaker,
                        "--max-usd", f"{max_usd:g}"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {"status": "error", "error": (r.stderr or r.stdout)[-800:]}
    return out, round(time.time() - started, 1), r.returncode


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--job-dir", required=True, type=Path)
    ap.add_argument("--scene", default="scene-04")
    ap.add_argument("--work", type=Path, default=Path.home() / "kling-trial")
    ap.add_argument("--default-speaker", default="panda",
                    help="the job's options.narrator (who speaks a line with no speaker)")
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--clip", type=Path, help="the scene's clip, if the manifest is ambiguous")
    ap.add_argument("--video-url", help="the clip's Higgsfield CDN link, if it cannot be found")
    ap.add_argument("--env-file", type=Path, help="read KLING_API_KEY / KLING_API_BASE_URL from it")
    ap.add_argument("--max-runs", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true", help="free steps only; nothing is sent")
    a = ap.parse_args(argv)

    job_dir = a.job_dir.resolve()
    if not (job_dir / "checkpoint_scene_plan.json").exists() and not (
            job_dir / "artifacts" / "scene_plan.json").exists():
        raise SystemExit(f"{job_dir} does not look like a panda-video job folder")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trial = (a.work.expanduser().resolve() / f"{job_dir.name}-{a.scene}-{stamp}")
    if job_dir in trial.parents or trial == job_dir or job_dir.parent in trial.parents:
        raise SystemExit("--work must be outside the job's projects folder")
    copy = trial / job_dir.name
    evidence = trial / "evidence"
    evidence.mkdir(parents=True)
    report: dict[str, Any] = {"job": job_dir.name, "scene": a.scene, "trial_dir": str(trial),
                              "code": _git_head(), "dry_run": a.dry_run, "started_at": stamp}

    # 1. copy (the original job folder is only read)
    shutil.copytree(job_dir, copy, ignore=shutil.ignore_patterns("renders", "lipsync_qa"))

    # 2. the clip, and its CDN link (proved by bytes, never assumed)
    clip, url = (a.clip.resolve(), None) if a.clip else find_clip(job_dir, a.scene)
    try:
        rel = clip.relative_to(job_dir).as_posix()
    except ValueError:
        raise SystemExit("--clip must be inside the job folder") from None
    clip_sha = _sha(clip)
    how = "flag" if a.video_url else ("manifest" if url else "search")
    tried = 0
    if a.video_url:
        url = a.video_url
    if url and _fetch_sha(url) != clip_sha:
        raise SystemExit(f"the link from the {how} is not this clip's bytes; nothing was sent")
    if not url:
        url, tried = match_url(candidate_urls(job_dir), clip_sha, _fetch_sha)
        if not url:
            raise SystemExit(f"none of the {tried} .mp4 links in the job folder is this clip; pass "
                             "--video-url (nothing was sent)")
    report["clip"] = {"path": rel, "sha256": clip_sha[:16], "link_found_by": how,
                      "links_checked": tried, "link": _short(url)}

    # 3. who speaks, which voice file (the lib's own rules, free)
    from lib import kling_lipsync as kl
    script = kl._latest_artifact(copy, "script") or {}
    plan = kl._latest_artifact(copy, "scene_plan") or {}
    try:
        lines = kl.scene_lines(script, plan, a.scene, a.default_speaker)
        ok, why = kl.eligibility(lines)
    except (LookupError, KeyError, TypeError, ValueError) as exc:
        lines, ok, why = [], False, f"the scene plan cannot place {a.scene}: {exc}"
    report["lines"] = lines
    report["eligible"] = ok if ok else why
    if ok:
        cust = [ln for ln in lines if ln["speaker"] == "customer"]
        try:
            files = kl.voice_files(copy, cust, {}, kl._manifest_voice_rows(copy))
            report["voice_files"] = [f.relative_to(copy).as_posix() for f in files]
        except kl.Ineligible as exc:
            report["voice_files"] = f"refused: {exc}"
    request = trial / "request.json"
    request.write_text(json.dumps({"scenes": [{"scene_id": a.scene, "clip_path": rel,
                                               "video_url": url}]}, indent=1), encoding="utf-8")

    # 4. the key (never printed) and a free, read-only account call
    kenv = read_kling_env(a.env_file)
    report["kling_key"] = "present" if kenv.get("KLING_API_KEY") else "MISSING"
    env = {**os.environ, **kenv, "KLING_LIPSYNC_TIMEOUT_S": "540"}
    if kenv.get("KLING_API_KEY"):
        os.environ.update(kenv)
        report["usage_before"] = _account_usage()
    if a.dry_run or not kenv.get("KLING_API_KEY") or not ok:
        report["result"] = ("dry run: nothing sent" if a.dry_run else
                            "not sent: " + ("KLING_API_KEY missing" if ok else why))
        return _finish(report, trial)

    # 5. the PR's actual command, re-run while it says run_again (it never pays twice)
    runs = []
    for _ in range(max(1, a.max_runs)):
        out, secs, rc = _run_pass(copy, request, a.default_speaker, a.max_usd, env)
        runs.append({"seconds": secs, "exit": rc, "status": out.get("status"),
                     "run_again": out.get("run_again"),
                     "scene": (out.get("scenes") or {}).get(a.scene)})
        if not out.get("run_again"):
            break
    report["runs"] = runs
    report["summary"] = out
    ledger = json.loads((copy / kl.KLING_DIR / kl.LEDGER_FILE).read_text(encoding="utf-8"))
    rec = (ledger.get("scenes") or {}).get(a.scene) or {}
    (evidence / "ledger.json").write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    faces = copy / kl.KLING_DIR / f"{kl._safe(a.scene)}.faces.json"
    if faces.is_file():
        shutil.copy2(faces, evidence / "faces.json")
    report["kling"] = {k: rec.get(k) for k in (
        "status", "reason", "note", "face_count", "faces", "task_id", "external_task_id",
        "attempts", "insert_ms", "audio_ms", "speech_end_ms", "latency_s", "output",
        "estimated_usd", "check_error")}

    # 6. actual usage (Kling's own account numbers; they can lag a little)
    time.sleep(60)
    report["usage_after"] = _account_usage()
    report["usage_used"] = usage_delta(report.get("usage_before") or {}, report["usage_after"])

    # 7. evidence for eyes: frames at the same speech times, side by side, and similarity
    if rec.get("status") == kl.DONE and rec.get("original_backup") and rec.get("kling_path"):
        original, kling = copy / rec["original_backup"], copy / rec["kling_path"]
        bed = copy / rec["qa_audio_path"]
        qa_o = _qa(original, bed, evidence / "qa_original", a.scene)
        qa_k = _qa(kling, bed, evidence / "qa_kling", a.scene)
        report["qa"] = {name: {k: q.get(k) for k in ("status", "reason", "speech_onset_seconds",
                                                    "speech_end_seconds", "sample_timestamps",
                                                    "error")}
                        for name, q in (("original", qa_o), ("kling", qa_k))}
        times = qa_k.get("sample_timestamps") or qa_o.get("sample_timestamps") or []
        sheet = _contact_sheet(original, kling, times, evidence / "contact_original_vs_kling.png")
        video = _side_by_side(original, kling, bed, float(rec["clip_probe"]["video_s"]),
                              evidence / "side_by_side.mp4")
        report["visual"] = {"probe_original": _probe_full(original),
                            "probe_kling": _probe_full(kling),
                            "similarity_kling_vs_original": _similarity(kling, original),
                            "contact_sheet": sheet and str(sheet),
                            "side_by_side": video and str(video)}
    return _finish(report, trial)


def _git_head() -> Optional[str]:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def _finish(report: dict[str, Any], trial: Path) -> int:
    path = trial / "report.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=True, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=True, default=str))
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
