/* Layout regression test: long filenames must not break responsiveness. */
const puppeteer = require("puppeteer");

const URL = "http://127.0.0.1:8000/";
const LONG_NAME = "AQM_EApKkRJ8Lk7KqqPo4LTiXiw1kpIqpHTw291KBYxpvE19vw28ZWhMJbLLHOB.mp4";
const EXTREME = "y".repeat(300) + ".mp4";
const LONG_TEXT = "z".repeat(70); // backend caps text items at 70 chars

const VIEWPORTS = [
  { name: "320px (small phone)", width: 320, height: 568, isMobile: true },
  { name: "375px (iPhone)", width: 375, height: 667, isMobile: true },
  { name: "390px (iPhone 14)", width: 390, height: 844, isMobile: true },
  { name: "768px (tablet)", width: 768, height: 1024, isMobile: false },
  { name: "1280px (desktop)", width: 1280, height: 800, isMobile: false },
  { name: "1920px (wide)", width: 1920, height: 1080, isMobile: false },
];

async function measure(page) {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const out = {
      vw,
      pageScrollWidth: document.documentElement.scrollWidth,
      pageOverflow: document.documentElement.scrollWidth - vw,
      offenders: [],
      lis: [],
      clippedNames: 0,
      totalNames: 0,
    };

    document.querySelectorAll("body *").forEach((el) => {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return;
      const selfClips = ["hidden", "clip", "auto", "scroll"].includes(cs.overflowX);
      if (r.right > vw + 1 && !selfClips) {
        out.offenders.push({
          tag: el.tagName.toLowerCase(),
          cls: String(el.className).slice(0, 60),
          right: Math.round(r.right),
          text: (el.textContent || "").trim().slice(0, 40),
        });
      }
    });

    document.querySelectorAll(".item-name").forEach((el) => {
      out.totalNames++;
      if (el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).textOverflow === "ellipsis") {
        out.clippedNames++;
      }
    });

    const card = document.getElementById("jobCard");
    const cardRect = card && !card.classList.contains("hidden") ? card.getBoundingClientRect() : null;
    document.querySelectorAll(".items li").forEach((li) => {
      const r = li.getBoundingClientRect();
      out.lis.push({
        w: Math.round(r.width),
        fitsInCard: cardRect ? r.right <= cardRect.right + 1 : null,
      });
    });
    return out;
  });
}

(async () => {
  const browser = await puppeteer.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const page = await browser.newPage();
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 20000 });

  const fakeJob = {
    id: "test1",
    link: "https://t.me/sometechnicalchannel/12345",
    status: "done",
    step: "Done",
    percent: 100,
    downloaded: 0,
    total: 0,
    speed: 0,
    error: null,
    message: "Saved to your Saved Messages.",
    text: null,
    items: [
      { name: LONG_NAME, kind: "video", size: 734003200 },
      { name: EXTREME, kind: "doc", size: 12345 },
      { name: LONG_TEXT, kind: "text", size: 0 },
      { name: "photo_12345.jpg", kind: "photo", size: 1234 },
    ],
    saved_ids: [1, 2, 3, 4],
    open_url: "tg://openmessage?user_id=111&message_id=1",
    created: Math.floor(Date.now() / 1000),
  };

  const runningJob = {
    ...fakeJob,
    id: "test2",
    status: "running",
    percent: 0,
    step: "Downloading 1/2  ·  12.3 MB / 64.2 MB  ·  1.2 MB/s",
    message: null,
    items: [],
    open_url: null,
    error: null,
  };

  const errorJob = {
    ...fakeJob,
    id: "test3",
    status: "error",
    percent: 0,
    step: "Finding the message...",
    message: null,
    items: [],
    open_url: null,
    error:
      "Could not fetch the message. Please check the link and make sure you have access to it.",
  };

  let failures = 0;
  for (const vp of VIEWPORTS) {
    await page.setViewport(vp);
    for (const [label, job] of [
      ["done+long-names", fakeJob],
      ["running", runningJob],
      ["error", errorJob],
    ]) {
      await page.evaluate((j) => {
        document.getElementById("loginSection").classList.add("hidden");
        document.getElementById("appSection").classList.remove("hidden");
        document.getElementById("userArea").classList.remove("hidden");
        state.jobs = [j];
        state.selected = j.id;
        renderJob(j);
        renderList();
      }, job);

      const m = await measure(page);
      const offenders = m.offenders;
      const lisFit = m.lis.every((l) => l.fitsInCard !== false);
      const ok = m.pageOverflow <= 1 && offenders.length === 0 && lisFit;
      if (!ok) failures++;
      console.log(
        `${ok ? "PASS" : "FAIL"} ${vp.name} [${label}]: ` +
          `pageScroll=${m.pageScrollWidth}/${m.vw} ` +
          `offenders=${offenders.length}` +
          (offenders.length ? ` ${JSON.stringify(offenders.slice(0, 3))}` : "") +
          ` lisFit=${lisFit}` +
          (m.totalNames ? ` clippedNames=${m.clippedNames}/${m.totalNames}` : "")
      );
    }
  }

  // login view must also fit
  for (const vp of VIEWPORTS.filter((v) => v.isMobile)) {
    await page.setViewport(vp);
    await page.evaluate(() => {
      document.getElementById("appSection").classList.add("hidden");
      document.getElementById("loginSection").classList.remove("hidden");
      document.getElementById("userArea").classList.add("hidden");
    });
    const m = await measure(page);
    const ok = m.pageOverflow <= 1 && m.offenders.length === 0;
    if (!ok) failures++;
    console.log(
      `${ok ? "PASS" : "FAIL"} ${vp.name} [login]: pageScroll=${m.pageScrollWidth}/${m.vw} ` +
        `offenders=${m.offenders.length}` +
        (m.offenders.length ? ` ${JSON.stringify(m.offenders.slice(0, 3))}` : "")
    );
  }

  // --- dark mode: palette applied, still fits ---
  await page.setViewport({ name: "375px dark", width: 375, height: 667, isMobile: true });
  await page.evaluate((j) => {
    document.documentElement.setAttribute("data-theme", "dark");
    document.getElementById("loginSection").classList.add("hidden");
    document.getElementById("appSection").classList.remove("hidden");
    document.getElementById("userArea").classList.remove("hidden");
    state.jobs = [j];
    state.selected = j.id;
    renderJob(j);
    renderList();
    document.querySelector('[data-utab="history"]').click();
  }, fakeJob);
  await new Promise((r) => setTimeout(r, 550));  // let the colour cross-fade settle
  const darkM = await measure(page);
  const darkC = await page.evaluate(() => ({
    body: getComputedStyle(document.body).backgroundColor,
    card: getComputedStyle(document.querySelector("#historyPanel .card")).backgroundColor,
    fg: getComputedStyle(document.body).color,
    topbar: getComputedStyle(document.querySelector(".topbar")).backgroundColor,
  }));
  const darkOk = darkM.pageOverflow <= 1 && darkM.offenders.length === 0 &&
    darkC.body === "rgb(10, 10, 12)" && darkC.card === "rgb(20, 20, 23)" &&
    darkC.fg === "rgb(244, 244, 245)";
  if (!darkOk) failures++;
  console.log(
    `${darkOk ? "PASS" : "FAIL"} 375px [dark history]: pageScroll=${darkM.pageScrollWidth}/${darkM.vw} ` +
      `offenders=${darkM.offenders.length} body=${darkC.body} card=${darkC.card} fg=${darkC.fg}` +
      (darkM.offenders.length ? ` ${JSON.stringify(darkM.offenders.slice(0, 3))}` : "")
  );

  await browser.close();
  console.log(failures ? `\n${failures} FAILURES` : "\nALL LAYOUT CHECKS PASSED");
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error("ERROR", e);
  process.exit(2);
});
