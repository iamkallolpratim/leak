Launch assets, all generated from the **synthetic demo dataset** (never real data):

| File | What | Made by |
|---|---|---|
| `dashboard.png` | Findings page, the README hero | `npm run shots` |
| `dashboard-full.png` | Full-page capture (scrolled in the video) | `npm run shots` |
| `evidence.png` | Evidence drawer on the cult.fit finding | `npm run shots` |
| `approvals.png` | Approval queue with draft previews | `npm run shots` |
| `run.png`, `llm.png` | Agent run trace; exactly what the LLM saw | `npm run shots` |
| `leak-demo.mp4` | 49 s walkthrough, 1920×1080 | `npm run render` |
| `demo.gif` | 16 s teaser for the README, 960×540 | `npm run gif` |

Regenerate: see [`video/README.md`](../../video/README.md). This folder is also the Remotion public
directory, so the video reads the screenshots from here.
