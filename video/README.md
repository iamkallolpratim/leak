# Leak launch video (Remotion)

Source for `docs/assets/leak-demo.mp4` and `demo.gif`. Scenes are React components in
`src/scenes/`; `src/Root.tsx` defines two compositions: `LeakDemo` (~49 s) and `LeakTeaser`
(~16 s, for the README GIF). Screenshots are read from `docs/assets/` (`remotion.config.ts`
sets it as the public dir).

```bash
cd video && npm install
npx remotion browser ensure     # once: downloads Remotion's headless Chrome

# 1. Screenshots (refuses to run unless the server says demo: true)
uv run leak demo --no-agent --no-serve   # or reuse a demo DB that has an auditor run
uv run leak serve --demo --no-open &     # http://127.0.0.1:8765
npm run shots                            # → docs/assets/*.png

# 2. Video
npm run studio                           # live preview
npm run render                           # → out/leak-demo.mp4
npm run gif                              # → out/leak-demo.gif (10 fps, half size)
cp out/leak-demo.mp4 ../docs/assets/leak-demo.mp4 && cp out/leak-demo.gif ../docs/assets/demo.gif
```

Notes

* **Demo data only.** `capture.mjs` refuses to run unless the server reports `demo: true`. Demo
  runs keep their traces in `.leak/demo_traces/` (real runs in `.leak/traces/`), and the script
  still picks an explicit demo run (ledger of 155 entries) instead of "the latest". Pass
  `--run run_xxx` to choose another.
* The approvals shot queues two drafts through the UI, screenshots them, then rejects exactly
  those two, so the demo database ends up as it started.
* The command shown in the last scene is `CMD` in `src/theme.ts`; change it to
  `uvx leak-audit demo` once the package is on PyPI.
* The numbers in the stat scene (F1, 18/18, ₹29) come from `evals/results.json` and
  `evals/live_costs.json`; update `src/scenes/Evals.tsx` if they change.
* Fonts (Inter, JetBrains Mono) are fetched from Google Fonts at render time.
