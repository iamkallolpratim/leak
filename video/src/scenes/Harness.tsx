import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { C, clamp, mono, sans, useEnter } from "../theme";

const NODES = [
  { t: "Budget", s: "steps · $ · ₹ ceilings" },
  { t: "Egress guard", s: "redact + re-scan" },
  { t: "Gemini", s: "plan · call tools" },
  { t: "Permissions", s: "per tool, in code" },
  { t: "Observe", s: "result → context" },
];
const OUTCOMES = [
  { k: "auto", v: "runs immediately", c: C.greenBright },
  { k: "needs_approval", v: "queued for you — never executed", c: C.amber },
  { k: "forbidden", v: "never shown to the model", c: C.red },
];

export const Harness: React.FC = () => {
  const f = useCurrentFrame();
  const active = Math.floor(Math.max(0, f - 36) / 24) % NODES.length;
  const out = useEnter(84);
  return (
    <AbsoluteFill style={{ background: C.dark, color: "#fff", fontFamily: sans, padding: "80px 100px" }}>
      <div style={{ fontSize: 28, color: C.greenBright, fontWeight: 600, letterSpacing: 3, textTransform: "uppercase" }}>The agent harness</div>
      <div style={{ fontSize: 66, fontWeight: 700, letterSpacing: -2, marginTop: 14, lineHeight: 1.1 }}>
        One loop. Built by hand.
        <span style={{ color: C.darkMuted }}> No framework.</span>
      </div>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", gap: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
        {NODES.map((n, i) => {
          const p = interpolate(f, [10 + i * 7, 26 + i * 7], [0, 1], clamp);
          const on = f > 36 && i === active;
          return (
            <div key={n.t} style={{ display: "flex", alignItems: "center", gap: 18, flex: 1 }}>
              <div
                style={{
                  flex: 1,
                  padding: "26px 22px",
                  borderRadius: 18,
                  background: on ? "rgba(29,122,71,0.35)" : C.darkCard,
                  border: `2px solid ${on ? C.greenBright : C.darkBorder}`,
                  opacity: p,
                  transform: `translateY(${(1 - p) * 24}px) scale(${on ? 1.05 : 1})`,
                  transition: "none",
                  boxShadow: on ? "0 0 50px rgba(74,222,128,0.25)" : "none",
                }}
              >
                <div style={{ fontSize: 32, fontWeight: 700 }}>{n.t}</div>
                <div style={{ fontSize: 20, color: C.darkMuted, marginTop: 6 }}>{n.s}</div>
              </div>
              {i < NODES.length - 1 && <div style={{ color: C.darkMuted, fontSize: 30, opacity: p }}>›</div>}
            </div>
          );
        })}
      </div>
      <div style={{ color: C.darkMuted, fontFamily: mono, fontSize: 22, marginTop: 22, textAlign: "right", opacity: interpolate(f, [60, 80], [0, 1], clamp) }}>
        ↺ repeat until the model stops calling tools — or a budget runs out
      </div>
      <div style={{ display: "flex", gap: 22, marginTop: 56, opacity: out, transform: `translateY(${(1 - out) * 24}px)` }}>
        {OUTCOMES.map((o) => (
          <div key={o.k} style={{ flex: 1, padding: "22px 26px", borderRadius: 16, background: C.darkCard, borderLeft: `6px solid ${o.c}` }}>
            <div style={{ fontFamily: mono, fontSize: 26, fontWeight: 700, color: o.c }}>{o.k}</div>
            <div style={{ fontSize: 23, color: C.darkMuted, marginTop: 6 }}>{o.v}</div>
          </div>
        ))}
      </div>
      </div>
      <div style={{ fontSize: 28, color: C.darkMuted, opacity: useEnter(110) }}>
        833 lines in <span style={{ color: "#fff", fontFamily: mono }}>leak/harness/</span> · tools · permissions · budgets · context · traces · grounding · <span style={{ color: "#fff" }}>mypy --strict</span>
      </div>
    </AbsoluteFill>
  );
};
