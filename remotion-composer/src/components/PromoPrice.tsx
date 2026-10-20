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

type PromoPriceProps = {
  /** Primary price e.g. $12.50 */
  text: string;
  /** Secondary period e.g. /MO or 6-MONTH */
  subtitle?: string;
  /** Yellow-on-black pill line e.g. FROM JUST */
  pill?: string;
  tiltDeg?: number;
};

/**
 * Tilted price sticker — white disc, black ExtraBlack price, optional yellow pill.
 */
export const PromoPrice: React.FC<PromoPriceProps> = ({
  text,
  subtitle,
  pill,
  tiltDeg = -7,
}) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();

  const enter = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 130 },
  });

  const discSize = Math.round(Math.min(height * 0.5, 460));

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
          background: PROMO_WHITE,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: discSize * 0.1,
          boxSizing: "border-box",
          fontFamily: promoFontFamily,
        }}
      >
        <div
          style={{
            fontWeight: 900,
            fontSize: Math.round(discSize * 0.22),
            color: PROMO_BLACK,
            lineHeight: 0.95,
            letterSpacing: "-0.03em",
            textAlign: "center",
          }}
        >
          {text}
        </div>
        {subtitle ? (
          <div
            style={{
              fontWeight: 800,
              fontSize: Math.round(discSize * 0.08),
              color: PROMO_BLACK,
              marginTop: 6,
              textAlign: "center",
              letterSpacing: "0.02em",
            }}
          >
            {subtitle}
          </div>
        ) : null}
        {pill ? (
          <div
            style={{
              marginTop: 16,
              background: PROMO_BLACK,
              color: PROMO_YELLOW,
              fontWeight: 900,
              fontSize: Math.round(discSize * 0.055),
              padding: "10px 22px",
              borderRadius: 999,
              letterSpacing: "0.04em",
              textTransform: "uppercase",
            }}
          >
            {pill}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
