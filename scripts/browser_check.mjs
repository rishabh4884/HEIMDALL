#!/usr/bin/env node
// Drive the running workbench in headless Chrome over the DevTools protocol (no dependencies; Node >= 22).
// Usage: node scripts/browser_check.mjs --base http://127.0.0.1:8777 [--out DIR] [--chrome PATH]
// Exercises the real UI against the real API: records, history, selection, strip, ribbon, twin views, jobs,
// links, keyboard, narrow layout; fails (exit 1) on any assertion, uncaught exception or console error.
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://127.0.0.1:8777");
const OUT = arg("--out", mkdtempSync(join(tmpdir(), "heimdall-browser-")));
const CHROME = arg("--chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");
mkdirSync(OUT, { recursive: true });
const port = 9300 + Math.floor(Math.random() * 400);
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), "hcdp-"))}`,
  "--no-first-run", "--no-default-browser-check", "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [], problems = [];
let ws, seq = 0;
const pending = new Map();

async function connect() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) { ws = new WebSocket(page.webSocketDebuggerUrl); await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; }); break; }
    } catch { /* not up yet */ }
    await sleep(200);
  }
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) { const { resolve, reject } = pending.get(msg.id); pending.delete(msg.id); msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result); }
    else if (msg.method === "Runtime.exceptionThrown") problems.push(`exception: ${msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text}`);
    else if (msg.method === "Runtime.consoleAPICalled" && ["error", "assert"].includes(msg.params.type)) problems.push(`console.${msg.params.type}: ${msg.params.args.map((a) => a.value ?? a.description).join(" ")}`);
    else if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error" && !/\/sky\/spherex\/|favicon\.ico/.test(msg.params.entry.url || "")
      && !(/status of 404/.test(msg.params.entry.text) && /\/api\/v1\//.test(msg.params.entry.url || ""))) problems.push(`log: ${msg.params.entry.text} ${msg.params.entry.url || ""}`);
  };
}
const send = (method, params = {}) => new Promise((resolve, reject) => { const id = ++seq; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); });
async function evaluate(expr) {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function waitFor(expr, timeout = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) { try { if (await evaluate(`return Boolean(${expr})`)) return true; } catch { /* page navigating */ } await sleep(150); }
  throw new Error(`timeout waiting for ${expr}`);
}
async function shot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, "base64"));
}
async function check(name, fn) {
  const t0 = Date.now();
  try { const detail = await fn(); results.push({ name, ok: true, ms: Date.now() - t0, detail }); console.log(`ok   ${name}${detail ? ` · ${detail}` : ""}`); }
  catch (e) { results.push({ name, ok: false, error: e.message }); console.log(`FAIL ${name}: ${e.message}`); await shot(`fail-${name.replace(/\W+/g, "-")}`).catch(() => {}); }
}
const go = async (path) => {
  await evaluate(`document.body && delete document.body.dataset.booted; return true`).catch(() => {});
  await send("Page.navigate", { url: BASE + path });
  await waitFor(`document.readyState === "complete" && document.body?.dataset.booted`);
};
const click = (sel) => evaluate(`const el = document.querySelector(${JSON.stringify(sel)}); if (!el) throw new Error("missing ${sel.replaceAll('"', "'")}"); el.click(); return true;`);
const text = (sel) => evaluate(`return document.querySelector(${JSON.stringify(sel)})?.textContent || ""`);
const assert = (c, m) => { if (!c) throw new Error(m); };

try {
  await connect();
  await send("Runtime.enable"); await send("Log.enable"); await send("Page.enable"); await send("Network.enable");
  const posts = [];
  ws.addEventListener("message", (m) => { const msg = JSON.parse(m.data); if (msg.method === "Network.requestWillBeSent" && msg.params.request.method === "POST") posts.push(msg.params.request.url); });

  await check("fresh start shows all sky, library and no analysis POST", async () => {
    await go("/");
    await waitFor(`document.querySelectorAll(".library-item").length >= 5`);
    assert((await text("#field-size")).includes("Full sky"), "not all-sky");
    assert(posts.length === 0, `unexpected POST ${posts}`);
    await shot("01-fresh");
    return `${await evaluate(`return document.querySelectorAll(".library-item").length`)} targets`;
  });
  await check("saved target NASA-02: gated quick answer, headline claim, status separation", async () => {
    await go("/?target=NASA-02");
    await waitFor(`document.querySelector("#qa-title") && !document.querySelector("#record").textContent.includes("Loading the gated")`);
    const qa = await text(".quick-answer");
    const chips = await text(".status-grid");
    assert(/Execution/.test(chips) && /Scientific test status/.test(chips) && /Claim/.test(chips), "status vocabularies not separated");
    assert(posts.length === 0, "loading a saved target must not POST");
    await shot("02-nasa02");
    return qa.slice(0, 110).replace(/\s+/g, " ");
  });
  await check("map ↔ chart ↔ inspector share one observation identity", async () => {
    await click('[data-panel="charts"]');
    await waitFor(`document.querySelector("#chart .chart-point.observed")`);
    const id = await evaluate(`const g = document.querySelector("#chart .chart-point.observed"); g.dispatchEvent(new MouseEvent("click", {bubbles: true})); return g.dataset.id;`);
    await waitFor(`location.search.includes("sel=observation")`);
    const sel = new URLSearchParams(await evaluate(`return location.search`)).get("sel");
    assert(sel === `observation:${id}`, `URL selection ${sel} != ${id}`);
    const insp = await text("#record");
    assert(insp.includes(id), "inspector does not show the selected observation");
    return id;
  });
  await check("back/forward restores the previous selection context", async () => {
    const before = await evaluate(`return location.search`);
    await evaluate(`document.querySelector('[data-tab="history"]').click()`);
    await waitFor(`location.search.includes("tab=history")`);
    await evaluate(`history.back()`);
    await waitFor(`!location.search.includes("tab=history")`);
    assert((await evaluate(`return location.search`)) === before, "history did not restore");
    return "ok";
  });
  await check("wavelength strip: 102 planes, blank dichroic 51/52, keyboard stepping, measurement marks", async () => {
    await waitFor(`document.querySelectorAll("#band-strip .plane").length === 102`);
    const blank = await evaluate(`return [...document.querySelectorAll('#band-strip .plane[data-blank="true"]')].map(e => e.dataset.plane).join(",")`);
    assert(blank === "51,52", `blank planes ${blank}`);
    await evaluate(`const s = document.querySelector("#band-strip"); s.focus(); s.dispatchEvent(new KeyboardEvent("keydown", {key: "End", bubbles: true}));`);
    assert((await text("#wavelength-label")).includes("102/102"), "End key did not select plane 102");
    await evaluate(`document.querySelector('#band-strip .plane[data-plane="51"]').click()`);
    assert((await text("#wavelength-label")).includes("blank"), "blank plane not labelled");
    const marks = await evaluate(`return document.querySelectorAll("#band-strip .mark").length`);
    assert(marks > 0, "no measurement marks for NASA-02");
    return `${marks} measurement marks`;
  });
  await check("Separate / Merge is reversible and labelled illustrative", async () => {
    await click("#separate");
    await waitFor(`document.querySelector("#band-strip").dataset.separated === "true"`);
    assert((await text("#separate-note")).includes("Illustrative"), "separate note missing");
    await shot("03-separated");
    await click("#separate");
    await waitFor(`document.querySelector("#band-strip").dataset.separated === "false"`);
    assert((await text("#separate-note")).includes("does not coadd"), "merge caveat missing");
    return "ok";
  });
  await check("time ribbon: real epochs, elapsed vs equal spacing, keyboard and playback", async () => {
    await waitFor(`!document.querySelector("#ribbon").hidden`);
    const n = await evaluate(`return document.querySelectorAll("#ribbon-track .epoch").length`);
    const el = await evaluate(`return [...document.querySelectorAll("#ribbon-track .epoch")].map(e => e.style.left).join("|")`);
    await evaluate(`const s = document.querySelector("#spacing"); s.value = "equal"; s.dispatchEvent(new Event("change"));`);
    const eq = await evaluate(`return [...document.querySelectorAll("#ribbon-track .epoch")].map(e => e.style.left).join("|")`);
    assert(n < 3 || el !== eq, "elapsed and equal spacing are indistinguishable");
    await evaluate(`const t = document.querySelector("#ribbon-track"); t.focus(); t.dispatchEvent(new KeyboardEvent("keydown", {key: "Home", bubbles: true}));`);
    await waitFor(`location.search.includes("frame=")`);
    assert((await text("#frame-label")).includes("MJD"), "frame label lacks MJD");
    await click("#play");
    await sleep(1300);
    const playing = await evaluate(`return document.querySelector("#play").getAttribute("aria-label")`);
    await click("#play");
    assert(playing.includes("Pause"), "playback did not start");
    await click("#show-all");
    return `${n} epochs`;
  });
  await check("box selection lists plotted records deterministically", async () => {
    await click("#focus-field");
    await sleep(500);
    await click('[data-mode="box"]');
    const r = await evaluate(`const c = document.querySelector("#sky").getBoundingClientRect();
      const ev = (t, x, y) => document.querySelector("#sky").dispatchEvent(new PointerEvent(t, {clientX: c.left + x, clientY: c.top + y, bubbles: true, pointerId: 1, button: 0, isPrimary: true}));
      ev("pointerdown", 20, 20); for (let i = 1; i <= 10; i++) ev("pointermove", 20 + i * (c.width - 40) / 10, 20 + i * (c.height - 60) / 10); ev("pointerup", c.width - 20, c.height - 40); return true;`);
    await waitFor(`document.querySelector(".region-block")`);
    const t = await text("#region-title");
    await evaluate(`document.querySelector("#inspector").scrollIntoView(); return true`);
    await shot("04-region");
    assert(/observation/.test(t), `region did not include observations: ${t}`);
    return t;
  });
  await check("filters: counts, hidden selection explained, clear", async () => {
    await evaluate(`document.querySelector('[data-panel="layers"]').click(); const s = document.querySelector("#detector"); s.value = s.options[1]?.value || ""; s.dispatchEvent(new Event("change"));`);
    await waitFor(`!document.querySelector("#filter-count").hidden`);
    const summary = await text("#filter-summary");
    assert(/of \d+ observations shown/.test(summary), summary);
    await click('[data-action="clear-filters"]');
    await waitFor(`document.querySelector("#filter-count").hidden`);
    return summary;
  });
  await check("twin views align real cached products; difference gated by comparability", async () => {
    await click('[data-panel="twin"]');
    await waitFor(`document.querySelector(".twin-status") || document.querySelector("#twin .empty.error")`, 20000);
    const status = await text(".twin-status");
    const diffDisabled = await evaluate(`return document.querySelector('[data-twin="difference"]').disabled`);
    assert(/COMPARABLE/.test(status), "no comparability shown");
    if (!diffDisabled) { await click('[data-twin="difference"]'); await waitFor(`document.querySelector('.twin-views[data-mode="difference"] canvas')`); }
    await click('[data-twin="blink"]');
    await waitFor(`document.querySelector('[data-twin="blink-step"]')`);
    // the ribbon drives image B while the comparison panel is open
    const bBefore = await evaluate(`return document.querySelector('[name="twin-b"]').value`);
    await evaluate(`const t = document.querySelector("#ribbon-track"); t.focus(); t.dispatchEvent(new KeyboardEvent("keydown", {key: "End", bubbles: true})); return true`);
    await sleep(300);
    await waitFor(`document.querySelector(".twin-status") && !document.querySelector("#twin").textContent.includes("Aligning")`, 20000);
    const followed = (await evaluate(`return document.querySelector('[name="twin-b"]').value`)) !== bBefore;
    await click('[data-twin="side"]');
    await evaluate(`document.querySelector("#twin-panel").scrollIntoView(); return true`);
    await shot("05-twin");
    await click('[data-twin="blink"]');
    await waitFor(`document.querySelector('[data-twin="blink-step"]')`);
    await click('[data-twin="blink-step"]');
    // choose a cross-detector pair: must be refused
    const refused = await evaluate(`const a = document.querySelector('[name="twin-a"]'), b = document.querySelector('[name="twin-b"]');
      const det = (o) => o.textContent.split("·")[1].trim(); const first = a.options[0]; const other = [...b.options].find(o => det(o) !== det(first));
      if (!other) return "single-detector record"; a.value = first.value; a.dispatchEvent(new Event("change", {bubbles: true}));
      const b2 = document.querySelector('[name="twin-b"]'); b2.value = other.value; b2.dispatchEvent(new Event("change", {bubbles: true})); return "pending";`);
    if (refused === "pending") {
      await waitFor(`document.querySelector(".twin-status")?.textContent.includes("NOT_COMPARABLE")`, 20000);
      assert(await evaluate(`return document.querySelector('[data-twin="difference"]').disabled`), "difference not disabled for a non-comparable pair");
    }
    return `${status.split("\n")[0].trim().slice(0, 60)}; B followed ribbon: ${followed}; cross-detector: ${refused === "pending" ? "refused" : refused}`;
  });
  await check("evidence traversal forward (claim → observation → product) and reverse lookup", async () => {
    await go("/?target=NASA-02&tab=evidence");
    await waitFor(`document.querySelector('#record [data-action="claim"]')`);
    await click('#record [data-action="claim"]');
    await waitFor(`document.querySelector('#record [data-action="observation"]')`);
    await click('#record [data-action="observation"]');
    await waitFor(`document.querySelector('#record [data-action="product"]')`);
    await click('#record [data-action="product"]');
    await waitFor(`document.querySelector('#record [data-action="lookup"]')`);
    await click('#record [data-action="lookup"]');
    await waitFor(`document.querySelector("#record").textContent.includes("Dependent results") && document.querySelector('#record [data-action="version"]')`);
    return `${await evaluate(`return document.querySelectorAll('#record [data-action="version"]').length`)} dependent versions`;
  });
  await check("history: versions labelled and comparison rendered (refusal respected)", async () => {
    await go("/?target=NASA-02&tab=history");
    await waitFor(`document.querySelector("#compare-version")`);
    const labels = await text("#record");
    assert(labels.includes("Latest usable") && labels.includes("Viewing"), "version labels missing");
    await click('[data-action="compare"]');
    await waitFor(`document.querySelector("#record").textContent.includes("Comparison") && !document.querySelector("#record").textContent.includes("Comparing")`);
    const t = await text("#record");
    assert(/COMPARABLE|NOT_COMPARABLE/.test(t), "comparability not shown");
    await go(`/?cmp=tinv:NASA-01:fd9680e5..tinv:NASA-02:0c714738&target=NASA-02`);
    await waitFor(`document.querySelector("#record").textContent.includes("NOT_COMPARABLE")`);
    assert((await text("#record")).includes("No changes are computed"), "refused comparison shows changes");
    return "comparable + refused pair";
  });
  await check("limited result NASA-01 shown as a complete outcome", async () => {
    await go("/?target=NASA-01");
    await waitFor(`document.querySelector(".status-grid")`);
    const t = await text("#record");
    assert(/Limited by data/i.test(t), "LIMITED_BY_DATA not presented");
    return "ok";
  });
  await check("all five NASA targets open through the common workflow", async () => {
    const out = [];
    for (const id of ["NASA-01", "NASA-02", "NASA-03", "NASA-04", "NASA-05"]) {
      await go(`/?target=${id}`);
      await waitFor(`document.querySelector(".status-grid") || document.querySelector("#record .empty.error")`);
      out.push(`${id}:${(await text(".status-grid .chip.warn")).trim() || "?"}`);
    }
    assert(posts.length === 0, "a target view issued a POST");
    return out.join(" ");
  });
  await check("saved field run: candidates, footprints, synthetic watermark when synthetic", async () => {
    const runs = await (await fetch(`${BASE}/api/v1/runs`)).json();
    const real = runs.runs.find((r) => r.data_origin === "REAL");
    const syn = runs.runs.find((r) => r.data_origin === "SYNTHETIC");
    await go(`/?run=${real.run_id}`);
    await waitFor(`document.querySelector(".quick-answer")?.textContent.includes("candidate")`);
    if (syn) {
      await go(`/?run=${syn.run_id}`);
      await waitFor(`document.querySelector(".synthetic-flag")`);
      assert((await text("#map-banner")).includes("SYNTHETIC"), "map banner lacks synthetic marking");
    }
    return `${real.run_id}${syn ? ` + synthetic ${syn.run_id}` : ""}`;
  });
  await check("coordinate coverage browsing never POSTs an analysis", async () => {
    const before = posts.length;
    await go("/?field=150.1,2.2,0.05");
    await waitFor(`document.querySelector(".quick-answer")?.textContent.includes("Coverage only")`);
    assert(posts.length === before, "coverage browsing POSTed");
    return await text(".quick-answer h3");
  });
  await check("invalid link explains the missing record and keeps the context", async () => {
    await go("/?version=tinv:NASA-02:deadbeef&plane=40&ra=331&dec=-56.8&span=0.2");
    await waitFor(`document.querySelector("#record .empty.error")`);
    const t = await text("#record");
    assert(/not on this server/.test(t), t);
    assert((await text("#wavelength-label")).includes("Plane 40"), "plane not kept");
    await go("/?target=NASA-9&token=abc");
    await waitFor(`!document.querySelector("#notice").hidden`);
    assert((await text("#notice")).includes("ignored"), "bad link not explained");
    return "ok";
  });
  await check("search: target name, identifier and coordinates", async () => {
    await go("/");
    await evaluate(`const i = document.querySelector("#omni-input"); i.value = "barnard"; i.dispatchEvent(new Event("input"));`);
    await waitFor(`document.querySelector("#omni-list li")`);
    await evaluate(`document.querySelector("#omnibox").requestSubmit()`);
    await waitFor(`location.search.includes("target=NASA-01")`);
    await evaluate(`const i = document.querySelector("#omni-input"); i.value = "17:57:48.5 +04:41:36"; i.dispatchEvent(new Event("input")); document.querySelector("#omnibox").requestSubmit();`);
    await waitFor(`location.search.includes("field=269.4")`);
    return "name + sexagesimal";
  });
  await check("jobs dialog reconnects to server jobs without resubmitting", async () => {
    const before = posts.length;
    await click("#show-jobs");
    await waitFor(`document.querySelector("#server-jobs table") || document.querySelector("#server-jobs .empty")`);
    await evaluate(`document.querySelector("#close-dialog").click()`);
    assert(posts.length === before, "jobs dialog POSTed");
    return "ok";
  });
  await check("data status renders capabilities (per-detector validation)", async () => {
    await click("#show-system");
    await waitFor(`document.querySelector("#dialog-content").textContent.includes("NOT_VALIDATED")`);
    await evaluate(`document.querySelector("#close-dialog").click()`);
    return "ok";
  });
  await check("worker/offline banner and run options follow /health and /capabilities", async () => {
    const health = await (await fetch(`${BASE}/api/v1/health`)).json();
    await go("/?target=NASA-04");
    const banner = await text("#worker-banner");
    if (health.offline) assert(banner.includes("Offline mode"), `offline banner missing: ${banner}`);
    else if (!health.worker?.running) assert(banner.includes("No data worker"), "absent-worker banner missing");
    else assert(!banner, "banner shown while a worker runs");
    await click("#open-field");
    await waitFor(`document.querySelector('#region-form input[value="field"]')`);
    const disabled = await evaluate(`return [...document.querySelectorAll('#region-form input[name="intent"]')].filter(i => i.disabled).length`);
    if (health.offline) assert((await text("#dialog-content")).includes("Offline mode"), "offline note missing in run dialog");
    await evaluate(`document.querySelector("#close-dialog").click()`);
    await evaluate(`document.querySelector('[data-tab="deeper"]').click()`);
    await waitFor(`document.querySelector("#record").textContent.includes("Not available in this backend")`);
    assert((await text("#record")).includes("Period search"), "not_available capabilities not listed");
    return `${health.offline ? "offline" : "online"}, worker ${health.worker?.running ? "running" : "absent"}, ${disabled} disabled options`;
  });
  await check("guided tour is skippable and opens real records", async () => {
    await click("#show-guide");
    await waitFor(`document.querySelector("[data-tour-go]")`);
    await click('[data-tour="1"]');
    await click("[data-tour-go]");
    await waitFor(`location.search.includes("target=NASA-02")`);
    await click("#show-guide");
    await click('[data-tour="end"]');
    assert(!(await evaluate(`return document.querySelector("#dialog").open`)), "skip did not close");
    return "ok";
  });
  await check("narrow (390 px) layout: no horizontal page scroll, caveats visible", async () => {
    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
    await go("/?target=NASA-01");
    await waitFor(`document.querySelector(".status-grid")`);
    const overflow = await evaluate(`return document.documentElement.scrollWidth - document.documentElement.clientWidth`);
    await shot("06-mobile");
    await send("Emulation.clearDeviceMetricsOverride");
    assert(overflow <= 1, `horizontal overflow ${overflow}px`);
    return "ok";
  });
  await check("pan responsiveness (frame times while dragging)", async () => {
    await go("/?target=NASA-02");
    await waitFor(`document.querySelector(".status-grid")`);
    const stats = await evaluate(`const c = document.querySelector("#sky"), r = c.getBoundingClientRect(); const times = []; let last = performance.now();
      let raf = true; const loop = (t) => { times.push(t - last); last = t; if (raf) requestAnimationFrame(loop); }; requestAnimationFrame(loop);
      const ev = (t, x) => c.dispatchEvent(new PointerEvent(t, {clientX: r.left + x, clientY: r.top + r.height / 2, bubbles: true, pointerId: 3, button: 0}));
      ev("pointerdown", 300);
      for (let i = 0; i < 60; i++) { await new Promise(res => setTimeout(res, 16)); ev("pointermove", 300 + i * 4); }
      ev("pointerup", 540); raf = false; times.sort((a, b) => a - b);
      return { n: times.length, p50: times[Math.floor(times.length / 2)], p95: times[Math.floor(times.length * .95)], max: times[times.length - 1] };`);
    return `frames ${stats.n}, p50 ${stats.p50.toFixed(1)} ms, p95 ${stats.p95.toFixed(1)} ms, max ${stats.max.toFixed(1)} ms`;
  });
} catch (e) {
  results.push({ name: "harness", ok: false, error: e.message });
  console.log("FAIL harness:", e.message);
} finally {
  const failed = results.filter((r) => !r.ok);
  const errs = [...new Set(problems)];
  writeFileSync(join(OUT, "results.json"), JSON.stringify({ base: BASE, results, problems: errs }, null, 1));
  console.log(`\n${results.length - failed.length}/${results.length} checks passed; ${errs.length} console/exception problem(s)${errs.length ? `:\n  ${errs.join("\n  ")}` : ""}\nartifacts: ${OUT}`);
  chrome.kill();
  process.exit(failed.length || errs.length ? 1 : 0);
}
