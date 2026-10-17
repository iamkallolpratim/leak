import { Img, staticFile } from "remotion";
import { C, mono } from "../theme";

type Props = {
  image: string;
  width: number;
  height: number; // visible content height
  /** translateY in px (positive = scrolled down). */
  panY?: number;
  zoom?: number;
  origin?: string;
  children?: React.ReactNode; // overlays, positioned relative to the content area
};

/** A browser-style frame around a screenshot, with optional scroll and zoom. */
export const Window: React.FC<Props> = ({ image, width, height, panY = 0, zoom = 1, origin = "50% 0%", children }) => (
  <div
    style={{
      width,
      borderRadius: 16,
      overflow: "hidden",
      background: C.card,
      boxShadow: "0 40px 100px rgba(10,30,20,0.28), 0 0 0 1px rgba(0,0,0,0.08)",
    }}
  >
    <div style={{ height: 44, background: "#eceae4", display: "flex", alignItems: "center", gap: 8, padding: "0 16px" }}>
      {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
        <span key={c} style={{ width: 12, height: 12, borderRadius: 6, background: c }} />
      ))}
      <div
        style={{
          marginLeft: 16,
          flex: 1,
          height: 26,
          borderRadius: 8,
          background: "#fff",
          fontFamily: mono,
          fontSize: 14,
          color: C.muted,
          display: "flex",
          alignItems: "center",
          padding: "0 12px",
        }}
      >
        🔒 127.0.0.1:8765
      </div>
    </div>
    <div style={{ position: "relative", height, overflow: "hidden", background: C.paper }}>
      <Img
        src={staticFile(image)}
        style={{
          width,
          display: "block",
          transform: `translateY(${-panY}px) scale(${zoom})`,
          transformOrigin: origin,
        }}
      />
      {children}
    </div>
  </div>
);
