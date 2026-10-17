import { Composition } from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { Fragment } from "react";
import { CTA } from "./scenes/CTA";
import { Evals } from "./scenes/Evals";
import { Harness } from "./scenes/Harness";
import { Hook } from "./scenes/Hook";
import { Pipeline } from "./scenes/Pipeline";
import { Redact } from "./scenes/Redact";
import { Approvals, Dashboard, Evidence } from "./scenes/Showcase";
import { FPS, H, TRANSITION, W } from "./theme";

type Scene = { id: string; frames: number; render: (frames: number) => React.ReactNode };

// Full walkthrough (~49s) and a short teaser (~16s) for the README GIF.
const FULL: Scene[] = [
  { id: "hook", frames: 150, render: () => <Hook /> },
  { id: "pipeline", frames: 150, render: () => <Pipeline /> },
  { id: "redact", frames: 190, render: () => <Redact /> },
  { id: "dashboard", frames: 210, render: (n) => <Dashboard duration={n} /> },
  { id: "evidence", frames: 180, render: (n) => <Evidence duration={n} /> },
  { id: "approvals", frames: 180, render: (n) => <Approvals duration={n} /> },
  { id: "harness", frames: 210, render: () => <Harness /> },
  { id: "evals", frames: 150, render: () => <Evals /> },
  { id: "cta", frames: 150, render: () => <CTA /> },
];
const TEASER: Scene[] = [
  { id: "hook", frames: 60, render: () => <Hook skip={93} /> },
  { id: "redact", frames: 140, render: () => <Redact /> },
  { id: "dashboard", frames: 120, render: (n) => <Dashboard duration={n} /> },
  { id: "approvals", frames: 120, render: (n) => <Approvals duration={n} /> },
  { id: "cta", frames: 90, render: () => <CTA /> },
];

const total = (scenes: Scene[]) => scenes.reduce((a, s) => a + s.frames, 0) - (scenes.length - 1) * TRANSITION;

// Scenes are bound here, not passed as props: Remotion serializes props to JSON,
// which would drop the render functions.
const film = (scenes: Scene[]): React.FC => () => (
  <TransitionSeries>
    {scenes.map((s, i) => (
      <Fragment key={s.id}>
        {i > 0 && <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: TRANSITION })} />}
        <TransitionSeries.Sequence durationInFrames={s.frames}>{s.render(s.frames)}</TransitionSeries.Sequence>
      </Fragment>
    ))}
  </TransitionSeries>
);
const FullFilm = film(FULL);
const TeaserFilm = film(TEASER);

export const Root: React.FC = () => (
  <>
    <Composition id="LeakDemo" component={FullFilm} durationInFrames={total(FULL)} fps={FPS} width={W} height={H} />
    <Composition id="LeakTeaser" component={TeaserFilm} durationInFrames={total(TEASER)} fps={FPS} width={W} height={H} />
  </>
);
