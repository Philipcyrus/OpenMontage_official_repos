import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  PROMO_YELLOW,
  promoFontFamily,
} from "./promoFonts";

type PromoHeroProps = {
  text: string;
  subtitle?: string;
  accentColor?: string;
  /** mid-right (default), left, center, bottom-left, bottom-right, split */
  position?: string;
};

/**
 * Flat yellow ExtraBlack punch word (YES / 50% OFF / N·O flanking).
 * No shadow, no stroke — brand-matched to Panda Mobile kinetic captions.
 */
export const PromoHero: React.FC<PromoHeroProps> = ({
  text,
  subtitle,
  accentColor = PROMO_YELLOW,
  position = "mid-right",
}) => {
  const frame = useCurrentFrame();
  const { fps, height, width } = useVideoConfig();

  const pop = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 160, mass: 0.65 },
  });

  const clean = (text || "").trim();
  const isSplit =
    position === "split" ||
    (clean.length === 2 && !/\s/.test(clean) && !subtitle);

  // Short punch words: ~45% of frame height; longer lines scale down.
  const baseSize = Math.round(
    Math.min(height * 0.48, Math.max(height * 0.14, (height * 1.1) / Math.max(clean.length, 1))),
  );

  const letterStyle: React.CSSProperties = {
    fontFamily: promoFontFamily,
    fontWeight: 900,
    fontSize: baseSize,
    color: accentColor,
    lineHeight: 0.9,
    letterSpacing: "-0.04em",
    textTransform: "uppercase",
    margin: 0,
    padding: 0,
  };

  if (isSplit && clean.length >= 2) {
    const left = clean[0];
    const right = clean.slice(1);
    const opacity = pop;
    const scale = interpolate(pop, [0, 1], [0.7, 1]);
    return (
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <div
          style={{
            position: "absolute",
            left: width * 0.06,
            top: "50%",
            transform: `translateY(-50%) scale(${scale})`,
            opacity,
            ...letterStyle,
          }}
        >
          {left}
        </div>
        <div
          style={{
            position: "absolute",
            right: width * 0.06,
            top: "50%",
            transform: `translateY(-50%) scale(${scale})`,
            opacity,
            ...letterStyle,
          }}
        >
          {right}
        </div>
      </AbsoluteFill>
    );
  }

  const alignItems =
    position.includes("left")
      ? ("flex-start" as const)
      : position.includes("center") && !position.includes("mid")
        ? ("center" as const)
        : ("flex-end" as const);
  const justifyContent =
    position.includes("bottom")
      ? ("flex-end" as const)
      : position.includes("top")
        ? ("flex-start" as const)
        : ("center" as const);

  const padX = Math.round(width * 0.05);
  const padY = Math.round(height * 0.08);

  return (
    <AbsoluteFill
      style={{
        justifyContent,
        alignItems,
        paddingLeft: padX,
        paddingRight: padX,
        paddingTop: padY,
        paddingBottom: padY,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          opacity: pop,
          transform: `scale(${interpolate(pop, [0, 1], [0.72, 1])})`,
          transformOrigin:
            alignItems === "flex-end"
              ? "right center"
              : alignItems === "center"
                ? "center center"
                : "left center",
          textAlign:
            alignItems === "flex-end"
              ? "right"
              : alignItems === "center"
                ? "center"
                : "left",
          maxWidth: "58%",
        }}
      >
        <div style={letterStyle}>{clean}</div>
        {subtitle ? (
          <div
            style={{
              ...letterStyle,
              fontSize: Math.round(baseSize * 0.38),
              marginTop: Math.round(baseSize * 0.08),
              letterSpacing: "-0.02em",
            }}
          >
            {subtitle}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
