/* Batch save + multi-link save UI test (textarea, toggle, hints, kind pills) */
const puppeteer = require("puppeteer");

const URL = "http://127.0.0.1:8000/";
const PIN = (() => {
  const fs = require("fs");
  const path = require("path");
  // The admin PIN lives in .env (git-ignored). The fallback keeps the
  // suite bootable on a fresh clone.
  const line = fs.readFileSync(path.join(__dirname, "..", "..", ".env"), "utf8")
    .split("\n").find((l) => l.startsWith("ADMIN_PIN="));
  return line ? line.split("=")[1].trim() : "123456";
})();
const failures = [];
const check = (name, cond, extra) => {
  console.log(`${cond ? "PASS" : "FAIL"} ${name}${!cond && extra ? "  " + extra : ""}`);
  if (!cond) failures.push(name);
};

async function raf(page, n) {
  for (let i = 0; i < n; i++) {
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(r)));
  }
}

async function waitFor(fn, ms = 6000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

(async () => {
  const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 375, height: 900, isMobile: true });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.goto(URL, { waitUntil: "networkidle0" });

  // log in through the digit password so the app shell loads
  await page.type("#phoneInput", PIN);
  await page.click("#sendCodeBtn");
  await page.waitForFunction(
    () => !document.getElementById("appSection").classList.contains("hidden"),
    { timeout: 10000 });
  // the forced password change only appears on the very first admin login
  await page.waitForFunction(
    () => !document.getElementById("pinModal").classList.contains("hidden"),
    { timeout: 2000 }).catch(() => {});
  await page.evaluate(() => {
    closePinModal(true);
    state.me.must_change_pin = false;
    activateAppTab("save");   // admins land on the dashboard; the Save card is user-facing
  });
  await raf(page, 30);

  // ------------------------------------------------- link field is a textarea
  const tag = await page.evaluate(() => document.getElementById("linkInput").tagName);
  check("link field is a textarea", tag === "TEXTAREA", tag);

  // hints mention the env limits from /api/me
  const hints = await page.evaluate(() => ({
    batch: document.getElementById("batchHint").textContent,
    multiHidden: document.getElementById("multiHint").classList.contains("hidden"),
    boxHidden: document.getElementById("batchBox").classList.contains("hidden"),
  }));
  check("batch box starts hidden", hints.boxHidden && hints.multiHidden, JSON.stringify(hints));
  check("batch hint ready", /50/.test(hints.batch), hints.batch);

  // --------------------------------------------- capture /api/fetch requests
  const bodies = [];
  const aborts = [];
  await page.setRequestInterception(true);
  const handler = (req) => {
    if (req.url().includes("/api/jobs/") && req.url().includes("/abort")) {
      aborts.push(req.method() + " " + req.url().split("/api")[1]);
      req.respond({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: true, job: {
          id: "j1", link: "https://t.me/foo/1", kind: "batch", count: 10,
          status: "aborted", step: "Stopped", percent: 30, downloaded: 0, total: 0,
          speed: 0, wait_left: 0, error: null, message: null, text: null,
          items: [], saved_ids: [], open_url: null, created: Date.now() / 1000,
        } }),
      });
      return;
    }
    if (req.url().includes("/api/fetch")) {
      bodies.push(req.postData() || "");
      req.respond({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ job: {
          id: "fake1", link: "https://t.me/foo/1", kind: "single", count: 0,
          status: "done", step: "Done", percent: 100, downloaded: 0, total: 0,
          speed: 0, error: null, message: "Saved to your Saved Messages.",
          text: null, items: [], saved_ids: [1],
          open_url: "tg://openmessage?user_id=1&message_id=1",
          created: Date.now() / 1000,
        } }),
      });
      return;
    }
    req.continue();
  };
  page.on("request", handler);

  // ------------------------------------ Enter breaks the line, Ctrl+Enter sends
  await page.evaluate(() => {
    document.getElementById("linkInput").value = "https://t.me/foo/1";
    updateLinkHints();
  });
  await page.focus("#linkInput");
  await page.keyboard.press("Enter");
  await new Promise((r) => setTimeout(r, 250));
  const afterEnter = await page.evaluate(
    () => document.getElementById("linkInput").value.split("\n").length);
  check("Enter breaks the line instead of sending",
    bodies.length === 0 && afterEnter === 2, `${bodies.length} req, ${afterEnter} lines`);

  await page.keyboard.down("Control");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Control");
  check("Ctrl+Enter sends the save", await waitFor(() => bodies.length === 1),
    String(bodies.length));
  const single = JSON.parse(bodies[0] || "{}");
  check("single mode sent", single.mode === "single" && !single.count, JSON.stringify(single));

  const enterHints = await page.evaluate(() => {
    const real = window.matchMedia.bind(window);
    window.matchMedia = () => ({ matches: false });
    renderEnterHint();
    const desktop = document.getElementById("enterHint").textContent.trim();
    window.matchMedia = () => ({ matches: true });
    renderEnterHint();
    const mobile = document.getElementById("enterHint").textContent.trim();
    window.matchMedia = real;
    renderEnterHint();
    return {
      desktop,
      mobile,
      now: document.getElementById("enterHint").textContent.trim(),
      coarse: window.matchMedia("(pointer: coarse)").matches,
    };
  });
  check("desktop hint explains Ctrl+Enter",
    /Ctrl\+Enter/.test(enterHints.desktop), enterHints.desktop);
  check("touch hint leaves out Ctrl+Enter",
    enterHints.mobile.length > 10 && !/Ctrl\+Enter/.test(enterHints.mobile), enterHints.mobile);
  check("the shown hint matches the device",
    enterHints.coarse ? !/Ctrl\+Enter/.test(enterHints.now) : /Ctrl\+Enter/.test(enterHints.now),
    JSON.stringify(enterHints));

  // -------------------------------------------- multi-line paste shows a hint
  const multi = await page.evaluate(() => {
    document.getElementById("linkInput").value =
      "https://t.me/foo/1\nhttps://t.me/foo/2\nhttps://t.me/foo/3";
    autoGrow();
    updateLinkHints();
    const hint = document.getElementById("multiHint");
    return {
      hidden: hint.classList.contains("hidden"),
      text: hint.textContent,
      height: parseInt(document.getElementById("linkInput").style.height, 10),
    };
  });
  check("multi-line hint appears with the count",
    !multi.hidden && multi.text.includes("3"), JSON.stringify(multi));
  check("textarea grows for several lines", multi.height > 45, String(multi.height));

  await page.click("#fetchBtn");
  check("multi mode sent", await waitFor(() => bodies.length === 2), String(bodies.length));
  const multiBody = JSON.parse(bodies[1] || "{}");
  check("multi body carries every link",
    multiBody.mode === "multi" && (multiBody.link || "").split("\n").length === 3,
    JSON.stringify(multiBody));

  // ------------------------------------------- too many links blocks the send
  await page.evaluate(() => {
    document.getElementById("linkInput").value =
      Array.from({ length: 6 }, (_, i) => `https://t.me/foo/${i + 1}`).join("\n");
    updateLinkHints();
  });
  await page.click("#fetchBtn");
  await new Promise((r) => setTimeout(r, 250));
  const toast1 = await page.evaluate(() => document.getElementById("toast").textContent);
  check("six links are refused with a hint",
    bodies.length === 2 && toast1.includes("5"), `${bodies.length} req, toast=${toast1}`);

  // ------------------------------------------------ batch toggle + count box
  await page.evaluate(() => document.getElementById("batchToggle").click());
  const toggled = await page.evaluate(() => ({
    checked: document.getElementById("batchToggle").checked,
    box: !document.getElementById("batchBox").classList.contains("hidden"),
    hint: document.getElementById("batchHint").textContent,
    multiHidden: document.getElementById("multiHint").classList.contains("hidden"),
  }));
  check("batch toggle reveals the count box",
    toggled.checked && toggled.box && toggled.multiHidden, JSON.stringify(toggled));
  check("batch hint shows limit + remaining", /50/.test(toggled.hint), toggled.hint);

  // ---------------------------------- unlimited quota: pill and hint change too
  const unlimited = await page.evaluate(() => {
    window.__wasRemaining = state.me.remaining;
    state.me.unlimited = true;
    state.me.unlimited_days_left = 4;
    state.me.remaining = 0;
    renderQuota();
    updateLinkHints();
    return {
      pill: document.getElementById("quota").textContent,
      warn: document.getElementById("quota").classList.contains("warn"),
      hint: document.getElementById("batchHint").textContent,
      error: document.getElementById("batchHint").classList.contains("error"),
    };
  });
  check("unlimited pill replaces the counter",
    unlimited.pill.includes("Unlimited saves") && unlimited.pill.includes("4d left")
    && !unlimited.warn, JSON.stringify(unlimited));
  check("batch hint shows unlimited instead of a remaining count",
    unlimited.hint.includes("Unlimited saves") && !unlimited.error, unlimited.hint);

  const limited = await page.evaluate(() => {
    state.me.unlimited = false;
    state.me.unlimited_days_left = 0;
    state.me.remaining = window.__wasRemaining;
    renderQuota();
    updateLinkHints();
    return {
      pill: document.getElementById("quota").textContent,
      hint: document.getElementById("batchHint").textContent,
    };
  });
  check("the counter returns after a grant ends",
    !limited.pill.includes("Unlimited") && / of /.test(limited.pill),
    JSON.stringify(limited));

  // missing count refuses to send
  await page.click("#fetchBtn");
  await new Promise((r) => setTimeout(r, 250));
  const toast2 = await page.evaluate(() => document.getElementById("toast").textContent);
  check("empty count refuses to send",
    bodies.length === 2 && toast2.includes("how many"), `${bodies.length} req, toast=${toast2}`);

  // over the max refuses to send
  await page.evaluate(() => { document.getElementById("batchCount").value = "999"; });
  await page.click("#fetchBtn");
  await new Promise((r) => setTimeout(r, 250));
  const toast3 = await page.evaluate(() => document.getElementById("toast").textContent);
  check("count over the max refuses to send",
    bodies.length === 2 && toast3.includes("50"), `${bodies.length} req, toast=${toast3}`);

  // a valid count sends batch mode
  await page.evaluate(() => { document.getElementById("batchCount").value = "3"; });
  await page.click("#fetchBtn");
  check("valid batch sends batch mode", await waitFor(() => bodies.length === 3),
    String(bodies.length));
  const batchBody = JSON.parse(bodies[2] || "{}");
  check("batch body has mode + count",
    batchBody.mode === "batch" && batchBody.count === 3 && !!batchBody.link,
    JSON.stringify(batchBody));

  // ------------------------------------------------- stopping a running job
  const runningJob = {
    id: "j1", link: "https://t.me/foo/1", kind: "batch", count: 10,
    status: "running", step: "Finding files 3/10...", percent: 30,
    items: [], saved_ids: [], created: Date.now() / 1000,
  };
  await new Promise((r) => setTimeout(r, 400));   // let pending fetches settle
  const stopUi = await page.evaluate((j) => {
    state.selected = j.id;
    renderJob(j);
    const btn = document.getElementById("abortBtn");
    return { hidden: btn.classList.contains("hidden"), text: btn.textContent.trim() };
  }, runningJob);
  check("stop button appears while the job runs",
    !stopUi.hidden && stopUi.text === "Stop", JSON.stringify(stopUi));

  await page.evaluate(() => document.getElementById("abortBtn").click());
  check("stop button calls the abort endpoint", await waitFor(() => aborts.length === 1),
    JSON.stringify(aborts));
  check("abort request targets the running job",
    aborts[0] === "POST /jobs/j1/abort", String(aborts[0]));

  await new Promise((r) => setTimeout(r, 400));   // let the response render
  const afterAbort = await page.evaluate(() => ({
    badge: document.getElementById("jobStatus").textContent.trim(),
    step: document.getElementById("jobStep").textContent.trim(),
    hidden: document.getElementById("abortBtn").classList.contains("hidden"),
  }));
  check("aborted job shows a stopped badge",
    afterAbort.badge === "stopped" && afterAbort.step === "Stopped", JSON.stringify(afterAbort));
  check("stop button disappears once the job is stopped", afterAbort.hidden,
    JSON.stringify(afterAbort));

  const flood = await page.evaluate(() => {
    setLang("fr");
    const wait = translateServer("Rate limited by Telegram, waiting 42s");
    const resume = translateServer("Resuming...");
    setLang("en");
    return { wait, resume };
  });
  check("rate-limit countdown is translated",
    flood.wait === "Limité par Telegram, attente de 42 s"
    && flood.resume === "Reprise...", JSON.stringify(flood));

  // ------------------------------------------------- kind pills in the lists
  const pills = await page.evaluate(() => {
    state.jobs = [
      { id: "s1", link: "https://t.me/a/1", kind: "single", status: "done",
        created: Date.now() / 1000, items: [{ kind: "photo", name: "p", size: 1 }] },
      { id: "b1", link: "https://t.me/a/2", kind: "batch", status: "done",
        created: Date.now() / 1000 - 60, items: [] },
      { id: "m1", link: "https://t.me/a/3", kind: "multi", status: "done",
        created: Date.now() / 1000 - 120, items: [] },
    ];
    renderList();
    renderJob(state.jobs[1]);
    return {
      hist: [...document.querySelectorAll("#jobList .kind-pill")].map((e) => e.textContent),
      job: document.getElementById("jobKind").textContent,
      jobHidden: document.getElementById("jobKind").classList.contains("hidden"),
      singlePills: [...document.querySelectorAll("#jobList li")]
        .filter((li) => li.querySelector(".kind-pill")).length,
    };
  });
  check("history shows batch + multi pills",
    pills.hist.length === 2 && pills.hist[0] === "Batch" && pills.hist[1] === "Multi",
    JSON.stringify(pills));
  check("single history rows carry no pill", pills.singlePills === 2, String(pills.singlePills));
  check("job card shows the kind pill", !pills.jobHidden && pills.job === "Batch",
    JSON.stringify(pills));

  // French translation of the pill + toggle label
  await page.evaluate(() => {
    setLang("fr");
    renderJob({ id: "b1", link: "https://t.me/a/2", kind: "batch", status: "done",
      step: "Done", percent: 100, items: [], saved_ids: [], created: Date.now() / 1000 });
  });
  const fr = await page.evaluate(() => ({
    toggle: document.querySelector('label.switch-row span:last-child').textContent,
    pill: document.getElementById("jobKind").textContent,
  }));
  check("batch labels translate",
    fr.toggle === "Enregistrement groupé" && fr.pill === "Groupe", JSON.stringify(fr));
  await page.evaluate(() => { setLang("en"); });

  // --------------------------------------------------------------- geometry
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth);
  check("save panel fits 375px with the batch box", overflow <= 1, String(overflow));

  check("no JS page errors", errors.length === 0, JSON.stringify(errors.slice(0, 3)));
  console.log(failures.length ? `\n${failures.length} FAILURES` : "\nALL BATCH CHECKS PASSED");
  await browser.close();
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
