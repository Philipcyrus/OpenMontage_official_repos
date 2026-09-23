import React, { useEffect, useState } from "react";
import { continueRender, delayRender, Img, staticFile } from "remotion";
import {
  Box,
  cardText,
  clamp,
  cursorAt,
  deviceGeometry,
  layerMotion,
  PANDA_BLACK,
  PANDA_YELLOW,
  ScreenLayerSpec,
  Step,
  visibleRegionAt,
  windowOpen,
} from "./screenGeometry";

export const PANDA_FONT_STACK =
  '"PandaCJK", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Noto Sans SC", sans-serif';

/** Loads the CJK font file staged into the public dir (the one panda_render uses). */
export const usePandaFont = (fontSrc?: string) => {
  const [handle] = useState(() => delayRender("panda-font"));
  useEffect(() => {
    if (!fontSrc || typeof FontFace === "undefined") {
      continueRender(handle);
      return;
    }
    const face = new FontFace("PandaCJK", `url(${staticFile(fontSrc)})`);
    face
      .load()
      .then((loaded) => {
        document.fonts.add(loaded);
        continueRender(handle);
      })
      .catch(() => continueRender(handle));
  }, [fontSrc, handle]);
};

type LayerProps = {
  spec: ScreenLayerSpec;
  width: number;
  height: number;
  t: number;
  sceneDuration: number;
  preview: boolean;
  language: string;
};

const abs = (b: Box): React.CSSProperties => ({
  position: "absolute",
  left: b.x,
  top: b.y,
  width: b.w,
  height: b.h,
});

const Cursor: React.FC<{ size: number; keyline: number }> = ({ size, keyline }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" style={{ overflow: "visible" }}>
    <path
      d="M3 2 L3 19 L7.5 14.8 L10.6 21.5 L13.6 20.1 L10.6 13.6 L16.8 13.6 Z"
      fill="#ffffff"
      stroke={PANDA_BLACK}
      strokeWidth={Math.max(1.2, (keyline / size) * 24)}
      strokeLinejoin="round"
    />
  </svg>
);

export const ScreenLayer: React.FC<LayerProps> = ({
  spec,
  width: W,
  height: H,
  t,
  sceneDuration,
  preview,
  language,
}) => {
  const { layout, natural } = spec;
  const g = deviceGeometry(layout, W, H, natural);
  const v = visibleRegionAt(t, layout, natural, g.aspect, preview);
  const k = g.screen.w / v.w; // screen px per image px
  const steps: Step[] = layout.steps || [];
  const motion = layerMotion(t, layout, sceneDuration, W, H, preview);
  if (motion.opacity <= 0) return null;

  const kl = g.keyline;
  const thin = Math.max(1, Math.round(kl * 0.6));
  const src = staticFile(spec.src);
  const plane: React.CSSProperties = {
    position: "absolute",
    left: -v.x * k,
    top: -v.y * k,
    width: natural.width * k,
    height: natural.height * k,
  };
  const px = (b: Box): Box => ({
    x: b.x * natural.width * k,
    y: b.y * natural.height * k,
    w: b.w * natural.width * k,
    h: b.h * natural.height * k,
  });
  const toScreen = (pt: [number, number]) => ({
    x: (pt[0] * natural.width - v.x) * k,
    y: (pt[1] * natural.height - v.y) * k,
  });

  const cursor = cursorAt(t, layout, preview);
  const cursorSize = clamp(0.07 * g.screen.w, 22, 72);

  const device = g.device;
  const chromeLess = g.frame === "none" || g.frame === "held";
  const deviceStyle: React.CSSProperties = {
    ...abs(device),
    background: chromeLess ? "transparent" : "#ffffff",
    border: chromeLess ? "none" : `${kl}px solid ${PANDA_BLACK}`,
    borderRadius: g.radiusBody,
    boxSizing: "border-box",
  };
  const screenBox: Box = {
    x: g.screen.x - device.x,
    y: g.screen.y - device.y,
    w: g.screen.w,
    h: g.screen.h,
  };

  const cards = steps.filter(
    (s): s is Extract<Step, { kind: "card" }> => s.kind === "card",
  );

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        opacity: motion.opacity,
        transform: motion.transform,
        transformOrigin: `${device.x + device.w / 2}px ${device.y + device.h / 2}px`,
      }}
    >
      <div style={deviceStyle}>
        {g.frame === "phone" ? (
          <div
            style={{
              position: "absolute",
              left: device.w / 2 - device.w * 0.09,
              top: Math.max(kl, (g.padTop - kl) / 2 - Math.max(3, g.padTop * 0.09)),
              width: device.w * 0.18,
              height: Math.max(4, g.padTop * 0.18),
              borderRadius: 999,
              background: PANDA_BLACK,
            }}
          />
        ) : null}
        {g.frame === "browser" ? (
          <div
            style={{
              position: "absolute",
              left: 0,
              top: 0,
              right: 0,
              height: g.padTop - kl,
              background: "#f2f2f2",
              borderTopLeftRadius: g.radiusBody,
              borderTopRightRadius: g.radiusBody,
              display: "flex",
              alignItems: "center",
              gap: g.padTop * 0.14,
              paddingLeft: g.padTop * 0.3,
              boxSizing: "border-box",
            }}
          >
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                style={{
                  width: g.padTop * 0.24,
                  height: g.padTop * 0.24,
                  borderRadius: 999,
                  background: i === 0 ? PANDA_YELLOW : "#ffffff",
                  border: `${thin}px solid ${PANDA_BLACK}`,
                  boxSizing: "border-box",
                }}
              />
            ))}
            <div
              style={{
                marginLeft: g.padTop * 0.2,
                flex: 1,
                marginRight: g.padTop * 0.3,
                height: g.padTop * 0.42,
                borderRadius: 999,
                background: "#ffffff",
                border: `${thin}px solid ${PANDA_BLACK}`,
                boxSizing: "border-box",
              }}
            />
          </div>
        ) : null}
        <div
          style={{
            ...abs(
              chromeLess
                ? screenBox
                : {
                    x: screenBox.x - kl,
                    y: screenBox.y - kl,
                    w: screenBox.w,
                    h: screenBox.h,
                  },
            ),
            overflow: "hidden",
            borderRadius: g.radiusScreen,
            background: "#ffffff",
            boxShadow: chromeLess ? "none" : `0 0 0 ${thin}px ${PANDA_BLACK}`,
          }}
        >
          <div style={plane}>
            <Img
              src={src}
              style={{ position: "absolute", left: 0, top: 0, width: "100%", height: "100%" }}
            />
            {steps
              .filter((s): s is Extract<Step, { kind: "blur_region" }> => s.kind === "blur_region")
              .map((s, i) => {
                const b = px(s.region);
                const radius = clamp(0.3 * Math.min(b.w, b.h), 6, 40);
                return (
                  <div key={`blur-${i}`} style={{ ...abs(b), overflow: "hidden" }}>
                    <Img
                      src={src}
                      style={{
                        position: "absolute",
                        left: -b.x,
                        top: -b.y,
                        width: natural.width * k,
                        height: natural.height * k,
                        filter: `blur(${radius}px)`,
                      }}
                    />
                    <div
                      style={{
                        position: "absolute",
                        inset: 0,
                        background: "rgba(255,255,255,0.35)",
                      }}
                    />
                  </div>
                );
              })}
            {steps
              .filter(
                (s): s is Extract<Step, { kind: "highlight_box" }> => s.kind === "highlight_box",
              )
              .filter((s) => windowOpen(t, s.at_s, s.duration_s, preview))
              .map((s, i) => {
                const b = px(s.region);
                const p = preview ? 1 : clamp((t - Number(s.at_s ?? 0)) / 0.25, 0, 1);
                const stroke = Math.max(3, kl * 1.4);
                return (
                  <div
                    key={`hl-${i}`}
                    style={{
                      ...abs({ x: b.x - stroke, y: b.y - stroke, w: b.w + 2 * stroke, h: b.h + 2 * stroke }),
                      border: `${stroke}px solid ${PANDA_YELLOW}`,
                      boxShadow: `0 0 0 ${thin}px ${PANDA_BLACK}, inset 0 0 0 ${thin}px ${PANDA_BLACK}`,
                      borderRadius: clamp(0.015 * g.screen.w, 6, 24),
                      opacity: p,
                      transform: `scale(${1.08 - 0.08 * p})`,
                      boxSizing: "border-box",
                    }}
                  />
                );
              })}
          </div>
          {cursor.pulses.map((pl, i) => {
            const c = toScreen(pl.at);
            const r = cursorSize * (0.25 + 0.75 * pl.p);
            return (
              <div
                key={`pulse-${i}`}
                style={{
                  position: "absolute",
                  left: c.x - r,
                  top: c.y - r,
                  width: 2 * r,
                  height: 2 * r,
                  borderRadius: 999,
                  border: `${Math.max(3, kl)}px solid ${PANDA_YELLOW}`,
                  boxShadow: `0 0 0 ${thin}px ${PANDA_BLACK}`,
                  opacity: 1 - pl.p * 0.9,
                  boxSizing: "border-box",
                }}
              />
            );
          })}
          {cursor.visible
            ? (() => {
                const c = toScreen(cursor.pos);
                return (
                  <div
                    style={{
                      position: "absolute",
                      left: c.x - cursorSize * 0.12,
                      top: c.y - cursorSize * 0.08,
                    }}
                  >
                    <Cursor size={cursorSize} keyline={kl} />
                  </div>
                );
              })()
            : null}
        </div>
      </div>
      {cards
        .filter((s) => windowOpen(t, s.at_s, s.duration_s, preview))
        .map((s, i) => {
          const z = s.zone;
          const b: Box = { x: z.x * W, y: z.y * H, w: z.w * W, h: z.h * H };
          const text = cardText(s.text, language);
          const units = Array.from(text).reduce(
            (n, ch) => n + (/[⺀-鿿豈-﫿＀-￯]/.test(ch) ? 1 : 0.56),
            0,
          );
          const fontSize = Math.max(
            12,
            Math.min(b.h * 0.5, (b.w * 0.88) / Math.max(1, units)),
          );
          const p = preview ? 1 : clamp((t - Number(s.at_s ?? 0)) / 0.25, 0, 1);
          return (
            <div
              key={`card-${i}`}
              style={{
                ...abs(b),
                background: PANDA_YELLOW,
                border: `${kl}px solid ${PANDA_BLACK}`,
                borderRadius: b.h * 0.3,
                boxSizing: "border-box",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: PANDA_BLACK,
                fontFamily: PANDA_FONT_STACK,
                fontWeight: 700,
                fontSize,
                lineHeight: 1,
                whiteSpace: "nowrap",
                overflow: "hidden",
                opacity: p,
                transform: `scale(${0.9 + 0.1 * p})`,
              }}
            >
              {text}
            </div>
          );
        })}
    </div>
  );
};

export const SubjectPlaceholder: React.FC<{
  zone: Box;
  width: number;
  height: number;
  label?: string;
}> = ({ zone, width: W, height: H, label }) => {
  const b: Box = { x: zone.x * W, y: zone.y * H, w: zone.w * W, h: zone.h * H };
  const kl = Math.max(2, Math.round(0.004 * Math.min(W, H)));
  return (
    <div
      style={{
        ...abs(b),
        background: "#eef0f2",
        border: `${kl}px dashed #8a9099`,
        borderRadius: 0.06 * Math.min(b.w, b.h),
        boxSizing: "border-box",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#5f6670",
        fontFamily: PANDA_FONT_STACK,
        fontWeight: 700,
        fontSize: clamp(0.09 * Math.min(b.w, b.h), 14, 64),
      }}
    >
      {label ?? "Panda"}
    </div>
  );
};
