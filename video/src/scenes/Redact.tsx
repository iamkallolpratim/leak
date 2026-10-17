import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { C, clamp, mono, sans, useEnter } from "../theme";

// All synthetic: the demo persona's made-up identifiers.
type Seg = { raw: string; token?: string };
const SEGS: Seg[] = [
  { raw: "Dear " },
  { raw: "Asha Testkumar", token: "[NAME_1]" },
  { raw: ", Rs.649.00 debited from a/c " },
  { raw: "50100123456789", token: "[ACCT_1]" },
  { raw: " to VPA netflix@hdfcbank on 05-04-26. Registered mobile " },
  { raw: "+91 98765 00001", token: "[PHONE_1]" },
  { raw: ", PAN " },
  { raw: "ABCPT1234F", token: "[PAN_1]" },
  { raw: "." },
];
const T0 = 46; // first token appears
const STEP = 14;

const Panel: React.FC<{ title: string; sub: string; tone: "raw" | "safe"; children: React.ReactNode; delay: number }> = ({ title, sub, tone, children, delay }) => {
  const p = useEnter(delay);
  const raw = tone === "raw";
  return (
    <div
      style={{
        flex: 1,
        borderRadius: 22,
        padding: "44px 40px",
        background: C.darkCard,
        border: `2px solid ${raw ? C.darkBorder : C.green}`,
        opacity: p,
        transform: `translateY(${(1 - p) * 30}px)`,
        boxShadow: raw ? "none" : "0 0 70px rgba(74,222,128,0.14)",
      }}
    >
      <div style={{ fontSize: 30, fontWeight: 700, color: raw ? "#fff" : C.greenBright }}>{title}</div>
      <div style={{ fontSize: 21, color: C.darkMuted, margin: "4px 0 24px" }}>{sub}</div>
      <div style={{ fontFamily: mono, fontSize: 34, lineHeight: 1.8, color: "#d9e3dd" }}>{children}</div>
    </div>
  );
};

export const Redact: React.FC = () => {
  const f = useCurrentFrame();
  const foot = useEnter(120);
  let pii = -1;
  const left = SEGS.map((s, i) => {
    if (!s.token) return <span key={i}>{s.raw}</span>;
    pii += 1;
    const t = T0 + pii * STEP;
    const blur = interpolate(f, [t, t + 10], [0, 9], clamp);
    const hi = interpolate(f, [t - 14, t], [0, 1], clamp);
    return (
      <span key={i} style={{ background: `rgba(239,83,80,${0.28 * hi})`, borderRadius: 6, padding: "0 4px", filter: `blur(${blur}px)` }}>
        {s.raw}
      </span>
    );
  });
  pii = -1;
  const right = SEGS.map((s, i) => {
    if (!s.token) return <span key={i}>{s.raw}</span>;
    pii += 1;
    const t = T0 + pii * STEP;
    const p = interpolate(f, [t, t + 8], [0, 1], clamp);
    return (
      <span
        key={i}
        style={{
          display: "inline-block",
          opacity: p,
          transform: `scale(${0.8 + 0.2 * p})`,
          background: "rgba(74,222,128,0.16)",
          color: C.greenBright,
          borderRadius: 6,
          padding: "0 8px",
          fontWeight: 700,
        }}
      >
        {s.token}
      </span>
    );
  });
  return (
    <AbsoluteFill style={{ background: C.dark, color: "#fff", fontFamily: sans, padding: "80px 100px" }}>
      <div style={{ fontSize: 28, color: C.greenBright, fontWeight: 600, letterSpacing: 3, textTransform: "uppercase" }}>Privacy</div>
      <div style={{ fontSize: 66, fontWeight: 700, letterSpacing: -2, marginTop: 14, lineHeight: 1.1 }}>
        The model never sees who you are.
      </div>
      <div style={{ display: "flex", gap: 36, marginTop: 70, alignItems: "stretch" }}>
        <Panel tone="raw" title="On your machine" sub="what the SMS actually says" delay={8}>{left}</Panel>
        <div style={{ alignSelf: "center", fontSize: 54, color: C.greenBright, opacity: useEnter(30) }}>→</div>
        <Panel tone="safe" title="Sent to the LLM" sub="after masking — merchants stay readable" delay={20}>{right}</Panel>
      </div>
      <div style={{ marginTop: "auto", display: "flex", gap: 28, opacity: foot, transform: `translateY(${(1 - foot) * 20}px)` }}>
        {["Fail-closed egress guard: still looks like PII? The call is refused.", "Reversible mapping stays in a local vault (chmod 600)."].map((t) => (
          <div key={t} style={{ flex: 1, fontSize: 24, color: C.darkMuted, borderLeft: `4px solid ${C.green}`, paddingLeft: 20, lineHeight: 1.4 }}>
            {t}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
