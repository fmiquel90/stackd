// node render.mjs preview t1,t2,...  |  node render.mjs all <fps>
import { chromium } from "../../../front/node_modules/@playwright/test/index.mjs";
import fs from "node:fs";
const DIR = new URL(".", import.meta.url).pathname.replace(/\/$/, "");
const [, , mode, arg] = process.argv;
const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome", args: ["--allow-file-access-from-files"] });
const page = await (await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 })).newPage();
await page.goto("file://" + DIR + "/intro.html");
await page.evaluate(() => window.ready);
await page.waitForTimeout(500);
if (mode === "preview") {
  for (const t of arg.split(",").map(Number)) {
    await page.evaluate((t) => window.render(t), t);
    await page.screenshot({ path: `${DIR}/prev-${t}.png` });
  }
} else {
  const fps = Number(arg || 30), total = Math.round(30 * fps);
  fs.mkdirSync(`${DIR}/frames`, { recursive: true });
  for (let i = 0; i < total; i++) {
    await page.evaluate((t) => window.render(t), i / fps);
    await page.screenshot({ path: `${DIR}/frames/f${String(i).padStart(4, "0")}.png` });
    if (i % 150 === 0) console.log("frame", i);
  }
}
await browser.close();
