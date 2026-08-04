/**
 * Proves the direction rules in `eslint.config.js` actually report.
 *
 * No source twin on purpose: what this guards is the lint config, and
 * specifically that a boundary rule can be present, correctly configured, and
 * still completely inert. `import-x/extensions` ships as `['.js']` — until it
 * names `.ts`/`.tsx` the plugin resolves an import but never opens the target to
 * read ITS imports, so `no-cycle` walks a graph one edge deep and reports
 * nothing, on any codebase, forever. A green lint run is indistinguishable from
 * a working one; only a known-bad fixture tells the two apart.
 *
 * Fixtures live under `src/utils/` and `src/api/` because the rules are scoped
 * by path, and are removed again afterwards so `pnpm lint` never sees them.
 */

import { ESLint } from "eslint";
import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const UTILS_DIR = path.join(process.cwd(), "src", "utils", "__eslint_fixtures__");
const API_DIR = path.join(process.cwd(), "src", "api", "__eslint_fixtures__");

const FIXTURES: [dir: string, name: string, source: string][] = [
  [
    UTILS_DIR,
    "reachesUp.ts",
    'import { showCoreChangeModal } from "../../components/CoreChangeModal";\nexport const probe = showCoreChangeModal;\n',
  ],
  [
    API_DIR,
    "reachesUp.ts",
    'import { showCoreChangeModal } from "../../components/CoreChangeModal";\nexport const probe = showCoreChangeModal;\n',
  ],
  [UTILS_DIR, "cycleA.ts", 'import { bee } from "./cycleB";\nexport const ay = (): number => bee() + 1;\n'],
  [
    UTILS_DIR,
    "cycleB.ts",
    'import { ay } from "./cycleA";\nexport const bee = (): number => (Math.random() > 2 ? ay() : 0);\n',
  ],
];

/** Rule IDs reported for `file`, using the repository's real ESLint config. */
async function rulesReportedFor(file: string): Promise<string[]> {
  const results = await new ESLint({ cwd: process.cwd() }).lintFiles([file]);
  return results.flatMap((r) => r.messages.map((m) => m.ruleId ?? "<fatal>"));
}

describe("frontend direction rules", () => {
  beforeAll(async () => {
    await mkdir(UTILS_DIR, { recursive: true });
    await mkdir(API_DIR, { recursive: true });
    await Promise.all(FIXTURES.map(([dir, name, source]) => writeFile(path.join(dir, name), source, "utf8")));
  });

  afterAll(async () => {
    await rm(UTILS_DIR, { recursive: true, force: true });
    await rm(API_DIR, { recursive: true, force: true });
  });

  it("reports utils/ reaching up into components/", async () => {
    expect(await rulesReportedFor(path.join(UTILS_DIR, "reachesUp.ts"))).toContain("import-x/no-restricted-paths");
  });

  it("reports api/ reaching into components/", async () => {
    expect(await rulesReportedFor(path.join(API_DIR, "reachesUp.ts"))).toContain("import-x/no-restricted-paths");
  });

  it("reports a dependency cycle between two .ts modules", async () => {
    expect(await rulesReportedFor(path.join(UTILS_DIR, "cycleA.ts"))).toContain("import-x/no-cycle");
  });

  it("leaves a compliant module alone", async () => {
    expect(await rulesReportedFor(path.join(process.cwd(), "src", "utils", "detach.ts"))).toEqual([]);
  });
}, 60_000);
