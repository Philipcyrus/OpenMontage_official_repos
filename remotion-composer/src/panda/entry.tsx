// Render entry for the Panda screenshot compositions only (tools/video/screen_overlay.py).
// src/index.tsx bundles every composition, and several of them download Google Fonts when the
// bundle loads — a network blip then fails an unrelated screenshot render. This entry bundles
// nothing but the Panda compositions, which use the local CJK font.
import { registerRoot } from "remotion";
import { PandaCompositions } from "./PandaCompositions";

registerRoot(PandaCompositions);
