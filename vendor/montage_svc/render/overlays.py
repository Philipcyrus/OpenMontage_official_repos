"""PIL overlay rendering — captions, bubbles, callouts, logo, intro/outro cards.

Refactored from projects/panda-mobile-going-home/artifacts/generate_overlays.py.
That script drew one specific ad: fixed strings, fixed colors, a fixed output
folder. Here the same drawing code is parameterised by (profile, text, frame
size) and returns a path, so it can serve any run.

Text is drawn here, never generated — this is why the pipeline can promise
correct Chinese. Image models can't be trusted with CJK glyphs; PIL can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from montage_svc.config import resolve_font
from montage_svc.storage import brand_file

RGBA = tuple[int, int, int, int]

ACCENTS: dict[str, RGBA] = {
    "red": (216, 50, 50, 255),
    "green": (28, 165, 86, 255),
    "yellow": (253, 197, 13, 255),
    "black": (17, 17, 17, 255),
}


def _rgba(v: Any, fallback: RGBA = (255, 255, 255, 255)) -> RGBA:
    if not isinstance(v, (list, tuple)) or len(v) not in (3, 4):
        return fallback
    c = list(v) + [255] if len(v) == 3 else list(v)
    return (int(c[0]), int(c[1]), int(c[2]), int(c[3]))


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(profile: dict, which: str, size: int) -> ImageFont.FreeTypeFont:
    name = profile.get(f"font_{which}") or profile["font_cjk"]
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(resolve_font(name)), size)
    return _font_cache[key]


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont):
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0], b[3] - b[1], b


def _center(draw: ImageDraw.ImageDraw, cx: float, y: float, text: str,
            font: ImageFont.FreeTypeFont, fill: RGBA,
            stroke: int = 0, stroke_fill: Optional[RGBA] = None) -> int:
    w, h, b = _measure(draw, text, font)
    draw.text((cx - w / 2 - b[0], y - b[1]), text, font=font, fill=fill,
              stroke_width=stroke, stroke_fill=stroke_fill)
    return h


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int) -> list[str]:
    """Wrap to max_w. Splits on spaces for Latin; falls back to per-character
    for CJK, which has no spaces to break on."""
    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            continue
        if _measure(draw, para, font)[0] <= max_w:
            lines.append(para)
            continue
        units = para.split(" ") if " " in para else list(para)
        joiner = " " if " " in para else ""
        cur = ""
        for u in units:
            trial = f"{cur}{joiner}{u}" if cur else u
            if _measure(draw, trial, font)[0] <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = u
        if cur:
            lines.append(cur)
    return lines


def _fit_logo(path: Path, target_w: int, opacity: float = 1.0) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    ratio = target_w / im.width
    im = im.resize((target_w, int(im.height * ratio)), Image.LANCZOS)
    if opacity < 1.0:
        alpha = im.getchannel("A").point(lambda a: int(a * opacity))
        im.putalpha(alpha)
    return im


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def draw_caption(img: Image.Image, profile: dict, zh: str | None, en: str | None) -> None:
    """Lower-third bilingual caption in an optional rounded scrim."""
    if not zh and not en:
        return
    W, H = img.size
    cfg = profile["caption"]
    d = ImageDraw.Draw(img)

    f_zh = _font(profile, "cjk", int(cfg["zh_size"]))
    f_en = _font(profile, "latin", int(cfg["en_size"]))
    max_w = int(W * float(cfg["max_width_frac"])) - 2 * int(cfg["scrim"]["pad_x"])

    lines: list[tuple[str, ImageFont.FreeTypeFont, RGBA]] = []
    if zh:
        for ln in _wrap(d, zh, f_zh, max_w):
            lines.append((ln, f_zh, _rgba(cfg["zh_color"])))
    if en:
        for ln in _wrap(d, en, f_en, max_w):
            lines.append((ln, f_en, _rgba(cfg["en_color"])))

    gap = int(cfg["gap"])
    heights = [_measure(d, t, f)[1] for t, f, _ in lines]
    widths = [_measure(d, t, f)[0] for t, f, _ in lines]

    scrim = cfg["scrim"]
    pad_x, pad_y = int(scrim["pad_x"]), int(scrim["pad_y"])
    box_w = min(W - 80, max(widths) + pad_x * 2)
    box_h = sum(heights) + gap * (len(lines) - 1) + pad_y * 2
    box_x0 = (W - box_w) / 2
    box_y1 = H - int(cfg["bottom_margin"])
    box_y0 = box_y1 - box_h

    if scrim.get("enabled", True):
        d.rounded_rectangle([box_x0, box_y0, box_x0 + box_w, box_y1],
                            radius=int(scrim["radius"]), fill=_rgba(scrim["color"], (0, 0, 0, 150)))

    st = cfg.get("stroke", {})
    sw = int(st.get("width", 0)) if st.get("enabled") else 0
    sf = _rgba(st.get("color"), (0, 0, 0, 255)) if sw else None

    y = box_y0 + pad_y
    for (text, font, color), h in zip(lines, heights):
        _center(d, W / 2, y, text, font, color, stroke=sw, stroke_fill=sf)
        y += h + gap


def _y_for(position: str, H: int) -> float:
    return {"top": H * 0.22, "middle": H * 0.5, "bottom": H * 0.74}.get(position, H * 0.22)


def draw_bubble(img: Image.Image, profile: dict, text: str, subtext: str | None,
                position: str, accent_name: str | None) -> None:
    """Notification-style card: colored icon disc + subtext over headline.

    The repo original also drew a tail pointing at the phone in frame. That
    needs per-shot handset coordinates, which no stateless request carries, so
    the tail is dropped here (see EXTRACTION_NOTES.md).
    """
    W, H = img.size
    cfg = profile["bubble"]
    d = ImageDraw.Draw(img)
    accent = ACCENTS.get(accent_name or "", _rgba(cfg["accent"], (216, 50, 50, 255)))

    f_val = _font(profile, "latin", int(cfg["value_size"]))
    f_sub = _font(profile, "cjk", int(cfg["title_size"]))

    vw, vh, _ = _measure(d, text, f_val)
    sw, sh, _ = (_measure(d, subtext, f_sub) if subtext else (0, 0, None))

    pad, gap, disc_r, icon_gap = 26, 6, 30, 22
    text_w = max(vw, sw)
    text_h = vh + (sh + gap if subtext else 0)
    card_w = min(W - 80, disc_r * 2 + icon_gap + text_w + pad * 2)
    card_h = max(text_h, disc_r * 2) + pad * 2
    x0 = (W - card_w) / 2
    y0 = _y_for(position, H) - card_h / 2

    d.rounded_rectangle([x0, y0, x0 + card_w, y0 + card_h], radius=26,
                        fill=_rgba(cfg["card"]), outline=accent, width=4)

    dcx, dcy = x0 + pad + disc_r, y0 + card_h / 2
    d.ellipse([dcx - disc_r, dcy - disc_r, dcx + disc_r, dcy + disc_r], fill=accent)
    # A check for the positive accents, a cross for the negative ones.
    if accent == ACCENTS["green"]:
        d.line([dcx - 13, dcy + 1, dcx - 3, dcy + 12], fill=(255, 255, 255, 255), width=6)
        d.line([dcx - 3, dcy + 12, dcx + 15, dcy - 12], fill=(255, 255, 255, 255), width=6)
    else:
        d.line([dcx - 12, dcy - 12, dcx + 12, dcy + 12], fill=(255, 255, 255, 255), width=6)
        d.line([dcx - 12, dcy + 12, dcx + 12, dcy - 12], fill=(255, 255, 255, 255), width=6)

    tx = dcx + disc_r + icon_gap
    ty = y0 + (card_h - text_h) / 2
    if subtext:
        d.text((tx, ty), subtext, font=f_sub, fill=_rgba(cfg["title_color"]))
        ty += sh + gap
    d.text((tx, ty), text, font=f_val, fill=_rgba(cfg["value_color"]))


def draw_callout(img: Image.Image, profile: dict, text: str, position: str) -> None:
    """Simple pill of text — the cheap emphasis overlay."""
    W, H = img.size
    cfg = profile["callout"]
    d = ImageDraw.Draw(img)
    font = _font(profile, "cjk", int(cfg["size"]))
    pad_x, pad_y = int(cfg["pad_x"]), int(cfg["pad_y"])

    lines = _wrap(d, text, font, W - 160 - pad_x * 2)
    widths = [_measure(d, t, font)[0] for t in lines]
    heights = [_measure(d, t, font)[1] for t in lines]
    gap = 10
    box_w = max(widths) + pad_x * 2
    box_h = sum(heights) + gap * (len(lines) - 1) + pad_y * 2
    x0 = (W - box_w) / 2
    y0 = _y_for(position, H) - box_h / 2

    d.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=int(cfg["radius"]),
                        fill=_rgba(cfg["bg"], (253, 197, 13, 255)))
    y = y0 + pad_y
    for t, h in zip(lines, heights):
        _center(d, W / 2, y, t, font, _rgba(cfg["color"], (17, 17, 17, 255)))
        y += h + gap


def draw_logo(img: Image.Image, profile: dict) -> None:
    """Wordmark watermark on a translucent pill. Skipped entirely when the
    profile disables it — that's what makes `ugc` renders look un-branded."""
    cfg = profile.get("logo", {})
    if not cfg.get("enabled"):
        return
    path = brand_file(cfg.get("image")) or brand_file("logo.png")
    if not path:
        return

    W, _H = img.size
    d = ImageDraw.Draw(img)
    logo = _fit_logo(path, int(cfg.get("width", 300)), float(cfg.get("opacity", 1.0)))
    pill = cfg.get("pill", {})
    pad = int(pill.get("pad", 22)) if pill.get("enabled") else 0
    mx, my = int(cfg.get("margin_x", 40)), int(cfg.get("margin_y", 70))

    pw, ph = logo.width + pad * 2, logo.height + pad * 2
    x0 = W - pw - mx if "right" in cfg.get("position", "top-right") else mx
    y0 = my

    if pill.get("enabled"):
        d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=ph // 2,
                            fill=_rgba(pill.get("color"), (0, 0, 0, 120)))
    img.alpha_composite(logo, (int(x0 + pad), int(y0 + pad)))


# --------------------------------------------------------------------------
# composites
# --------------------------------------------------------------------------

def scene_overlay(profile: dict, w: int, h: int, captions: dict | None,
                  overlays: list[dict], out: Path, with_logo: bool = True) -> Path | None:
    """Flatten every layer for one scene into a single transparent PNG.

    One PNG per scene means one ffmpeg overlay link per scene, instead of one
    per element — the filtergraph stays short no matter how busy the scene is.
    Returns None when the scene has nothing to draw.
    """
    logo_on = with_logo and profile.get("logo", {}).get("enabled", False)
    if not captions and not overlays and not logo_on:
        return None

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for ov in overlays:
        if ov["type"] == "bubble":
            draw_bubble(img, profile, ov["text"], ov.get("subtext"),
                        ov.get("position", "top"), ov.get("accent"))
        else:
            draw_callout(img, profile, ov["text"], ov.get("position", "top"))
    if captions:
        draw_caption(img, profile, captions.get("zh"), captions.get("en"))
    if logo_on:
        draw_logo(img, profile)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def intro_card(profile: dict, w: int, h: int, card: dict, out: Path) -> Path:
    cfg = profile["cards"]
    img = Image.new("RGBA", (w, h), _rgba(cfg["bg"]))
    d = ImageDraw.Draw(img)

    y = int(h * 0.29)
    logo_path = brand_file(cfg.get("logo_image")) or brand_file("logo.png")
    if logo_path:
        logo = _fit_logo(logo_path, int(cfg["logo_width"]))
        img.alpha_composite(logo, (int((w - logo.width) / 2), y))
        y += logo.height + 50
        d.rounded_rectangle([(w - 120) / 2, y, (w + 120) / 2, y + 12], radius=6,
                            fill=_rgba(cfg["accent"]))
        y += 90

    y = max(y, int(h * 0.51))
    if card.get("title"):
        f = _font(profile, "cjk", int(cfg["title_size"]))
        for line in _wrap(d, card["title"], f, w - 160):
            y += _center(d, w / 2, y, line, f, _rgba(cfg["title_color"])) + 28
        y += 20
    if card.get("subtitle"):
        f = _font(profile, "latin", int(cfg["subtitle_size"]))
        for line in _wrap(d, card["subtitle"], f, w - 160):
            y += _center(d, w / 2, y, line, f, _rgba(cfg["subtitle_color"])) + 22

    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    return out


def outro_card(profile: dict, w: int, h: int, card: dict, out: Path) -> Path:
    cfg = profile["cards"]
    img = Image.new("RGBA", (w, h), _rgba(cfg["bg"]))
    d = ImageDraw.Draw(img)
    accent = _rgba(cfg["accent"])
    title_color = _rgba(cfg["title_color"])

    y = int(h * 0.08)
    logo_path = brand_file(cfg.get("logo_image")) or brand_file("logo.png")
    if logo_path:
        logo = _fit_logo(logo_path, int(cfg["logo_width"]))
        img.alpha_composite(logo, (int((w - logo.width) / 2), y))
        y += logo.height + 120

    bullets = card.get("bullets") or []
    if bullets:
        f = _font(profile, "cjk", 38)
        left = 96
        for text in bullets:
            cy = y + 19
            d.ellipse([left, cy - 19, left + 38, cy + 19], fill=accent)
            d.line([left + 10, cy + 1, left + 17, cy + 10], fill=title_color, width=5)
            d.line([left + 17, cy + 10, left + 29, cy - 7], fill=title_color, width=5)
            d.text((left + 58, y), text, font=f, fill=title_color)
            y += 84
        y += 60

    if card.get("title"):
        f = _font(profile, "cjk", int(cfg["title_size"]) - 8)
        for line in _wrap(d, card["title"], f, w - 160):
            y += _center(d, w / 2, y, line, f, title_color) + 30
    if card.get("subtitle"):
        f = _font(profile, "latin", int(cfg["subtitle_size"]) - 2)
        for line in _wrap(d, card["subtitle"], f, w - 160):
            y += _center(d, w / 2, y, line, f, _rgba(cfg["subtitle_color"])) + 22

    if card.get("cta"):
        y += 70
        f = _font(profile, "cjk", int(cfg["cta_size"]))
        cw, ch, _ = _measure(d, card["cta"], f)
        pw, ph = cw + 80, ch + 44
        x0 = (w - pw) / 2
        y = min(y, h - ph - 80)
        d.rounded_rectangle([x0, y, x0 + pw, y + ph], radius=ph // 2,
                            fill=_rgba(cfg["cta_bg"]))
        _center(d, w / 2, y + 22, card["cta"], f, _rgba(cfg["cta_color"]))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    return out
