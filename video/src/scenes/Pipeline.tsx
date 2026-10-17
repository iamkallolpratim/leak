import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { C, clamp, sans, useEnter } from "../theme";

const SOURCES = [
  { t: "Email", s: "22 receipts, renewals, price notices", icon: "✉" },
  { t: "Bank & card statements", s: "HDFC · ICICI · SBI · Axis — password-protected PDFs", icon: "▤" },
  { t: "SMS alerts", s: "21 bank messages (OTPs dropped on arrival)", icon: "✆" },
];

const Arrow: React.FC<{ delay: number }> = ({ delay }) => {
  const f = useCurrentFrame();
  const p = interpolate(f, [delay, delay + 18], [0, 1], clamp);
  return (
    <div style={{ width: 120, display: "flex", alignItems: "center", opacity: p }}>
      <div style={{ height: 4, borderRadius: 2, background: C.greenBright, width: `${p * 100}%` }} />
      <div style={{ borderLeft: `14px solid ${C.greenBright}`, borderTop: "9px solid transparent", borderBottom: "9px solid transparent" }} />
    </div>
  );
};

const Box: React.FC<{ delay: number; accent?: boolean; children: React.ReactNode; w?: number }> = ({ delay, accent, children, w = 400 }) => {
  const p = useEnter(delay);
  return (
    <div
      style={{
        width: w,
        padding: "28px 30px",
        borderRadius: 20,
        background: accent ? `linear-gradient(150deg, ${C.green}, #11512e)` : C.darkCard,
        border: `1px solid ${accent ? "transparent" : C.darkBorder}`,
        opacity: p,
        transform: `translateY(${(1 - p) * 30}px)`,
        boxShadow: accent ? "0 20px 70px rgba(29,122,71,0.4)" : "none",
      }}
    >
      {children}
    </div>
  );
};

export const Pipeline: React.FC = () => (
  <AbsoluteFill style={{ background: C.dark, color: "#fff", fontFamily: sans, padding: "90px 100px" }}>
    <div style={{ fontSize: 28, color: C.greenBright, fontWeight: 600, letterSpacing: 3, textTransform: "uppercase" }}>How it works</div>
    <div style={{ fontSize: 68, fontWeight: 700, letterSpacing: -2, marginTop: 14, lineHeight: 1.1 }}>
      Reads what you already have.
      <br />
      <span style={{ color: C.darkMuted }}>Keeps it on your machine.</span>
    </div>
    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 30 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {SOURCES.map((s, i) => (
          <Box key={s.t} delay={10 + i * 8} w={520}>
            <div style={{ display: "flex", gap: 20, alignItems: "center" }}>
              <div style={{ fontSize: 40, width: 56, color: C.greenBright }}>{s.icon}</div>
              <div>
                <div style={{ fontSize: 30, fontWeight: 600 }}>{s.t}</div>
                <div style={{ fontSize: 21, color: C.darkMuted, marginTop: 4 }}>{s.s}</div>
              </div>
            </div>
          </Box>
        ))}
      </div>
      <Arrow delay={42} />
      <Box delay={50} accent w={360}>
        <div style={{ fontSize: 26, opacity: 0.8 }}>Redact</div>
        <div style={{ fontSize: 40, fontWeight: 700, margin: "6px 0 10px" }}>PII → tokens</div>
        <div style={{ fontSize: 20, lineHeight: 1.45, opacity: 0.9 }}>PAN · Aadhaar · cards · accounts · phones · emails · addresses</div>
      </Box>
      <Arrow delay={74} />
      <Box delay={82} w={380}>
        <div style={{ fontSize: 26, color: C.darkMuted }}>Stored in</div>
        <div style={{ fontSize: 40, fontWeight: 700, margin: "6px 0 10px" }}>One SQLite file</div>
        <div style={{ fontSize: 20, lineHeight: 1.45, color: C.darkMuted }}>
          .leak/ on your disk. No Leak server. No telemetry. The token ↔ value vault never leaves.
        </div>
      </Box>
    </div>
    <div style={{ fontSize: 26, color: C.darkMuted, opacity: useEnter(110) }}>
      Parsing is deterministic code first — the LLM only handles the messy parts.
    </div>
  </AbsoluteFill>
);
