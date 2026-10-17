import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Logo } from "../components/Logo";
import { C, clamp, sans } from "../theme";

const LINES = ["Forgotten subscriptions.", "Price hikes nobody mentioned.", "Free trials that quietly turned paid."];

/** `skip` jumps past the opening lines (used by the short teaser). */
export const Hook: React.FC<{ skip?: number }> = ({ skip = 0 }) => {
  const f = useCurrentFrame() + skip;
  const lineA = (i: number) => interpolate(f, [i * 20, i * 20 + 16], [0, 1], clamp);
  const fadeA = interpolate(f, [78, 92], [1, 0], clamp);
  const b = interpolate(f, [92, 112], [0, 1], clamp);
  return (
    <AbsoluteFill style={{ background: C.dark, color: "#fff", fontFamily: sans, alignItems: "center", justifyContent: "center" }}>
      <div style={{ position: "absolute", opacity: fadeA, display: "flex", flexDirection: "column", gap: 22 }}>
        {LINES.map((t, i) => (
          <div
            key={t}
            style={{
              fontSize: 64,
              fontWeight: 600,
              letterSpacing: -1.5,
              opacity: lineA(i),
              transform: `translateY(${(1 - lineA(i)) * 26}px)`,
              color: i === 2 ? C.amber : "#fff",
            }}
          >
            {t}
          </div>
        ))}
      </div>
      <div
        style={{
          opacity: b,
          transform: `translateY(${(1 - b) * 30}px)`,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 26,
          textAlign: "center",
        }}
      >
        <Logo size={128} />
        <div style={{ fontSize: 120, fontWeight: 700, letterSpacing: -4 }}>Leak</div>
        <div style={{ fontSize: 40, color: C.darkMuted, maxWidth: 1100, lineHeight: 1.3 }}>
          finds the money quietly leaking out of your accounts —{" "}
          <span style={{ color: C.greenBright }}>without your data ever leaving your machine.</span>
        </div>
      </div>
    </AbsoluteFill>
  );
};
