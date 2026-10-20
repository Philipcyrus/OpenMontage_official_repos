import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  PROMO_BLACK,
  promoFontFamily,
} from "./promoFonts";

type PromoCaptionProps = {
  text: string;
  /** black by default — readable on yellow/cream underlays */
  color?: string;
  /** bottom (default) or top */
  position?: string;
};

/**
 * Quiet VO-sync line — small, centered, no box.
 * Default black for contrast on yellow/white panda scenes.
 */
export const PromoCaption: React.FC<PromoCaptionProps> = ({
  text,
  color = PROMO_BLACK,
  position = "bottom",
}) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();

  const enter = spring({
    frame,
    fps,
    config: { damping: 18, stiffness: 120 },
  });

  const isTop = position.includes("top");
  const fill = color || PROMO_BLACK;

  return (
    <AbsoluteFill
      style={{
        justifyContent: isTop ? "flex-start" : "flex-end",
        alignItems: "center",
        paddingBottom: isTop ? 0 : Math.round(height * 0.1),
        paddingTop: isTop ? Math.round(height * 0.06) : 0,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          opacity: enter,
          transform: `translateY(${interpolate(enter, [0, 1], [8, 0])}px)`,
          fontFamily: promoFontFamily,
          fontWeight: 800,
          fontSize: Math.round(height * 0.038),
          color: fill,
          textAlign: "center",
          letterSpacing: "0.02em",
          maxWidth: "88%",
          lineHeight: 1.25,
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};
