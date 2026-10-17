import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";
import { spring, useCurrentFrame, useVideoConfig } from "remotion";

const subsets = ["latin", "latin-ext"] as const; // latin-ext carries the ₹ glyph
export const sans = loadInter("normal", { weights: ["400", "500", "600", "700"], subsets: [...subsets] }).fontFamily;
export const mono = loadMono("normal", { weights: ["400", "500", "700"], subsets: [...subsets] }).fontFamily;

export const W = 1920;
export const H = 1080;
export const FPS = 30;
export const TRANSITION = 12; // frames of cross-fade between scenes

// Change this one line once the package is published to PyPI (`uvx leak-audit demo`).
export const CMD = "uv run leak demo";

export const C = {
  dark: "#0c130f",
  darkCard: "#141e18",
  darkBorder: "#25332b",
  darkMuted: "#8aa194",
  paper: "#faf9f6",
  card: "#ffffff",
  border: "#e6e3dc",
  ink: "#111713",
  muted: "#5f6b64",
  green: "#1d7a47",
  greenBright: "#4ade80",
  greenSoft: "#e2f3e8",
  amber: "#f2c14e",
  red: "#ef5350",
  redSoft: "#fde8e7",
  blue: "#4f9cf9",
};

/** 0 → 1 spring, starting `delay` frames into the current sequence. */
export const useEnter = (delay = 0, damping = 200) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return spring({ frame: frame - delay, fps, config: { damping } });
};

export const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
