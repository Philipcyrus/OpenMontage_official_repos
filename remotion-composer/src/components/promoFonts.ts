import { loadFont } from "@remotion/google-fonts/Montserrat";

/**
 * Panda Mobile promo caption tokens — flat yellow ExtraBlack, black/white discs.
 * Matches brand refs (YES punch type, OnePool circle badges, quiet bottom captions).
 */
const { fontFamily } = loadFont("normal", {
  weights: ["800", "900"],
  subsets: ["latin"],
});

export const promoFontFamily = fontFamily;

export const PROMO_YELLOW = "#fdc50d";
export const PROMO_BLACK = "#000000";
export const PROMO_WHITE = "#FFFFFF";
