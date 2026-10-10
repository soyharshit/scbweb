/* Admin panel / PIN / banners browser test (session-tab removal included) */
const puppeteer = require("puppeteer");

const URL = "http://127.0.0.1:8000/";
const PIN = (() => {
  const fs = require("fs");
  const path = require("path");
  // The admin PIN lives in webapp/.env (git-ignored). The fallback keeps the
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

// the dashboard payload the test renders (until > 0 = a user holds a grant)
function mkStats(until) {
  const daily = [];
  for (let i = 29; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    daily.push({
      day: d.toISOString().slice(0, 10),
      files: i % 7, visits: (i * 3) % 11, logins: 0,
    });
  }
  return {
    totals: { users: 42, logged_in: 7, visited: 30, returning: 12, banned: 2,
              files_saved: 99, jobs_total: 120, saves_today: 5, visits_today: 9 },
    daily, active_jobs: 3,
    users: [
      { uid: 111, name: "Alice", banned: false, visits: 5,
        last_seen: Date.now() / 1000, saves_today: 2,
        phone: "+15854346400", ip: "203.0.113.7", unlimited_until: until || 0 },
      { uid: 222, name: "Bob", banned: true, visits: 1, last_seen: 0, saves_today: 0,
        phone: "+34600111222", ip: "198.51.100.23", unlimited_until: 0 },
    ],
  };
}

(async () => {
  const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 375, height: 800, isMobile: true });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.goto(URL, { waitUntil: "networkidle0" });

  // ---------------------------------------------------------- login page
  const sessionGone = await page.evaluate(() => ({
    tabAttrs: document.querySelectorAll("[data-tab]").length,
    panel: !document.getElementById("tab-session"),
    textarea: !document.getElementById("sessionInput"),
    tabsBlock: document.querySelectorAll("#loginSection .tabs").length,
  }));
  check("session-string login removed",
    sessionGone.tabAttrs === 0 && sessionGone.panel && sessionGone.textarea && sessionGone.tabsBlock === 0,
    JSON.stringify(sessionGone));

  const adminHint = await page.evaluate(() => {
    const el = document.querySelector("#phoneStep .hint-text");
    return el ? el.textContent.trim() : "";
  });
  check("admin login hint shown on the phone step", adminHint.length > 10, adminHint);

  // --------------------------------------- admin logs in via the phone box
  await page.type("#phoneInput", PIN);
  await page.click("#sendCodeBtn");
  await page.waitForFunction(
    () => !document.getElementById("appSection").classList.contains("hidden"),
    { timeout: 10000 }
  );
  await page.waitForFunction(
    () => !document.getElementById("pinModal").classList.contains("hidden"),
    { timeout: 10000 }
  );
  await raf(page, 30);

  const pinState = await page.evaluate(() => ({
    modalOpen: !document.getElementById("pinModal").classList.contains("hidden"),
    cancelHidden: document.getElementById("pinCancel").classList.contains("hidden"),
    currentHidden: document.getElementById("pinCurrentField").classList.contains("hidden"),
    saveHidden: document.getElementById("saveTab").classList.contains("hidden"),
    historyHidden: document.getElementById("historyTab").classList.contains("hidden"),
    tabsBarHidden: document.getElementById("appTabs").classList.contains("hidden"),
    pinTitle: document.getElementById("pinTitle").textContent.trim(),
    userName: document.getElementById("userName").textContent.trim(),
  }));
  check("force-change modal opens after admin login", pinState.modalOpen, JSON.stringify(pinState));
  check("forced modal has no cancel button", pinState.cancelHidden);
  check("forced modal skips the current-password field", pinState.currentHidden);
  check("save and history tabs hidden for the admin",
    pinState.saveHidden && pinState.historyHidden, JSON.stringify(pinState));
  check("top tab bar hidden for the admin", pinState.tabsBarHidden, JSON.stringify(pinState));
  check("header shows Admin", pinState.userName.toLowerCase() === "admin", pinState.userName);
  check("pin modal title set", pinState.pinTitle.length > 3, pinState.pinTitle);

  await page.keyboard.press("Escape");
  await raf(page, 20);
  const stillOpen = await page.evaluate(
    () => !document.getElementById("pinModal").classList.contains("hidden"));
  check("forced modal cannot be dismissed with Escape", stillOpen);

  // local validation: mismatched confirmation
  await page.type("#pinNew", "654321");
  await page.type("#pinConfirm", "999999");
  await page.click("#pinSave");
  const err1 = await page.evaluate(() => ({
    text: document.getElementById("pinError").textContent.trim(),
    hidden: document.getElementById("pinError").classList.contains("hidden"),
  }));
  check("password mismatch error shown", !err1.hidden && err1.text.length > 3, JSON.stringify(err1));

  // close the forced modal programmatically and open the admin tab
  await page.evaluate(() => {
    closePinModal(true);
    state.me.must_change_pin = false;
    activateAppTab("admin");
  });
  await page.waitForFunction(
    () => !document.getElementById("adminBody").classList.contains("hidden"),
    { timeout: 10000 }
  );
  await raf(page, 30);

  const adminDom = await page.evaluate(() => ({
    panelOpen: !document.getElementById("adminPanel").classList.contains("hidden"),
    skeletonHidden: document.getElementById("adminSkeleton").classList.contains("hidden"),
    tiles: document.querySelectorAll(".stat-tile").length,
    statUsers: document.getElementById("statUsers").textContent.trim(),
    charts: document.querySelectorAll(".chart-card").length,
    chartState: document.querySelectorAll("#chartFiles svg, #chartFiles .empty").length,
    savingOn: document.getElementById("savingToggle").checked,
    userList: !!document.getElementById("userList"),
    noUsers: !document.getElementById("adminNoUsers").classList.contains("hidden"),
    rows: document.querySelectorAll("#userList .user-row").length,
  }));
  check("admin panel opens with live stats", adminDom.panelOpen && adminDom.skeletonHidden,
    JSON.stringify(adminDom));
  check("ten stat tiles rendered", adminDom.tiles === 10, String(adminDom.tiles));
  check("stat value populated", /^\d+$/.test(adminDom.statUsers), adminDom.statUsers);
  check("two chart cards", adminDom.charts === 2, String(adminDom.charts));
  check("chart has svg or empty state", adminDom.chartState >= 1, String(adminDom.chartState));
  check("saving toggle reflects enabled state", adminDom.savingOn === true);
  check("user list state consistent",
    adminDom.userList && (adminDom.rows === 0 ? adminDom.noUsers : !adminDom.noUsers),
    JSON.stringify(adminDom));

  // --------------------------------- sub-tabs: Overview / Settings / Users
  const sub = await page.evaluate(() => ({
    overview: !document.getElementById("adminOverview").classList.contains("hidden"),
    settings: !document.getElementById("adminSettings").classList.contains("hidden"),
    users: !document.getElementById("adminUsers").classList.contains("hidden"),
    active: document.querySelector(".admin-subtabs .tab.active").dataset.atab,
  }));
  check("overview sub-tab is the default",
    sub.overview && !sub.settings && !sub.users && sub.active === "overview",
    JSON.stringify(sub));

  await page.click("#adminSettingsTab");
  const sub2 = await page.evaluate(() => ({
    overview: !document.getElementById("adminOverview").classList.contains("hidden"),
    settings: !document.getElementById("adminSettings").classList.contains("hidden"),
    active: document.querySelector(".admin-subtabs .tab.active").dataset.atab,
  }));
  check("settings sub-tab switches",
    sub2.settings && !sub2.overview && sub2.active === "settings", JSON.stringify(sub2));

  await page.click("#adminUsersTab");
  const sub3 = await page.evaluate(() => ({
    settings: !document.getElementById("adminSettings").classList.contains("hidden"),
    users: !document.getElementById("adminUsers").classList.contains("hidden"),
    active: document.querySelector(".admin-subtabs .tab.active").dataset.atab,
  }));
  check("users sub-tab switches",
    sub3.users && !sub3.settings && sub3.active === "users", JSON.stringify(sub3));

  await page.click("#adminOverviewTab");
  const sub4 = await page.evaluate(() => ({
    overview: !document.getElementById("adminOverview").classList.contains("hidden"),
    users: !document.getElementById("adminUsers").classList.contains("hidden"),
  }));
  check("overview sub-tab switches back", sub4.overview && !sub4.users, JSON.stringify(sub4));

  // ---------------------------------------- synthetic render (full shapes)
  await page.evaluate((p) => renderAdmin(p), mkStats(0));

  const synth = await page.evaluate(() => ({
    users: document.getElementById("statUsers").textContent.trim(),
    files: document.getElementById("statFiles").textContent.trim(),
    active: document.getElementById("statActive").textContent.trim(),
    svg: !!document.querySelector("#chartFiles svg"),
    line: !!document.querySelector("#chartFiles .chart-line"),
    bars: document.querySelectorAll("#chartVisits .bar").length,
    rows: document.querySelectorAll("#userList .user-row").length,
    banBtn: (document.querySelector("#userList .user-row .btn") || {}).textContent || "",
    pill: document.getElementById("adminUserCount").textContent.trim(),
    pillHidden: document.getElementById("adminUserCount").classList.contains("hidden"),
    meta: (document.querySelector("#userList .user-meta") || {}).textContent || "",
  }));
  check("tile values render",
    synth.users === "42" && synth.files === "99" && synth.active === "3", JSON.stringify(synth));
  check("files chart drawn as svg line", synth.svg && synth.line);
  check("visits chart has 30 bars", synth.bars === 30, String(synth.bars));
  check("user rows rendered", synth.rows === 2, String(synth.rows));
  check("ban button present", synth.banBtn.length > 0, synth.banBtn);
  check("user count pill", !synth.pillHidden && /2/.test(synth.pill), synth.pill);
  check("user meta shows phone, ip and visits",
    synth.meta.includes("+15854346400") && synth.meta.includes("203.0.113.7")
    && /5/.test(synth.meta), synth.meta);

  // ------------------------- ban button posts to the API (intercepted, no DB)
  await page.evaluate(() => activateAdminSub("users"));
  await raf(page, 30);
  await page.setRequestInterception(true);
  const banHits = [];
  const banHandler = (req) => {
    if (req.url().includes("/api/admin/ban")) {
      banHits.push(req.postData() || "");
      req.respond({ status: 200, contentType: "application/json",
                    body: JSON.stringify({ ok: true, uid: 111, banned: true }) });
      return;
    }
    req.continue();
  };
  page.on("request", banHandler);
  await page.click("#userList .user-row .btn");
  await raf(page, 30);
  check("ban button calls /api/admin/ban",
    banHits.length === 1 && banHits[0].includes('"uid":111'), JSON.stringify(banHits));
  page.off("request", banHandler);
  await page.setRequestInterception(false);

  // the ban handler above re-fetched real stats: put the test rows back
  await page.evaluate((p) => renderAdmin(p), mkStats(0));

  // ------------------------- unlimited quota: inline grant (intercepted, no DB)
  const grantState = { until: 0 };
  await page.setRequestInterception(true);
  const unHits = [];
  const unHandler = (req) => {
    if (req.url().includes("/api/admin/unlimited")) {
      const body = JSON.parse(req.postData() || "{}");
      unHits.push(req.postData() || "");
      grantState.until = body.days ? Date.now() / 1000 + body.days * 86400 : 0;
      req.respond({ status: 200, contentType: "application/json",
                    body: JSON.stringify({ ok: true, uid: body.uid, days: body.days,
                                           unlimited_until: grantState.until }) });
      return;
    }
    if (req.url().includes("/api/admin/stats")) {
      req.respond({ status: 200, contentType: "application/json",
                    body: JSON.stringify(mkStats(grantState.until)) });
      return;
    }
    req.continue();
  };
  page.on("request", unHandler);

  const before = await page.evaluate(() => ({
    labels: [...document.querySelectorAll("#userList .user-row .user-actions .btn")]
      .map((b) => b.textContent),
    forms: document.querySelectorAll("#userList .user-unlimited:not(.hidden)").length,
    badges: [...document.querySelectorAll("#userList .user-meta")].map((m) => m.textContent),
  }));
  check("every row offers the unlimited control",
    before.labels.filter((l) => l === "Unlimited").length === 2, JSON.stringify(before.labels));
  check("grant form starts closed", before.forms === 0, String(before.forms));
  check("no grant badge yet",
    !before.badges.some((b) => b.includes("Unlimited")), JSON.stringify(before.badges));

  await page.evaluate(() => {
    const b = [...document.querySelectorAll("#userList .user-row .user-actions .btn")]
      .find((el) => el.textContent === "Unlimited");
    b.click();
  });
  await raf(page, 20);
  const form = await page.evaluate(() => {
    const f = document.querySelector("#userList .user-unlimited:not(.hidden)");
    if (!f) return null;
    const input = f.querySelector("input");
    return {
      label: (f.querySelector("label") || {}).textContent || "",
      type: input && input.type, value: input && input.value,
      min: input && input.min, max: input && input.max,
      buttons: [...f.querySelectorAll("button")].map((b) => b.textContent),
      hint: (f.querySelector("span") || {}).textContent || "",
      focused: document.activeElement === input,
    };
  });
  check("grant form opens with a days input",
    form && form.type === "number" && form.value === "7"
    && form.min === "1" && form.max === "3650", JSON.stringify(form));
  check("grant form labelled and hinted",
    form && form.label === "Days" && form.hint.includes("count from now")
    && form.buttons.length === 2, JSON.stringify(form));
  check("days input takes focus", !!(form && form.focused), JSON.stringify(form));

  await page.evaluate(() => {
    const b = [...document.querySelectorAll("#userList .user-unlimited:not(.hidden) button")]
      .find((el) => el.textContent === "Cancel");
    b.click();
  });
  await raf(page, 15);
  const closedCount = await page.evaluate(
    () => document.querySelectorAll("#userList .user-unlimited:not(.hidden)").length);
  check("cancel closes the form without posting",
    closedCount === 0 && unHits.length === 0, `${closedCount} ${unHits.length}`);

  await page.evaluate(() => {
    const b = [...document.querySelectorAll("#userList .user-row .user-actions .btn")]
      .find((el) => el.textContent === "Unlimited");
    b.click();
  });
  await raf(page, 15);
  await page.evaluate(() => {
    document.querySelector("#userList .user-unlimited:not(.hidden) input").value = "3";
    [...document.querySelectorAll("#userList .user-unlimited:not(.hidden) button")]
      .find((b) => b.textContent === "Give").click();
  });
  await raf(page, 40);
  check("give posts the days to /api/admin/unlimited",
    unHits.length === 1 && unHits[0].includes('"uid":111') && unHits[0].includes('"days":3'),
    JSON.stringify(unHits));

  const after = await page.evaluate(() => ({
    labels: [...document.querySelectorAll("#userList .user-row .user-actions .btn")]
      .map((b) => b.textContent),
    badges: [...document.querySelectorAll("#userList .user-meta")].map((m) => m.textContent),
    toast: (document.getElementById("toast") || {}).textContent || "",
    forms: document.querySelectorAll("#userList .user-unlimited:not(.hidden)").length,
  }));
  check("grant shows as a badge with its days left",
    after.badges.some((b) => b.includes("Unlimited") && /3d left/.test(b)),
    JSON.stringify(after.badges));
  check("granted row switches to Remove",
    after.labels.filter((l) => l === "Remove").length === 1
    && after.labels.filter((l) => l === "Unlimited").length === 1, JSON.stringify(after.labels));
  check("grant toast shown", after.toast.includes("Unlimited quota activated."), after.toast);
  check("form closed after giving", after.forms === 0, String(after.forms));

  await page.evaluate(() => {
    const b = [...document.querySelectorAll("#userList .user-row .user-actions .btn")]
      .find((el) => el.textContent === "Remove");
    b.click();
  });
  await raf(page, 40);
  const removed = await page.evaluate(() => ({
    labels: [...document.querySelectorAll("#userList .user-row .user-actions .btn")]
      .map((b) => b.textContent),
    badges: [...document.querySelectorAll("#userList .user-meta")].map((m) => m.textContent),
    toast: (document.getElementById("toast") || {}).textContent || "",
  }));
  check("remove posts days 0",
    unHits.length === 2 && unHits[1].includes('"days":0'), JSON.stringify(unHits));
  check("grant badge disappears",
    !removed.badges.some((b) => b.includes("Unlimited")), JSON.stringify(removed.badges));
  check("remove toast shown", removed.toast.includes("Unlimited quota removed."), removed.toast);
  check("remove puts the button back",
    removed.labels.filter((l) => l === "Unlimited").length === 2, JSON.stringify(removed.labels));

  page.off("request", unHandler);
  await page.setRequestInterception(false);

  // ------------------------------------- notice banner + save-off on dashboard
  const banners = await page.evaluate(() => {
    state.me.notice = "Scheduled maintenance tonight.";
    state.me.saving_enabled = false;
    renderQuota();
    return {
      noticeHidden: document.getElementById("noticeBanner").classList.contains("hidden"),
      noticeText: document.getElementById("noticeBanner").textContent.trim(),
      saveOffHidden: document.getElementById("saveOffBanner").classList.contains("hidden"),
      fetchDisabled: document.getElementById("fetchBtn").disabled,
    };
  });
  check("notice banner visible",
    !banners.noticeHidden && banners.noticeText.indexOf("maintenance") >= 0, JSON.stringify(banners));
  check("save-off banner + disabled save button",
    !banners.saveOffHidden && banners.fetchDisabled, JSON.stringify(banners));

  // restore in-page state
  await page.evaluate(() => {
    state.me.notice = "";
    state.me.saving_enabled = true;
    renderQuota();
  });

  // --------------------------------- two notices, each with its own colour
  const notices = await page.evaluate(() => {
    state.me.notice = "Scheduled maintenance tonight.";
    state.me.notice_color = "yellow";
    state.me.notice_login = "The site opens at 8:00.";
    state.me.notice_login_color = "green";
    activateAdminSub("settings");
    renderAdmin({ totals: {}, daily: [], users: [] });
    return {
      dash: document.getElementById("noticeInput").value,
      login: document.getElementById("noticeLoginInput").value,
      dashActive: document.querySelector('.swatch[data-target="notice"].is-active').dataset.color,
      loginActive: document.querySelector('.swatch[data-target="notice-login"].is-active').dataset.color,
      swatches: document.querySelectorAll(".swatch").length,
    };
  });
  check("both notices load into the form",
    notices.dash === "Scheduled maintenance tonight." && notices.login === "The site opens at 8:00.",
    JSON.stringify(notices));
  check("stored colours are pre-selected",
    notices.dashActive === "yellow" && notices.loginActive === "green", JSON.stringify(notices));
  check("four colour swatches per notice", notices.swatches === 8, String(notices.swatches));

  await page.click('.swatch[data-target="notice"][data-color="red"]');
  const picked = await page.evaluate(() => ({
    active: document.querySelector('.swatch[data-target="notice"].is-active').dataset.color,
    login: document.querySelector('.swatch[data-target="notice-login"].is-active').dataset.color,
  }));
  check("picking a colour only affects its own notice",
    picked.active === "red" && picked.login === "green", JSON.stringify(picked));

  // saving posts both notices and both colours (intercepted, no DB writes)
  await page.setRequestInterception(true);
  let settingsBody = null;
  const settingsHandler = (req) => {
    if (req.url().includes("/api/admin/settings")) {
      settingsBody = req.postData() || "";
      req.respond({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: true, notice: "Scheduled maintenance tonight.",
          notice_color: "red", notice_login: "The site opens at 8:00.",
          notice_login_color: "green", saving_enabled: true }),
      });
      return;
    }
    req.continue();
  };
  page.on("request", settingsHandler);
  await page.click("#settingsSaveBtn");
  await raf(page, 30);
  page.off("request", settingsHandler);
  await page.setRequestInterception(false);
  const sentSettings = JSON.parse(settingsBody || "{}");
  check("settings save posts both notices and colours",
    sentSettings.notice === "Scheduled maintenance tonight."
    && sentSettings.notice_login === "The site opens at 8:00."
    && sentSettings.notice_color === "red"
    && sentSettings.notice_login_color === "green", settingsBody);

  // the dashboard banner paints with its colour, the login page with its own
  const noticeBanners = await page.evaluate(() => {
    state.me.notice_color = "yellow";   // the save above left it on red
    renderQuota();
    const dash = document.getElementById("noticeBanner");
    const dashCls = dash.className;
    const dashText = dash.textContent;
    showLogin();
    const login = document.getElementById("noticeBanner");
    const out = { dashCls, dashText, loginCls: login.className, loginText: login.textContent };
    activateAppTab("admin");
    activateAdminSub("settings");
    return out;
  });
  check("dashboard banner uses its own colour",
    noticeBanners.dashCls.includes("alert-yellow")
    && noticeBanners.dashText === "Scheduled maintenance tonight.",
    JSON.stringify(noticeBanners));
  check("login page shows the login notice",
    noticeBanners.loginCls.includes("alert-green")
    && noticeBanners.loginText === "The site opens at 8:00.",
    JSON.stringify(noticeBanners));

  // admin view resets for non-admins
  const demoted = await page.evaluate(() => {
    state.me.is_admin = false;
    renderAdminTab();
    const tabsBack = !document.getElementById("saveTab").classList.contains("hidden")
      && !document.getElementById("historyTab").classList.contains("hidden")
      && !document.getElementById("appTabs").classList.contains("hidden");
    const backToSave = !document.getElementById("savePanel").classList.contains("hidden")
      && document.getElementById("adminPanel").classList.contains("hidden");
    state.me.is_admin = true;
    renderAdminTab();
    return { tabsBack, backToSave };
  });
  check("tabs and view reset for non-admins",
    demoted.tabsBack && demoted.backToSave, JSON.stringify(demoted));

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth);
  check("admin panel fits 375px", overflow <= 1, String(overflow));

  check("no JS page errors", errors.length === 0, JSON.stringify(errors.slice(0, 3)));
  console.log(failures.length ? `\n${failures.length} FAILURES` : "\nALL ADMIN CHECKS PASSED");
  await browser.close();
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
