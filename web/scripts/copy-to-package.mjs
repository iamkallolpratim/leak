// Copy the static export into the Python package (leak/_web) so `leak serve`
// and published wheels ship the dashboard with no Node at runtime.
import { cpSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";

const out = fileURLToPath(new URL("../out", import.meta.url));
const dest = fileURLToPath(new URL("../../leak/_web", import.meta.url));
rmSync(dest, { recursive: true, force: true });
cpSync(out, dest, { recursive: true });
console.log(`✓ dashboard copied to ${dest}`);
