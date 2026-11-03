// Front invariants that a type checker cannot see (CLAUDE §4.8, §5; DESIGN §8): no browser storage,
// no hard-coded colour outside the token file. Run in CI and before each commit of the redesign.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../src", import.meta.url).pathname;
const TOKEN_FILE = "styles/tokens.css";
const CHECKS = [
  { name: "browser storage", re: /\b(localStorage|sessionStorage)\b/, files: /\.(tsx?|css)$/ },
  // A hex or rgb()/hsl() literal anywhere but the token file (tests may assert on token values).
  {
    name: "hard-coded colour",
    re: /#[0-9a-fA-F]{3,8}\b(?![\w-])|\brgba?\(|\bhsla?\(/,
    files: /\.(tsx?|css)$/,
    skip: (f) => f === TOKEN_FILE || /\.test\.tsx?$/.test(f),
  },
  // `base` is a colour token here (the page ground), so `text-base` paints text in the background
  // colour instead of setting 16 px: invisible text. Use an explicit size (`text-[16px]`).
  { name: "text-base is a colour here", re: /(^|[\s"'`:])text-base(?![\w/-])/, files: /\.tsx?$/ },
];

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

let failures = 0;
for (const file of walk(ROOT)) {
  const rel = relative(ROOT, file);
  const lines = readFileSync(file, "utf8").split("\n");
  for (const c of CHECKS) {
    if (!c.files.test(rel) || c.skip?.(rel)) continue;
    lines.forEach((line, i) => {
      if (/^\s*(\/\/|\*|\/\*)/.test(line)) return; // comments may name the rule
      if (c.re.test(line)) {
        failures++;
        console.error(`${rel}:${i + 1}: ${c.name}: ${line.trim()}`);
      }
    });
  }
}
if (failures) {
  console.error(`\n${failures} invariant violation(s).`);
  process.exit(1);
}
console.log("front invariants: ok");
