/* Login-page notice banner + new-login security prompt */
const puppeteer = require("puppeteer");

const URL = "http://127.0.0.1:8000/";
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

(async () => {
  const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 375, height: 900, isMobile: true });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  // the synthetic login has no real token, so API calls answer 401: that is
  // expected noise here, not a page error
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource|401|403/.test(m.text())) {
      errors.push(m.text());
    }
  });

  // serve a logged-out /api/me that carries the public login notice
  await page.setRequestInterception(true);
  const meHandler = (req) => {
    if (req.url().includes("/api/me")) {
      req.respond({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          logged_in: false,
          notice_login: "The site opens at 8:00. Existing users can still save.",
          notice_login_color: "green",
        }),
      });
      return;
    }
    req.continue();
  };
  page.on("request", meHandler);

  await page.goto(URL, { waitUntil: "networkidle0" });
  await raf(page, 30);

  const banner = await page.evaluate(() => {
    const el = document.getElementById("noticeBanner");
    return { cls: el.className, text: el.textContent.trim() };
  });
  check("login page shows the login notice",
    !banner.cls.includes("hidden") && banner.cls.includes("alert-green"), banner.cls);
  check("login notice text rendered",
    banner.text === "The site opens at 8:00. Existing users can still save.", banner.text);

  // a blue login notice is painted the same way
  const blue = await page.evaluate(() => {
    state.me.notice_login_color = "blue";
    state.me.notice_login = "Maintenance at 8:00.";
    renderLoginNotice();
    const el = document.getElementById("noticeBanner");
    return { cls: el.className, text: el.textContent };
  });
  check("login notice repaints when the colour changes",
    blue.cls.includes("alert-blue") && blue.text === "Maintenance at 8:00.", blue.cls);

  // ---------------------------------------- security prompt after a login
  await page.evaluate(() => onLoggedIn({ uid: 7, name: "Tester" }));
  await page.waitForFunction(
    () => !document.getElementById("secModal").classList.contains("hidden"),
    { timeout: 10000 });
  await raf(page, 25);
  const sec = await page.evaluate(() => {
    const m = document.getElementById("secModal");
    return {
      open: !m.classList.contains("hidden"),
      title: document.querySelector("#secModal .modal-title").textContent.trim(),
      desc: document.querySelector("#secModal .modal-desc").textContent.trim(),
      hint: document.querySelector("#secModal .modal-hint").textContent.trim(),
      btn: document.getElementById("secOk").textContent.trim(),
      icon: document.querySelector("#secModal .modal-icon").className,
      preview: {
        title: document.querySelector("#secModal .tg-notice-title").textContent.trim(),
        sessions: document.querySelector("#secModal .tg-notice-btn").textContent.trim(),
        ok: document.querySelector("#secModal .tg-notice-ok").textContent.trim(),
        when: document.getElementById("secWhen").textContent.trim(),
      },
    };
  });
  check("security prompt opens after login", sec.open, JSON.stringify(sec));
  check("security prompt informs instead of warning",
    sec.title === "Telegram will show you a notification", sec.title);
  check("security prompt explains it is safe and needed for the files",
    /Telegram/.test(sec.desc) && /safe/.test(sec.desc) && /files/.test(sec.desc), sec.desc);
  check("security prompt says what to do in Telegram",
    /Yes, it's me/.test(sec.hint) && /Telegram/.test(sec.hint), sec.hint);
  check("security prompt confirms with 'got it'",
    sec.btn === "Got it", sec.btn);
  // the replica of Telegram's own dialog, so the alert is recognised
  check("replica of the Telegram alert is shown",
    sec.preview.title === "New login to your account"
      && /Yes, it's me/.test(sec.preview.ok)
      && /Check Active Sessions/.test(sec.preview.sessions),
    JSON.stringify(sec.preview));
  check("replica carries the moment it happens", sec.preview.when.length > 6, sec.preview.when);
  check("the prompt uses a calm icon, not a warning",
    sec.icon.includes("info") && !sec.icon.includes("danger"), sec.icon);

  // Escape closes it
  await page.keyboard.press("Escape");
  await raf(page, 20);
  check("Escape closes the security prompt",
    await page.evaluate(() => document.getElementById("secModal").classList.contains("hidden")));

  // the button closes it too
  await page.evaluate(() => openSecurityNotice());
  await raf(page, 15);
  await page.click("#secOk");
  await raf(page, 20);
  check("the button closes the security prompt",
    await page.evaluate(() => document.getElementById("secModal").classList.contains("hidden")));

  // the admin signs in to the dashboard, no prompt (no Telegram session)
  await page.evaluate(() => onLoggedIn({ uid: -1, name: "Admin", admin: true }));
  await raf(page, 40);
  check("admin login skips the security prompt",
    await page.evaluate(() => document.getElementById("secModal").classList.contains("hidden")));

  // the prompt is translated
  const fr = await page.evaluate(() => {
    setLang("fr");
    const out = {
      title: document.querySelector("#secModal .modal-title").textContent.trim(),
      btn: document.getElementById("secOk").textContent.trim(),
    };
    setLang("en");
    return out;
  });
  check("security prompt is translated",
    fr.title === "Telegram va afficher une notification" && fr.btn === "Compris",
    JSON.stringify(fr));

  page.off("request", meHandler);
  await page.setRequestInterception(false);

  check("no JS page errors", errors.length === 0, JSON.stringify(errors.slice(0, 3)));
  console.log(failures.length ? `\n${failures.length} FAILURES` : "\nALL LOGIN CHECKS PASSED");
  await browser.close();
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });