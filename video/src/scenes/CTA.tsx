import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Logo } from "../components/Logo";
import { C, CMD, clamp, mono, sans, useEnter } from "../theme";

export const CTA: React.FC = () => {
  const f = useCurrentFrame();
  const typed = CMD.slice(0, Math.floor(interpolate(f, [14, 14 + CMD.length * 2.2], [0, CMD.length], clamp)));
  const done = f > 14 + CMD.length * 2.2;
  const out = useEnter(14 + Math.round(CMD.length * 2.2) + 8);
  const cursor = Math.floor(f / 15) % 2 === 0;
  return (
    <AbsoluteFill style={{ background: C.dark, color: "#fff", fontFamily: sans, alignItems: "center", justifyContent: "center", gap: 36 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 22, opacity: useEnter(0) }}>
        <Logo size={84} />
        <div style={{ fontSize: 84, fontWeight: 700, letterSpacing: -3 }}>Leak</div>
      </div>
      <div style={{ width: 1000, borderRadius: 18, background: "#050906", border: `1px solid ${C.darkBorder}`, padding: "34px 40px", fontFamily: mono, fontSize: 46, opacity: useEnter(4) }}>
        <div>
          <span style={{ color: C.greenBright }}>$ </span>
          {typed}
          {!done && cursor && <span style={{ color: C.greenBright }}>▌</span>}
        </div>
        <div style={{ fontSize: 32, color: C.greenBright, marginTop: 22, height: 44, opacity: out }}>₹24,635/year in potential savings</div>
      </div>
      <div style={{ display: "flex", gap: 22, opacity: useEnter(60) }}>
        {["Local-first", "No send tool", "Open source · MIT"].map((t) => (
          <div key={t} style={{ fontSize: 28, padding: "10px 26px", borderRadius: 999, border: `1px solid ${C.darkBorder}`, color: C.darkMuted }}>
            {t}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
