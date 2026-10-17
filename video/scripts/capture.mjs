// Capture dashboard screenshots into docs/assets (also the Remotion public dir).
//
//   leak serve --demo --no-open      # in another terminal
//   npm run shots                    # [-- --url http://127.0.0.1:8765 --run run_xxx]
//
// Refuses to run unless the server reports `demo: true`: these images are
// committed to the repo, so they must never show real financial data.
import { existsSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const here = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(here, "../../docs/assets");
const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const BASE = (args.url ?? "http://127.0.0.1:8765").replace(/\/$/, "");

// Remotion's bundled Chrome Headless Shell (`npx remotion browser ensure`).
function findChrome() {
  const root = resolve(here, "../node_modules/.remotion/chrome-headless-shell");
  const walk = (d) =>
    readdirSync(d, { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? walk(join(d, e.name)) : e.name === "chrome-headless-shell" ? [join(d, e.name)] : [],
    );
  const found = existsSync(root) ? walk(root) : [];
  if (!found.length) throw new Error("Chrome not found. Run: npx remotion browser ensure");
  return found[0];
}

const getJson = async (path) => (await fetch(BASE + path)).json();

const session = await getJson("/api/session");
if (session.demo !== true) {
  console.error(`✗ ${BASE} is serving REAL data (demo=false). Start \`leak serve --demo\` and retry.`);
  process.exit(1);
}

// Traces are shared between demo and real runs, so pick a run that is clearly the
// demo ledger (155 entries) instead of "the latest run".
let runId = args.run;
if (!runId) {
  const runs = await getJson("/api/runs");
  runId = runs.find((r) => r.status === "completed" && r.prompt.includes("'entries': 155"))?.run_id;
}
if (!runId) console.warn("! no demo agent run found; skipping run/LLM screenshots");

const browser = await puppeteer.launch({ executablePath: findChrome(), headless: true });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 });
await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);

const settle = async (selector) => {
  if (selector) await page.waitForSelector(selector, { timeout: 15000 });
  await new Promise((r) => setTimeout(r, 900)); // let data + fonts render
};
const hideScrollbars = () =>
  page.addStyleTag({ content: "::-webkit-scrollbar{display:none}html{scrollbar-width:none}" });
const shot = async (name, opts = {}) => {
  await page.screenshot({ path: join(OUT, name), ...opts });
  console.log("✓", name);
};
const go = async (path, selector) => {
  await page.goto(BASE + path, { waitUntil: "networkidle0" });
  await hideScrollbars();
  await settle(selector);
};

// 1. Dashboard: viewport (README hero) + full page (video scroll).
await go("/", "main section");
await page.waitForFunction(() => document.body.innerText.includes("₹24,635"), { timeout: 15000 });
await shot("dashboard.png");
await shot("dashboard-full.png", { fullPage: true });

// 2. Evidence drawer on the first finding.
const evidenceButton = await page.waitForSelector("xpath/.//button[starts-with(normalize-space(.), 'Evidence (')]");
await evidenceButton.click();
await page.waitForSelector("[role=dialog] pre");
await settle();
await shot("evidence.png");
await page.keyboard.press("Escape");

// 3. Approvals queue. Queue two drafts through the UI so there are pending cards to
//    show, screenshot, then reject exactly those two so the demo DB ends up as it began.
const pendingIds = async () =>
  new Set((await getJson("/api/actions")).filter((a) => a.status === "pending").map((a) => a.id));
const before = await pendingIds();
await go("/", "main section");
for (let i = 0; i < 2; i++) {
  const btn = await page.$("xpath/.//button[normalize-space(.)='Draft email']");
  if (!btn) break;
  await btn.click();
  await new Promise((r) => setTimeout(r, 700));
}
const queued = [...(await pendingIds())].filter((id) => !before.has(id));
await go("/approvals/", "main h1");
await page.waitForSelector("xpath/.//button[contains(., 'Approve')]");
await shot("approvals.png");
for (const id of queued) {
  await fetch(`${BASE}/api/actions/${id}/reject`, { method: "POST", headers: { "X-Leak-Token": session.token } });
}
console.log(`  (queued and rejected ${queued.length} demo drafts for the approvals shot)`);

// 4. Agent run + exactly what the LLM saw (explicit demo run id, never the list page).
if (runId) {
  await go(`/runs/?id=${runId}`, "main ol");
  await shot("run.png");
  await go(`/llm/?id=${runId}`, "main ol li");
  await shot("llm.png");
}

await browser.close();
console.log(`\nSaved to ${OUT}`);
