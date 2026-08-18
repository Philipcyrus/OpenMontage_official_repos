"""Static Backlot-style storyboard for Dify's file-preview slot at approve_stills.

Writes storyboard.html + storyboard.png from artifacts.stills × scene_plan.scenes.
Does not start the live Backlot server. Does not put those files in artifacts.stills.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from dify_launcher import store

STORYBOARD_PNG = "storyboard.png"
STORYBOARD_HTML = "storyboard.html"

_BG = (10, 10, 12)
_SURFACE = (22, 22, 26)
_TEXT = (236, 236, 239)
_TEXT2 = (160, 160, 169)
_TEXT3 = (95, 95, 104)
_BORDER = (35, 35, 41)
_PAD = 24
_GAP = 12
_THUMB_W = 220
_SLATE_H = 22
_LINE_H = 18
_CAPTION_PAD = 8
_MAX_ROW_W = 1400
_MAX_CAPTION_LINES = 8


def is_storyboard_name(name: Any) -> bool:
    n = Path(str(name)).name.lower()
    return "storyboard" in n


def still_basenames(arts: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in arts.get("stills") or []:
        name = Path(str(s)).name
        if name and not is_storyboard_name(name) and name not in out:
            out.append(name)
    return out


def _scene_label(sid: str, index: int) -> str:
    m = re.search(r"(\d+)\s*$", str(sid or ""))
    if m:
        return f"SC {int(m.group(1)):02d}"
    return f"SC {index + 1:02d}"


def cards_from_arts(arts: dict[str, Any]) -> list[dict[str, Any]]:
    """Zip stills[i] with scene_plan.scenes[i] (stills gate often has no asset_manifest)."""
    stills = still_basenames(arts)
    plan = arts.get("scene_plan") if isinstance(arts.get("scene_plan"), dict) else {}
    scenes = list(plan.get("scenes") or []) if isinstance(plan, dict) else []
    n = max(len(stills), len(scenes), 0)
    cards: list[dict[str, Any]] = []
    for i in range(n):
        sc = scenes[i] if i < len(scenes) and isinstance(scenes[i], dict) else {}
        still = stills[i] if i < len(stills) else None
        sid = str(sc.get("id") or f"scene-{i + 1}")
        start, end = sc.get("start_seconds"), sc.get("end_seconds")
        dur = None
        if start is not None and end is not None:
            try:
                dur = max(0.0, float(end) - float(start))
            except (TypeError, ValueError):
                dur = None
        caps = sc.get("captions") if isinstance(sc.get("captions"), dict) else {}
        cap_parts = []
        for key in ("zh", "en"):
            raw = str(caps.get(key) or "").strip()
            if raw:
                cap_parts.append(raw)
        desc = "\n".join(cap_parts) if cap_parts else str(
            sc.get("description") or sc.get("shot_intent") or "").strip()
        shot = sc.get("shot_language") if isinstance(sc.get("shot_language"), dict) else {}
        cards.append({
            "id": sid,
            "label": _scene_label(sid, i),
            "still": still,
            "description": desc,
            "framing": sc.get("framing") or shot.get("shot_size"),
            "movement": sc.get("movement") or shot.get("camera_movement"),
            "duration_seconds": dur,
            "hero_moment": bool(sc.get("hero_moment")),
        })
    return cards


def _thumb_size(job_id: str, still: Optional[str]) -> tuple[int, int]:
    """Keep the still's aspect (9:16 / 4:5 / 1:1); do not force Backlot's 16:9."""
    if still:
        p = store.artifact_path(job_id, still)
        try:
            with Image.open(p) as im:
                w, h = im.size
            if w > 0 and h > 0:
                tw = _THUMB_W
                th = max(80, round(tw * h / w))
                return tw, min(th, 360)
        except OSError:
            pass
    return _THUMB_W, 280


_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_FONT_CANDIDATES = (
    _ENGINE_ROOT / "vendor" / "brand" / "fonts" / "msyhbd.ttc",  # Noto Sans SC (CJK + Latin)
    Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
)


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
    try:
        return draw.textlength(text, font=font)
    except Exception:
        return len(text) * 6


def _wrap_para(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
               max_w: int) -> list[str]:
    """Wrap mixed zh/en by glyph so CJK isn't one unbreakable token."""
    raw = (text or "").replace("\n", " ").strip()
    if not raw:
        return []
    lines: list[str] = []
    cur = ""
    for ch in raw:
        trial = cur + ch
        if _text_width(draw, trial, font) <= max_w or not cur:
            cur = trial
            continue
        lines.append(cur.rstrip())
        cur = "" if ch == " " else ch
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
                max_w: int, max_lines: int = _MAX_CAPTION_LINES) -> list[str]:
    out: list[str] = []
    for para in (text or "").splitlines() or [""]:
        wrapped = _wrap_para(draw, para, font, max_w)
        if not wrapped and para.strip():
            wrapped = [para.strip()]
        out.extend(wrapped)
        if len(out) >= max_lines:
            return out[:max_lines]
    return out[:max_lines]


def _caption_block(draw: ImageDraw.ImageDraw, card: dict[str, Any],
                   font_c: ImageFont.ImageFont, inner_w: int) -> tuple[str, list[str]]:
    chips = " · ".join(str(b) for b in (card.get("framing"), card.get("movement")) if b)
    lines = _wrap_lines(draw, card.get("description") or "", font_c, inner_w)
    return chips, lines


def _caption_height(chips: str, lines: list[str]) -> int:
    h = _CAPTION_PAD
    if chips:
        h += _LINE_H
    h += _LINE_H * len(lines)
    h += _CAPTION_PAD + 12  # descent + bottom inset so the last line isn't clipped
    return max(h, _CAPTION_PAD * 2 + _LINE_H)


def write_storyboard_png(job_id: str, cards: list[dict[str, Any]]) -> None:
    store.ensure_job(job_id)
    font_s = _font(12)
    font_c = _font(11)
    if not cards:
        Image.new("RGB", (400, 120), _BG).save(store.artifact_path(job_id, STORYBOARD_PNG))
        return

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1), _BG))
    sizes = [_thumb_size(job_id, c.get("still")) for c in cards]
    card_ws = [max(tw, 240) for tw, _th in sizes]
    blocks = [_caption_block(probe, cards[i], font_c, card_ws[i] - 12) for i in range(len(cards))]
    card_hs = [_SLATE_H + sizes[i][1] + _caption_height(*blocks[i]) for i in range(len(cards))]

    rows: list[list[int]] = [[]]
    row_w = _PAD
    for i, cw in enumerate(card_ws):
        need = cw + (_GAP if rows[-1] else 0)
        if rows[-1] and row_w + need + _PAD > _MAX_ROW_W:
            rows.append([i])
            row_w = _PAD + cw
        else:
            rows[-1].append(i)
            row_w += need
    canvas_w = _PAD
    for row in rows:
        rw = _PAD + sum(card_ws[i] for i in row) + _GAP * max(0, len(row) - 1) + _PAD
        canvas_w = max(canvas_w, rw)
    canvas_h = _PAD
    row_heights = []
    for row in rows:
        rh = max(card_hs[i] for i in row)
        row_heights.append(rh)
        canvas_h += rh + _GAP
    canvas_h = canvas_h - _GAP + _PAD

    img = Image.new("RGB", (canvas_w, canvas_h), _BG)
    draw = ImageDraw.Draw(img)
    y = _PAD
    for row, rh in zip(rows, row_heights):
        x = _PAD
        for i in row:
            tw, th = sizes[i]
            cw = card_ws[i]
            card = cards[i]
            chips, lines = blocks[i]
            draw.rounded_rectangle([x, y, x + cw, y + rh], radius=6, fill=_SURFACE,
                                   outline=_BORDER)
            slate = card["label"]
            if card.get("duration_seconds") is not None:
                slate += f"  {card['duration_seconds']:.0f}s"
            if card.get("hero_moment"):
                slate += "  HERO"
            draw.text((x + 6, y + 4), slate, fill=_TEXT2, font=font_s)
            ty = y + _SLATE_H
            still = card.get("still")
            if still:
                sp = store.artifact_path(job_id, still)
                try:
                    with Image.open(sp) as src:
                        thumb = src.convert("RGB").resize((tw, th), Image.Resampling.LANCZOS)
                    img.paste(thumb, (x + (cw - tw) // 2, ty))
                except OSError:
                    draw.rectangle([x + 4, ty, x + cw - 4, ty + th], outline=_BORDER)
            cap_y = ty + th + _CAPTION_PAD
            if chips:
                draw.text((x + 6, cap_y), chips, fill=_TEXT2, font=font_s)
                cap_y += _LINE_H
            for line in lines:
                draw.text((x + 6, cap_y), line, fill=_TEXT, font=font_c)
                cap_y += _LINE_H
            x += cw + _GAP
        y += rh + _GAP
    img.save(store.artifact_path(job_id, STORYBOARD_PNG), "PNG")


def write_storyboard_html(job_id: str, cards: list[dict[str, Any]]) -> None:
    store.ensure_job(job_id)
    cells = []
    for c in cards:
        still = c.get("still")
        img = (f'<div class="thumb"><img src="{html.escape(still)}" alt=""></div>'
               if still else '<div class="thumb spec"></div>')
        dur = f'<span class="dur">{c["duration_seconds"]:.0f}s</span>' if c.get("duration_seconds") is not None else ""
        hero = '<span class="hero">HERO</span>' if c.get("hero_moment") else ""
        shot = " · ".join(str(b) for b in (c.get("framing"), c.get("movement")) if b)
        chips = f'<div class="shotchips">{html.escape(shot)}</div>' if shot else ""
        desc = html.escape(c.get("description") or "").replace("\n", "<br>")
        cells.append(
            f'<div class="scene-card">'
            f'<div class="sc-slate"><span class="num">{html.escape(c["label"])}</span>'
            f'{hero}{dur}</div>{img}{chips}'
            f'<div class="narr">{desc}</div></div>'
        )
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Storyboard</title>
<style>
:root {{ --bg:#0a0a0c; --surface:#101013; --surface-2:#16161a; --border:#232329;
  --text:#ececef; --text-2:#a0a0a9; --text-3:#5f5f68; --amber:#f0a83c; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font-family: Inter, -apple-system, sans-serif; }}
.filmstrip {{ display:flex; flex-wrap:wrap; gap:12px; padding:26px 16px; }}
.scene-card {{ flex:none; width:min(220px, 42vw); display:flex; flex-direction:column; }}
.sc-slate {{ display:flex; align-items:baseline; gap:8px; font-size:10px;
  letter-spacing:.05em; color:var(--text-3); padding:0 2px 6px; font-family: ui-monospace, monospace; }}
.sc-slate .num {{ color:var(--text-2); font-weight:600; }}
.sc-slate .dur {{ margin-left:auto; }}
.sc-slate .hero {{ color:var(--amber); }}
.thumb {{ border-radius:7px; overflow:hidden; background:var(--surface-2);
  border:1px solid var(--border); }}
.thumb img {{ width:100%; height:auto; display:block; object-fit:cover; }}
.shotchips {{ font-family: ui-monospace, monospace; font-size:9px; color:#62626c;
  padding:7px 2px 0; }}
.narr {{ padding:8px 3px 0; font-size:11px; color:var(--text-2); line-height:1.45;
  font-style:italic; }}
</style></head>
<body>
<div class="section-title" style="padding:16px 16px 0;font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--text-3)">Storyboard
  <span style="margin-left:8px">{len(cards)} scenes</span></div>
<div class="filmstrip">{"".join(cells)}</div>
</body></html>
"""
    store.artifact_path(job_id, STORYBOARD_HTML).write_text(doc, encoding="utf-8")


def apply_storyboard_preview(job_id: str, arts: dict[str, Any],
                             gate: Optional[str] = None) -> dict[str, Any]:
    """At approve_stills, write the composite and set preview. Other gates drop preview."""
    stills = still_basenames(arts)
    if gate == "approve_stills" and stills:
        store.ensure_job(job_id)
        cards = cards_from_arts(arts)
        write_storyboard_html(job_id, cards)
        write_storyboard_png(job_id, cards)
        arts["storyboard_html"] = STORYBOARD_HTML
        arts["preview"] = [STORYBOARD_PNG]
        # never list the composite as a scene still
        arts["stills"] = stills
    else:
        arts.pop("preview", None)
    return arts
