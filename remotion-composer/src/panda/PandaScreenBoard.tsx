import React from "react";
import { AbsoluteFill, CalculateMetadataFunction } from "remotion";
import { Box, ScreenLayerSpec } from "./screenGeometry";
import { ScreenBackground, ScreenBackgroundFill } from "./PandaScreenOverlay";
import {
  PANDA_FONT_STACK,
  ScreenLayer,
  SubjectPlaceholder,
  usePandaFont,
} from "./ScreenLayer";

export type BoardCell = {
  label: string;
  canvas: { width: number; height: number };
  sceneDuration?: number;
  /**
   * Scene-local second to draw this cell at. Set it for a cell that shows ONE display window of a
   * scene that has several: the layers are then drawn as they really are at that moment (a layer
   * whose show window has not opened is not drawn), so a later screenshot cannot hide an earlier
   * one. Left out, every layer passed is drawn settled, which is what a still or a set of
   * screenshots shown together should look like.
   */
  atSeconds?: number;
  background?: ScreenBackground;
  layers: ScreenLayerSpec[];
  subjectZones?: Box[];
  note?: string;
};

export type PandaScreenBoardProps = {
  title?: string;
  cells: BoardCell[];
  columns?: number;
  cellWidth?: number;
  language?: string;
  fontSrc?: string;
};

const PAD = 32;
const GAP = 28;
const LABEL_H = 100;
const TITLE_H = 72;

const layoutOf = (props: PandaScreenBoardProps) => {
  const cells = props.cells || [];
  const columns = Math.max(1, Math.min(props.columns || 4, Math.max(1, cells.length)));
  const cellWidth = props.cellWidth || 360;
  const first = cells[0]?.canvas || { width: 1080, height: 1920 };
  const cellH = Math.round((cellWidth * first.height) / first.width);
  const rows = Math.max(1, Math.ceil(cells.length / columns));
  const titleH = props.title ? TITLE_H : 0;
  const width = PAD * 2 + columns * cellWidth + (columns - 1) * GAP;
  const height = PAD * 2 + titleH + rows * (cellH + LABEL_H) + (rows - 1) * GAP;
  return { columns, cellWidth, cellH, width, height, titleH };
};

export const calculatePandaScreenBoardMetadata: CalculateMetadataFunction<
  PandaScreenBoardProps
> = async ({ props }) => {
  const l = layoutOf(props);
  return { width: l.width, height: l.height, fps: 30, durationInFrames: 1 };
};

export const PandaScreenBoard: React.FC<PandaScreenBoardProps> = (props) => {
  usePandaFont(props.fontSrc);
  const l = layoutOf(props);
  const cells = props.cells || [];
  return (
    <AbsoluteFill style={{ background: "#ffffff", fontFamily: PANDA_FONT_STACK }}>
      {props.title ? (
        <div
          style={{
            position: "absolute",
            left: PAD,
            top: PAD,
            width: l.width - PAD * 2,
            height: l.titleH,
            fontSize: 28,
            fontWeight: 700,
            color: "#111111",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {props.title}
        </div>
      ) : null}
      {cells.map((cell, i) => {
        const col = i % l.columns;
        const row = Math.floor(i / l.columns);
        const x = PAD + col * (l.cellWidth + GAP);
        const y = PAD + l.titleH + row * (l.cellH + LABEL_H + GAP);
        const W = cell.canvas.width;
        const H = cell.canvas.height;
        const scale = l.cellWidth / W;
        return (
          <div key={`cell-${i}`} style={{ position: "absolute", left: x, top: y }}>
            <div
              style={{
                position: "relative",
                width: l.cellWidth,
                height: Math.round(H * scale),
                overflow: "hidden",
                border: "2px solid #d6d9dd",
                boxSizing: "content-box",
                background: "#ffffff",
              }}
            >
              <div
                style={{
                  position: "absolute",
                  left: 0,
                  top: 0,
                  width: W,
                  height: H,
                  transform: `scale(${scale})`,
                  transformOrigin: "0 0",
                }}
              >
                <ScreenBackgroundFill background={cell.background} />
                {(cell.subjectZones || []).map((z, j) => (
                  <SubjectPlaceholder key={`s-${j}`} zone={z} width={W} height={H} />
                ))}
                {(cell.layers || []).map((spec, j) => (
                  <ScreenLayer
                    key={`l-${j}`}
                    spec={spec}
                    width={W}
                    height={H}
                    t={cell.atSeconds ?? 0}
                    sceneDuration={cell.sceneDuration ?? 5}
                    preview={cell.atSeconds === undefined}
                    language={props.language || "zh"}
                  />
                ))}
              </div>
            </div>
            <div
              style={{
                width: l.cellWidth,
                height: LABEL_H,
                paddingTop: 10,
                boxSizing: "border-box",
                fontSize: 18,
                lineHeight: 1.25,
                color: cell.note ? "#b42318" : "#111111",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  fontWeight: 700,
                  color: "#111111",
                  display: "-webkit-box",
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: "vertical",
                  overflow: "hidden",
                }}
              >
                {cell.label}
              </div>
              {cell.note ? (
                <div
                  style={{
                    fontSize: 16,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {cell.note}
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
