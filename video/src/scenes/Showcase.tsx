import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from "remotion";
import { Window } from "../components/Window";
import { C, clamp, sans, useEnter } from "../theme";

const WIN_W = 1180;
const WIN_H = 760;

type Props = {
  kicker: string;
  title: React.ReactNode;
  bullets: string[];
  image: string;
  duration: number; // frames this scene lasts (drives the pan/zoom)
  motionStart?: number; // frame at which pan/zoom begins
  hero?: React.ReactNode; // big number between title and bullets
  pan?: [number, number]; // px, in window pixels
  zoom?: [number, number];
  origin?: string;
  overlay?: React.ReactNode;
};

/** Left: headline + bullets. Right: a screenshot in a browser frame with slow motion. */
export const Showcase: React.FC<Props> = ({ kicker, title, bullets, image, duration, motionStart = 24, hero, pan = [0, 0], zoom = [1, 1], origin, overlay }) => {
  const f = useCurrentFrame();
  const t = interpolate(f, [motionStart, duration - 20], [0, 1], { ...clamp, easing: Easing.inOut(Easing.cubic) });
  const win = useEnter(4);
  return (
    <AbsoluteFill style={{ background: C.paper, fontFamily: sans, color: C.ink, flexDirection: "row", alignItems: "center", padding: "0 80px", gap: 60 }}>
      <div style={{ width: 520, flexShrink: 0 }}>
        <div style={{ fontSize: 24, color: C.green, fontWeight: 700, letterSpacing: 3, textTransform: "uppercase", opacity: useEnter(0) }}>{kicker}</div>
        <div style={{ fontSize: 58, fontWeight: 700, letterSpacing: -2, lineHeight: 1.08, margin: "16px 0 30px", opacity: useEnter(4), transform: `translateY(${(1 - useEnter(4)) * 24}px)` }}>
          {title}
        </div>
        {hero}
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          {bullets.map((b, i) => {
            const p = useEnter(18 + i * 12);
            return (
              <div key={b} style={{ display: "flex", gap: 14, fontSize: 26, lineHeight: 1.35, color: C.muted, opacity: p, transform: `translateX(${(1 - p) * -20}px)` }}>
                <span style={{ color: C.green, fontWeight: 700 }}>✓</span>
                <span>{b}</span>
              </div>
            );
          })}
        </div>
      </div>
      <div style={{ opacity: win, transform: `translateX(${(1 - win) * 70}px)` }}>
        <Window
          image={image}
          width={WIN_W}
          height={WIN_H}
          panY={interpolate(t, [0, 1], pan)}
          zoom={interpolate(t, [0, 1], zoom)}
          origin={origin}
        >
          {overlay}
        </Window>
      </div>
    </AbsoluteFill>
  );
};

/** A rotated "not sent" stamp that slams onto the approvals screenshot. */
export const NotSentStamp: React.FC<{ delay: number }> = ({ delay }) => {
  const p = useEnter(delay, 12);
  return (
    <div
      style={{
        position: "absolute",
        right: 56,
        top: 70,
        transform: `rotate(-9deg) scale(${1.7 - 0.7 * p})`,
        opacity: p,
        border: `6px solid ${C.green}`,
        color: C.green,
        background: "rgba(226,243,232,0.92)",
        borderRadius: 14,
        padding: "10px 28px",
        fontFamily: sans,
        fontWeight: 800,
        fontSize: 46,
        letterSpacing: 2,
        textAlign: "center",
        lineHeight: 1.1,
      }}
    >
      DRAFT ONLY
      <div style={{ fontSize: 22, letterSpacing: 1, fontWeight: 600 }}>nothing is ever sent</div>
    </div>
  );
};

const SavingsCounter: React.FC = () => {
  const f = useCurrentFrame();
  const k = interpolate(f, [16, 64], [0, 1], { ...clamp, easing: Easing.out(Easing.cubic) });
  const shown = Math.round(24635 * k).toLocaleString("en-IN");
  return (
    <div style={{ margin: "-6px 0 34px", whiteSpace: "nowrap", opacity: interpolate(f, [12, 24], [0, 1], clamp) }}>
      <span style={{ fontSize: 92, fontWeight: 700, letterSpacing: -3, color: C.green, fontVariantNumeric: "tabular-nums" }}>₹{shown}</span>
      <span style={{ fontSize: 30, color: C.muted, marginLeft: 10 }}>/ year</span>
    </div>
  );
};

export const Dashboard: React.FC<{ duration: number }> = ({ duration }) => (
  <Showcase
    hero={<SavingsCounter />}
    motionStart={76}
    kicker="Findings"
    title={<>Savings you can <span style={{ color: C.green }}>check.</span></>}
    bullets={["Cancel, dispute or downgrade — ranked by ₹ per year", "Counted once per merchant: never double-counted"]}
    image="dashboard-full.png"
    duration={duration}
    pan={[0, 440]}
  />
);

export const Evidence: React.FC<{ duration: number }> = ({ duration }) => (
  <Showcase
    kicker="Grounded"
    title={<>Every claim cites <span style={{ color: C.green }}>its source.</span></>}
    bullets={["The exact statement line, email or SMS", "A finding with no valid citation is rejected by the harness", "Personal details masked — unmask locally, on demand"]}
    image="evidence.png"
    duration={duration}
    zoom={[1, 1.32]}
    origin="90% 0%"
  />
);

export const Approvals: React.FC<{ duration: number }> = ({ duration }) => (
  <Showcase
    kicker="You stay in control"
    title={<>There is no <span style={{ color: C.green }}>send</span> tool.</>}
    bullets={["The agent can only queue a Gmail draft or a reminder", "You read it, then press Approve", "Drafts land in your Gmail Drafts folder — never sent"]}
    image="approvals.png"
    duration={duration}
    zoom={[1, 1.06]}
    origin="30% 20%"
    overlay={<NotSentStamp delay={50} />}
  />
);
