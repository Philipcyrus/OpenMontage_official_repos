import React from "react";
import {
  AbsoluteFill,
  CalculateMetadataFunction,
  Img,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { Box, ScreenLayerSpec } from "./screenGeometry";
import { ScreenLayer, SubjectPlaceholder, usePandaFont } from "./ScreenLayer";

export type ScreenBackground =
  | { type: "image"; src: string }
  | { type: "color"; color: string }
  | { type: "none" };

export type PandaScreenOverlayProps = {
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
  /** Seconds; defaults to durationInFrames / fps. */
  sceneDuration?: number;
  /** "none" renders a transparent overlay for compositing over the Panda clip. */
  background?: ScreenBackground;
  /** Carousel / image stills: every layer and step in its settled state, no timing. */
  still?: boolean;
  layers: ScreenLayerSpec[];
  subjectZones?: Box[];
  language?: string;
  fontSrc?: string;
};

export const calculatePandaScreenOverlayMetadata: CalculateMetadataFunction<
  PandaScreenOverlayProps
> = async ({ props }) => ({
  width: Math.max(2, Math.round(props.width || 1080)),
  height: Math.max(2, Math.round(props.height || 1920)),
  fps: props.fps || 30,
  durationInFrames: Math.max(1, Math.round(props.durationInFrames || 1)),
});

export const ScreenBackgroundFill: React.FC<{ background?: ScreenBackground }> = ({
  background,
}) => {
  if (!background || background.type === "none") return null;
  if (background.type === "color") {
    return <AbsoluteFill style={{ background: background.color }} />;
  }
  return (
    <AbsoluteFill>
      <Img
        src={staticFile(background.src)}
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
      />
    </AbsoluteFill>
  );
};

export const PandaScreenOverlay: React.FC<PandaScreenOverlayProps> = (props) => {
  const frame = useCurrentFrame();
  const { fps, width, height, durationInFrames } = useVideoConfig();
  usePandaFont(props.fontSrc);
  const t = frame / fps;
  const sceneDuration = props.sceneDuration ?? durationInFrames / fps;
  return (
    <AbsoluteFill style={{ background: "transparent" }}>
      <ScreenBackgroundFill background={props.background} />
      {(props.subjectZones || []).map((z, i) => (
        <SubjectPlaceholder key={`subject-${i}`} zone={z} width={width} height={height} />
      ))}
      {(props.layers || []).map((spec, i) => (
        <ScreenLayer
          key={`layer-${i}`}
          spec={spec}
          width={width}
          height={height}
          t={t}
          sceneDuration={sceneDuration}
          preview={props.still === true}
          language={props.language || "zh"}
        />
      ))}
    </AbsoluteFill>
  );
};
