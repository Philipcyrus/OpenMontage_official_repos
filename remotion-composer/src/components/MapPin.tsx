import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  PROMO_BLACK,
  PROMO_YELLOW,
  promoFontFamily,
} from "./promoFonts";

type MapPinProps = {
  text: string;
  /** Normalized 0–1 position; defaults to lower-center pump. */
  x?: number;
  y?: number;
  accentColor?: string;
};

/**
 * Destination name that pumps on a lit map country.
 * Black label + yellow ExtraBlack type (no navy chip).
 */
export const MapPin: React.FC<MapPinProps> = ({
  text,
  x = 0.5,
  y = 0.62,
  accentColor = PROMO_YELLOW,
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();

  const pump = spring({
    frame,
    fps,
    config: { damping: 9, stiffness: 180, mass: 0.55 },
  });
  const bounce = spring({
    frame: frame - Math.round(fps * 0.35),
    fps,
    config: { damping: 10, stiffness: 200, mass: 0.45 },
  });
  const scale =
    interpolate(pump, [0, 1], [0.4, 1]) *
    interpolate(bounce, [0, 1], [1.08, 1]);

  const left = Math.max(24, Math.min(width - 24, x * width));
  const top = Math.max(24, Math.min(height - 24, y * height));

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div
        style={{
          position: "absolute",
          left,
          top,
          transform: `translate(-50%, -50%) scale(${scale})`,
          opacity: pump,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 8,
        }}
      >
        <div
          style={{
            width: 12,
            height: 12,
            borderRadius: "50%",
            background: accentColor,
          }}
        />
        <div
          style={{
            background: PROMO_BLACK,
            color: accentColor,
            fontWeight: 900,
            fontSize: 28,
            fontFamily: promoFontFamily,
            padding: "10px 18px",
            borderRadius: 12,
            letterSpacing: "0.04em",
            textTransform: "uppercase",
            whiteSpace: "nowrap",
          }}
        >
          {text}
        </div>
      </div>
    </AbsoluteFill>
  );
};
