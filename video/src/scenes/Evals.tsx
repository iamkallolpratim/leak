import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from "remotion";
import { C, clamp, sans, useEnter } from "../theme";

type Stat = { value: number; decimals?: number; prefix?: string; suffix?: string; label: string; sub: string };
const STATS: Stat[] = [
  { value: 1, decimals: 3, label: "extraction F1", sub: "42 of 42 · 0 false positives" },
  { value: 18, suffix: "/18", label: "planted leaks found", sub: "precision 1.000 · recall 1.000" },
  { value: 0, prefix: "₹", label: "to re-run", sub: "content-hash cache · statements parsed by code" },
  { value: 29, prefix: "₹", label: "one auditor run", sub: "Gemini Pro · 20 steps · 67% cached input" },
];

const Card: React.FC<{ s: Stat; delay: number }> = ({ s, delay }) => {
  const f = useCurrentFrame();
  const p = useEnter(delay);
  const k = interpolate(f, [delay + 6, delay + 40], [0, 1], { ...clamp, easing: Easing.out(Easing.cubic) });
  const shown = (s.value * k).toFixed(s.decimals ?? 0);
  return (
    <div style={{ flex: 1, padding: "46px 36px", borderRadius: 22, background: C.darkCard, border: `1px solid ${C.darkBorder}`, opacity: p, transform: `translateY(${(1 - p) * 30}px)` }}>
      <div style={{ fontSize: 112, fontWeight: 700, letterSpacing: -4, color: C.greenBright, fontVariantNumeric: "tabular-nums" }}>
        {s.prefix}
        {shown}
        <span style={{ fontSize: 52, color: C.darkMuted }}>{s.suffix}</span>
      </div>
      <div style={{ fontSize: 30, fontWeight: 600, marginTop: 8 }}>{s.label}</div>
      <div style={{ fontSize: 20, color: C.darkMuted, marginTop: 8, lineHeight: 1.4 }}>{s.sub}</div>
    </div>
  );
};

export const Evals: React.FC = () => (
  <AbsoluteFill style={{ background: C.dark, color: "#fff", fontFamily: sans, padding: "90px 100px" }}>
    <div style={{ fontSize: 28, color: C.greenBright, fontWeight: 600, letterSpacing: 3, textTransform: "uppercase" }}>Measured</div>
    <div style={{ fontSize: 66, fontWeight: 700, letterSpacing: -2, marginTop: 14, lineHeight: 1.1 }}>Cheap, because code goes first.</div>
    <div style={{ flex: 1, display: "flex", alignItems: "center" }}>
      <div style={{ display: "flex", gap: 26, width: "100%" }}>
        {STATS.map((s, i) => (
          <Card key={s.label} s={s} delay={8 + i * 10} />
        ))}
      </div>
    </div>
    <div style={{ fontSize: 24, color: C.darkMuted, opacity: useEnter(70) }}>
      Synthetic fixtures: read these as “no regressions”, not “works on every inbox”.
    </div>
  </AbsoluteFill>
);
