import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  PROMO_BLACK,
  PROMO_WHITE,
  PROMO_YELLOW,
  promoFontFamily,
} from "./promoFonts";

type PromoBadgeProps = {
  text: string;
  subtitle?: string;
  lines?: string[];
  brand?: string;
  /** white (default) or black disc */
  variant?: "white" | "black";
  /** degrees; brand stickers tilt ~-8 */
  tiltDeg?: number;
  showTm?: boolean;
};

/**
 * Center disc + black brand pill + heavy body lines (OnePool™ style).
 */
export const PromoBadge: React.FC<PromoBadgeProps> = ({
  text,
  subtitle,
  lines,
  brand,
  variant = "white",
  tiltDeg = -8,
  showTm = true,
}) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();

  const enter = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 120 },
  });

  const featureLines = lines?.length
    ? lines
    : subtitle
      ? subtitle
          .split(/\s*[·|]\s*/)
          .map((s) => s.trim())
          .filter(Boolean)
      : [];

  const isWhite = variant !== "black";
  const discBg = isWhite ? PROMO_WHITE : PROMO_BLACK;
  const bodyColor = isWhite ? PROMO_BLACK : PROMO_YELLOW;
  const discSize = Math.round(Math.min(height * 0.52, 480));

  const brandLabel = brand || (text && !featureLines.length ? text : undefined);
  const headline = brand ? text : featureLines.length ? undefined : text;

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          opacity: enter,
          transform: `scale(${interpolate(enter, [0, 1], [0.75, 1])}) rotate(${tiltDeg}deg)`,
          width: discSize,
          height: discSize,
          borderRadius: "50%",
          background: discBg,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: discSize * 0.1,
          boxSizing: "border-box",
        }}
      >
        {brandLabel ? (
          <div
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 6,
              marginBottom: 14,
            }}
          >
            <div
              style={{
                background: PROMO_BLACK,
                color: PROMO_WHITE,
                fontWeight: 900,
                fontSize: Math.round(discSize * 0.055),
                fontFamily: promoFontFamily,
                padding: "8px 20px",
                borderRadius: 999,
                letterSpacing: "0.02em",
                textTransform: brandLabel.match(/[a-z]/i) ? "none" : "uppercase",
              }}
            >
              {brandLabel}
            </div>
            {showTm && brand ? (
              <span
                style={{
                  fontFamily: promoFontFamily,
                  fontWeight: 800,
                  fontSize: Math.round(discSize * 0.035),
                  color: isWhite ? PROMO_BLACK : PROMO_WHITE,
                  marginTop: 2,
                }}
              >
                TM
              </span>
            ) : null}
          </div>
        ) : null}

        {headline ? (
          <div
            style={{
              fontSize: Math.round(discSize * 0.09),
              fontWeight: 900,
              color: bodyColor,
              fontFamily: promoFontFamily,
              textAlign: "center",
              lineHeight: 1.1,
              marginBottom: featureLines.length ? 10 : 0,
            }}
          >
            {headline}
          </div>
        ) : null}

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            alignItems: "center",
          }}
        >
          {featureLines.map((line, i) => {
            const lineSpring = spring({
              frame: frame - 5 - i * 3,
              fps,
              config: { damping: 16, stiffness: 140 },
            });
            return (
              <div
                key={`${line}-${i}`}
                style={{
                  opacity: lineSpring,
                  transform: `translateY(${interpolate(lineSpring, [0, 1], [10, 0])}px)`,
                  fontSize: Math.round(discSize * 0.065),
                  fontWeight: 900,
                  color: bodyColor,
                  fontFamily: promoFontFamily,
                  textAlign: "center",
                  lineHeight: 1.15,
                }}
              >
                {line}
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};
