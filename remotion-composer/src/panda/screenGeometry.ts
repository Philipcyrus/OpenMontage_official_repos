/**
 * Panda screenshot layout — geometry and timing.
 *
 * One layout (written by the scene-plan director into the approved scene plan)
 * drives the previews and the final overlay. The Python side mirrors
 * deviceGeometry() and fitRegion() in dify_launcher/screens.py for its checks,
 * so keep the two in step.
 *
 * Coordinates:
 *   zone, card zone, subject zone ... fractions of the video frame (0-1)
 *   crop, step regions and points ... fractions of the WHOLE screenshot (0-1)
 *   *_s ............................. seconds from the start of the scene
 */

export type Box = { x: number; y: number; w: number; h: number };
export type Point = [number, number];
export type FrameStyle = "phone" | "browser" | "card" | "none";
export type MotionType =
  | "none"
  | "fade"
  | "pop"
  | "slide_left"
  | "slide_right"
  | "slide_up"
  | "slide_down";
export type Motion = { type?: MotionType; at_s?: number; duration_s?: number };

export type Step =
  | { kind: "blur_region"; region: Box }
  | { kind: "highlight_box"; region: Box; at_s?: number; duration_s?: number }
  | { kind: "cursor_move"; to: Point; from?: Point; at_s?: number; duration_s?: number }
  | { kind: "click_pulse"; at?: Point; at_s?: number; duration_s?: number }
  | { kind: "zoom_to"; region: Box; at_s?: number; duration_s?: number }
  | {
      kind: "card";
      text: string | { zh?: string; en?: string };
      zone: Box;
      at_s?: number;
      duration_s?: number;
    };

export type ScreenLayout = {
  zone: Box;
  frame?: FrameStyle;
  crop?: Box;
  show?: { from_s?: number; to_s?: number };
  enter?: Motion;
  exit?: Motion;
  steps?: Step[];
};

export type ScreenLayerSpec = {
  /** File name inside the render's public dir. */
  src: string;
  natural: { width: number; height: number };
  layout: ScreenLayout;
};

export const PANDA_YELLOW = "#fdc50d";
export const PANDA_BLACK = "#111111";

export const clamp = (v: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, v));

export const easeInOutCubic = (p: number) => {
  const x = clamp(p, 0, 1);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
};

const lerp = (a: number, b: number, p: number) => a + (b - a) * p;

/** Chrome sizes as fractions of the zone's shorter side. Mirrored in screens.py. */
export const CHROME: Record<
  FrameStyle,
  { side: number; top: number; bottom: number; body: number; screen: number }
> = {
  phone: { side: 0.035, top: 0.07, bottom: 0.05, body: 0.09, screen: 0.05 },
  browser: { side: 0.012, top: 0.085, bottom: 0.012, body: 0.03, screen: 0.006 },
  card: { side: 0.03, top: 0.03, bottom: 0.03, body: 0.04, screen: 0.02 },
  none: { side: 0, top: 0, bottom: 0, body: 0, screen: 0 },
};

export type DeviceGeometry = {
  frame: FrameStyle;
  device: Box; // canvas px
  screen: Box; // canvas px
  padTop: number;
  radiusBody: number;
  radiusScreen: number;
  keyline: number;
  aspect: number; // screen width / height
};

export function normBox(b: Box | undefined, fallback: Box): Box {
  if (!b) return fallback;
  const x = clamp(Number(b.x) || 0, 0, 1);
  const y = clamp(Number(b.y) || 0, 0, 1);
  const w = clamp(Number(b.w) || 0, 0.001, 1 - x);
  const h = clamp(Number(b.h) || 0, 0.001, 1 - y);
  return { x, y, w, h };
}

const FULL: Box = { x: 0, y: 0, w: 1, h: 1 };

export function deviceGeometry(
  layout: ScreenLayout,
  W: number,
  H: number,
  natural: { width: number; height: number },
): DeviceGeometry {
  const frame: FrameStyle =
    layout.frame && CHROME[layout.frame] ? layout.frame : "card";
  const z = normBox(layout.zone, FULL);
  const zx = z.x * W;
  const zy = z.y * H;
  const zw = z.w * W;
  const zh = z.h * H;
  const m = Math.min(zw, zh);
  const c = CHROME[frame];
  const crop = normBox(layout.crop, FULL);
  const aspect = (crop.w * natural.width) / (crop.h * natural.height);
  const side = c.side * m;
  const top = c.top * m;
  const bottom = c.bottom * m;
  const availW = Math.max(1, zw - 2 * side);
  const availH = Math.max(1, zh - top - bottom);
  let sw: number;
  let sh: number;
  if (availW / availH > aspect) {
    sh = availH;
    sw = sh * aspect;
  } else {
    sw = availW;
    sh = sw / aspect;
  }
  const dw = sw + 2 * side;
  const dh = sh + top + bottom;
  const dx = zx + (zw - dw) / 2;
  const dy = zy + (zh - dh) / 2;
  return {
    frame,
    device: { x: dx, y: dy, w: dw, h: dh },
    screen: { x: dx + side, y: dy + top, w: sw, h: sh },
    padTop: top,
    radiusBody: c.body * m,
    radiusScreen: c.screen * m,
    keyline: Math.max(2, Math.round(0.004 * Math.min(W, H))),
    aspect,
  };
}

/** A region of the screenshot (fractions) grown to the screen's aspect, in image px. */
export function fitRegion(
  region: Box,
  aspect: number,
  natural: { width: number; height: number },
): Box {
  const nw = natural.width;
  const nh = natural.height;
  const r = normBox(region, FULL);
  let w = r.w * nw;
  let h = r.h * nh;
  const cx = (r.x + r.w / 2) * nw;
  const cy = (r.y + r.h / 2) * nh;
  if (w / h < aspect) {
    w = h * aspect;
  } else {
    h = w / aspect;
  }
  if (w > nw) {
    w = nw;
    h = w / aspect;
  }
  if (h > nh) {
    h = nh;
    w = h * aspect;
  }
  const x = clamp(cx - w / 2, 0, nw - w);
  const y = clamp(cy - h / 2, 0, nh - h);
  return { x, y, w, h };
}

const stepAt = (s: { at_s?: number }) => Number(s.at_s ?? 0) || 0;

/** The part of the screenshot on screen at time t (image px). */
export function visibleRegionAt(
  t: number,
  layout: ScreenLayout,
  natural: { width: number; height: number },
  aspect: number,
  preview: boolean,
): Box {
  let cur = fitRegion(normBox(layout.crop, FULL), aspect, natural);
  const zooms = (layout.steps || [])
    .filter((s): s is Extract<Step, { kind: "zoom_to" }> => s.kind === "zoom_to")
    .sort((a, b) => stepAt(a) - stepAt(b));
  for (const z of zooms) {
    const at = stepAt(z);
    const d = Math.max(0.01, Number(z.duration_s ?? 0.8));
    const target = fitRegion(z.region, aspect, natural);
    if (preview || t >= at + d) {
      cur = target;
      continue;
    }
    if (t > at) {
      const p = easeInOutCubic((t - at) / d);
      cur = {
        x: lerp(cur.x, target.x, p),
        y: lerp(cur.y, target.y, p),
        w: lerp(cur.w, target.w, p),
        h: lerp(cur.h, target.h, p),
      };
    }
    break;
  }
  return cur;
}

export type CursorState = {
  visible: boolean;
  pos: Point; // screenshot fractions
  pulses: { at: Point; p: number }[];
};

export function cursorAt(t: number, layout: ScreenLayout, preview: boolean): CursorState {
  const events = (layout.steps || [])
    .filter((s) => s.kind === "cursor_move" || s.kind === "click_pulse")
    .sort((a, b) => stepAt(a as { at_s?: number }) - stepAt(b as { at_s?: number }));
  let pos: Point | null = null;
  let visible = false;
  const pulses: { at: Point; p: number }[] = [];
  for (const e of events) {
    const at = stepAt(e as { at_s?: number });
    if (!preview && t < at) break;
    if (e.kind === "cursor_move") {
      const start: Point =
        pos ?? e.from ?? [clamp(e.to[0] + 0.12, 0, 1), clamp(e.to[1] + 0.12, 0, 1)];
      const d = Math.max(0.01, Number(e.duration_s ?? 0.6));
      if (preview || t >= at + d) {
        pos = e.to;
      } else {
        const p = easeInOutCubic((t - at) / d);
        pos = [lerp(start[0], e.to[0], p), lerp(start[1], e.to[1], p)];
      }
      visible = true;
    } else if (e.kind === "click_pulse") {
      const pt: Point = e.at ?? pos ?? [0.5, 0.5];
      if (!pos) pos = pt;
      visible = true;
      const d = Math.max(0.01, Number(e.duration_s ?? 0.5));
      if (preview) {
        pulses.push({ at: pt, p: 0.4 });
      } else if (t <= at + d) {
        pulses.push({ at: pt, p: clamp((t - at) / d, 0, 1) });
      }
    }
  }
  return { visible, pos: pos ?? [0.5, 0.5], pulses };
}

export function windowOpen(
  t: number,
  at: number | undefined,
  duration: number | undefined,
  preview: boolean,
): boolean {
  if (preview) return true;
  const a = Number(at ?? 0) || 0;
  if (t < a) return false;
  if (duration === undefined || duration === null) return true;
  return t <= a + Number(duration);
}

/** Opacity and transform for a layer's enter / exit at time t. */
export function layerMotion(
  t: number,
  layout: ScreenLayout,
  sceneDuration: number,
  W: number,
  H: number,
  preview: boolean,
): { opacity: number; transform: string } {
  if (preview) return { opacity: 1, transform: "none" };
  const from = Number(layout.show?.from_s ?? 0) || 0;
  const to = Number(layout.show?.to_s ?? sceneDuration);
  if (t < from || t > to) return { opacity: 0, transform: "none" };

  const enterType: MotionType = layout.enter?.type ?? "fade";
  const enterAt = Number(layout.enter?.at_s ?? from);
  const enterDur = Math.max(0.01, Number(layout.enter?.duration_s ?? 0.35));
  let pIn = 1;
  if (enterType !== "none") pIn = easeInOutCubic((t - enterAt) / enterDur);
  else pIn = t >= enterAt ? 1 : 0;

  let exitType: MotionType = layout.exit?.type ?? "none";
  let exitDur = Math.max(0.01, Number(layout.exit?.duration_s ?? 0.3));
  if (!layout.exit && to < sceneDuration - 1e-3) {
    exitType = "fade";
    exitDur = 0.3;
  }
  let pOut = 1;
  if (exitType !== "none") {
    const exitAt = Number(layout.exit?.at_s ?? to - exitDur);
    pOut = 1 - easeInOutCubic((t - exitAt) / exitDur);
  }

  const partFor = (type: MotionType, p: number) => {
    const q = 1 - p;
    switch (type) {
      case "slide_left":
        return { o: p, tr: `translateX(${q * 0.35 * W}px)` };
      case "slide_right":
        return { o: p, tr: `translateX(${-q * 0.35 * W}px)` };
      case "slide_up":
        return { o: p, tr: `translateY(${q * 0.25 * H}px)` };
      case "slide_down":
        return { o: p, tr: `translateY(${-q * 0.25 * H}px)` };
      case "pop":
        return { o: p, tr: `scale(${0.85 + 0.15 * p})` };
      case "none":
        return { o: p >= 1 ? 1 : p, tr: "" };
      default:
        return { o: p, tr: "" };
    }
  };
  const a = partFor(enterType, pIn);
  const b = partFor(exitType, pOut);
  const transform = [a.tr, b.tr].filter(Boolean).join(" ") || "none";
  return { opacity: clamp(Math.min(a.o, b.o), 0, 1), transform };
}

export function cardText(
  text: string | { zh?: string; en?: string },
  language: string,
): string {
  if (typeof text === "string") return text;
  if (language === "zh") return text.zh || text.en || "";
  return text.en || text.zh || "";
}
