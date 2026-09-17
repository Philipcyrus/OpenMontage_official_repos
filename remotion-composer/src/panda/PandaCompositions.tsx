import React from "react";
import { Composition } from "remotion";
import {
  PandaScreenOverlay,
  PandaScreenOverlayProps,
  calculatePandaScreenOverlayMetadata,
} from "./PandaScreenOverlay";
import {
  PandaScreenBoard,
  PandaScreenBoardProps,
  calculatePandaScreenBoardMetadata,
} from "./PandaScreenBoard";

/** Panda: user screenshots laid over generated shots (tools/video/screen_overlay.py). */
export const PandaCompositions: React.FC = () => (
  <>
    <Composition
      id="PandaScreenOverlay"
      component={PandaScreenOverlay}
      durationInFrames={150}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={{
        width: 1080,
        height: 1920,
        fps: 30,
        durationInFrames: 150,
        background: { type: "none" },
        layers: [],
        language: "zh",
      } as PandaScreenOverlayProps}
      calculateMetadata={calculatePandaScreenOverlayMetadata}
    />
    <Composition
      id="PandaScreenBoard"
      component={PandaScreenBoard}
      durationInFrames={1}
      fps={30}
      width={1200}
      height={800}
      defaultProps={{ cells: [], language: "zh" } as PandaScreenBoardProps}
      calculateMetadata={calculatePandaScreenBoardMetadata}
    />
  </>
);
