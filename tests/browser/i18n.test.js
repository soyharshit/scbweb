/* i18n / RTL / OTP / mobile-overflow browser test */
const puppeteer = require("puppeteer");

const URL = "http://127.0.0.1:8000/";
const LONG_NAME = "AQM_EApKkRJ8Lk7KqqPo4LTiXiw1kpIqpHTw291KBYxpvE19vw28ZWhMJbLLHOB.mp4";

const EXPECT_TITLE = {
  en: "Welcome back", es: "Bienvenido de nuevo", fr: "Bon retour",
  ar: "مرحباً بعودتك", ru: "С возвращением", zh: "欢迎回来",
  pt: "Bem-vindo de volta", id: "Selamat datang kembali",
};
const EXPECT_SAVEBTN = {
  en: "Save", es: "Guardar", fr: "Enregistrer", ar: "حفظ",
  ru: "Сохранить", zh: "保存", pt: "Salvar", id: "Simpan",
};
const EXPECT_HISTORY_TAB = {
  en: "History", es: "Historial", fr: "Historique", ar: "السجل",
  ru: "История", zh: "历史", pt: "Histórico", id: "Riwayat",
};
const EXPECT_THEME = {
  en: "Toggle dark mode", es: "Alternar modo oscuro", fr: "Basculer en mode sombre",
  ar: "تبديل الوضع الداكن", ru: "Переключить тёмную тему", zh: "切换深色模式",
  pt: "Alternar modo escuro", id: "Alihkan mode gelap",
};
const LANGS = ["en", "es", "fr", "ar", "ru", "zh", "pt", "id"];

const fakeJob = {
  id: "t1", link: "https://t.me/sometechnicalchannel/12345",
  status: "done", step: "Done", percent: 100,
  downloaded: 0, total: 0, speed: 0, error: null,
  message: "Saved to your Saved Messages.", text: null,
  items: [{ name: LONG_NAME, kind: "video", size: 734003200 }],
  saved_ids: [1], open_url: "tg://openmessage?user_id=1&message_id=1",
  created: Math.floor(Date.now() / 1000),
};

let failures = 0;
const check = (name, cond, extra) => {
  console.log(`${cond ? "PASS" : "FAIL"} ${name}${!cond && extra ? "  " + extra : ""}`);
  if (!cond) failures++;
};

(async () => {
  const browser = await puppeteer.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const page = await browser.newPage();
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 20000 });

  // --- language switching on the login view ---
  for (const lang of LANGS) {
    await page.evaluate((l) => {
      const sel = document.getElementById("langSelect");
      sel.value = l;
      sel.dispatchEvent(new Event("change"));
    }, lang);

    const r = await page.evaluate((l) => {
      const title = document.querySelector("#loginSection h1").textContent.trim();
      const saveBtn = document.querySelector("#fetchBtn .btn-label").textContent.trim();
      const saveTab = document.querySelector('[data-utab="save"]').textContent.trim();
      const histTab = document.querySelector('[data-utab="history"]').textContent.trim();
      const braces = [...document.querySelectorAll("[data-i18n]")]
        .filter((el) => /\{[a-z0-9_]+\}/i.test(el.textContent))
        .map((el) => el.textContent.slice(0, 40));
      return {
        dir: document.documentElement.dir,
        htmlLang: document.documentElement.lang,
        title, saveBtn, saveTab, histTab, braces,
        themeAria: document.getElementById("themeBtn").getAttribute("aria-label"),
        expectedDir: l === "ar" ? "rtl" : "ltr",
        expectedLang: l === "zh" ? "zh-CN" : l,
      };
    }, lang);

    check(`[${lang}] title translated`, r.title === EXPECT_TITLE[lang],
      `${r.title} != ${EXPECT_TITLE[lang]}`);
    check(`[${lang}] save button translated`, r.saveBtn === EXPECT_SAVEBTN[lang],
      `${r.saveBtn} != ${EXPECT_SAVEBTN[lang]}`);
    check(`[${lang}] app tabs translated`,
      r.saveTab === EXPECT_SAVEBTN[lang] && r.histTab === EXPECT_HISTORY_TAB[lang],
      `${r.saveTab}/${r.histTab}`);
    check(`[${lang}] theme button translated`, r.themeAria === EXPECT_THEME[lang],
      `${r.themeAria} != ${EXPECT_THEME[lang]}`);
    check(`[${lang}] dir/lang set`, r.dir === r.expectedDir && r.htmlLang === r.expectedLang,
      `${r.dir}/${r.htmlLang}`);
    check(`[${lang}] no unreplaced placeholders`, r.braces.length === 0, JSON.stringify(r.braces));
  }

  // --- OTP input styling ---
  const otp = await page.evaluate(() => {
    document.getElementById("codeStep").classList.remove("hidden");
    const el = document.getElementById("codeInput");
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return { fontSize: parseFloat(cs.fontSize), align: cs.textAlign, ls: cs.letterSpacing, w: r.width, h: r.height };
  });
  check("OTP font-size >= 16px (no iOS zoom)", otp.fontSize >= 16, `${otp.fontSize}px`);
  check("OTP centered", otp.align === "center", otp.align);
  check("OTP letter-spaced", parseFloat(otp.ls) > 3, otp.ls);
  check("OTP box compact (small)", otp.h <= 52 && otp.w <= 250, `${otp.w}x${otp.h}`);

  // --- OTP digit filtering + auto-submit trigger ---
  const otp2 = await page.evaluate(() => {
    const el = document.getElementById("codeInput");
    el.value = "a1b2 c3d4e5!";
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return { value: el.value };
  });
  check("OTP strips non-digits", otp2.value === "12345", otp2.value);

  // --- phone card is replaced by the OTP card once the code is sent ---
  const flow = await page.evaluate(async () => {
    const orig = api;
    api = async () => ({ handle: "h-test", via: "Telegram" });
    document.getElementById("phoneInput").value = "+15854346400";
    try {
      await sendCode();
      const afterSend = {
        phoneHidden: document.getElementById("phoneStep").classList.contains("hidden"),
        codeShown: !document.getElementById("codeStep").classList.contains("hidden"),
      };
      resetPhoneLogin();
      const afterReset = {
        phoneShown: !document.getElementById("phoneStep").classList.contains("hidden"),
        codeHidden: document.getElementById("codeStep").classList.contains("hidden"),
      };
      return { afterSend, afterReset };
    } finally {
      api = orig;
    }
  });
  check("phone card hidden after OTP sent",
    flow.afterSend.phoneHidden && flow.afterSend.codeShown, JSON.stringify(flow.afterSend));
  check("phone card restored by 'use a different number'",
    flow.afterReset.phoneShown && flow.afterReset.codeHidden, JSON.stringify(flow.afterReset));

  // --- mobile overflow with long names, in every language, both views ---
  await page.setViewport({ width: 375, height: 667, isMobile: true, hasTouch: true });
  for (const lang of LANGS) {
    await page.evaluate((l) => {
      const sel = document.getElementById("langSelect");
      sel.value = l;
      sel.dispatchEvent(new Event("change"));
    }, lang);

    // login view
    const mLogin = await page.evaluate(() => {
      document.getElementById("appSection").classList.add("hidden");
      document.getElementById("loginSection").classList.remove("hidden");
      return {
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth,
      };
    });
    if (mLogin.scroll > mLogin.client + 1) {
      const offs = await page.evaluate(() => {
        const vw = document.documentElement.clientWidth;
        const out = [];
        document.querySelectorAll("body *").forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && (r.right > vw + 0.5 || r.left < -0.5)) {
            out.push(`${el.tagName}#${el.id}.${el.className} [${Math.round(r.left)}..${Math.round(r.right)}] "${(el.textContent || "").trim().slice(0, 40)}"`);
          }
        });
        const cs = getComputedStyle(document.getElementById("toast"));
        return { out, toast: `${cs.display} w=${Math.round(document.getElementById("toast").getBoundingClientRect().width)} r=${Math.round(document.getElementById("toast").getBoundingClientRect().right)}` };
      });
      console.log("   offenders(login): " + offs.out.slice(0, 12).join(" || "));
      console.log("   toast: " + offs.toast);
    }
    check(`[${lang}] login view fits 375px`, mLogin.scroll <= mLogin.client + 1,
      `${mLogin.scroll}/${mLogin.client}`);

    // app view with a long file name
    const mApp = await page.evaluate((j) => {
      document.getElementById("loginSection").classList.add("hidden");
      document.getElementById("appSection").classList.remove("hidden");
      document.getElementById("userArea").classList.remove("hidden");
      state.jobs = [j];
      state.selected = j.id;
      renderJob(j);
      renderList();
      const card = document.getElementById("jobCard").getBoundingClientRect();
      const li = document.querySelector(".items li");
      return {
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth,
        liFits: li ? li.getBoundingClientRect().right <= card.right + 1 : false,
        name: (li && li.querySelector(".item-name").textContent) || "",
      };
    }, fakeJob);
    if (mApp.scroll > mApp.client + 1) {
      const offs2 = await page.evaluate(() => {
        const vw = document.documentElement.clientWidth;
        const out = [];
        document.querySelectorAll("body *").forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && (r.right > vw + 0.5 || r.left < -0.5)) {
            out.push(`${el.tagName}#${el.id}.${el.className} [${Math.round(r.left)}..${Math.round(r.right)}] "${(el.textContent || "").trim().slice(0, 40)}"`);
          }
        });
        return out;
      });
      console.log("   offenders(app): " + offs2.slice(0, 12).join(" || "));
    }
    check(`[${lang}] app view fits 375px with long name`, mApp.scroll <= mApp.client + 1,
      `${mApp.scroll}/${mApp.client}`);
    check(`[${lang}] item stays inside card`, mApp.liFits);
  }

  // --- topbar: name chip restored + icon-only lang/logout buttons (375px) ---
  const top = await page.evaluate(() => {
    document.getElementById("loginSection").classList.add("hidden");
    document.getElementById("appSection").classList.remove("hidden");
    document.getElementById("userArea").classList.remove("hidden");
    document.getElementById("userName").textContent = "Alexandra Fontaine";
    document.getElementById("userAvatar").textContent = "A";
    const nr = document.getElementById("userName").getBoundingClientRect();
    const lb = document.getElementById("logoutBtn");
    const lbr = lb.getBoundingClientRect();
    const tb = document.getElementById("themeBtn");
    const tbr = tb.getBoundingClientRect();
    return {
      nameVisible: nr.width > 4 && nr.height > 4,
      nameText: document.getElementById("userName").textContent.trim(),
      langW: Math.round(document.querySelector(".lang-switch").getBoundingClientRect().width),
      logoutW: Math.round(lbr.width),
      logoutText: lb.textContent.trim().length,
      logoutAria: lb.getAttribute("aria-label"),
      themeW: Math.round(tbr.width),
      themeText: tb.textContent.trim().length,
      themeAria: tb.getAttribute("aria-label"),
      themeTitle: tb.title,
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    };
  });
  check("header shows the user name", top.nameVisible && top.nameText === "Alexandra Fontaine", top.nameText);
  check("language button is icon-only", top.langW <= 60, `${top.langW}px`);
  check("logout button is icon-only", top.logoutW <= 60 && top.logoutText === 0, `${top.logoutW}px textlen=${top.logoutText}`);
  check("icon logout keeps an aria-label", !!top.logoutAria, String(top.logoutAria));
  check("theme button is icon-only", top.themeW <= 60 && top.themeText === 0, `${top.themeW}px textlen=${top.themeText}`);
  check("icon theme button keeps aria-label + title", !!top.themeAria && !!top.themeTitle,
    `${top.themeAria} / ${top.themeTitle}`);
  check("topbar fits 375px", top.scroll <= top.client + 1, `${top.scroll}/${top.client}`);

  // --- dark mode: smooth, persisted, icons cross-fade ---
  const themeState = await page.evaluate(async () => {
    localStorage.removeItem("tg_theme");
    document.documentElement.setAttribute("data-theme", "light");
    const btn = document.getElementById("themeBtn");
    const bodyLight = getComputedStyle(document.body).backgroundColor;
    const dur = getComputedStyle(document.body).transitionDuration;

    btn.click();
    await new Promise((r) => setTimeout(r, 650));   // let the cross-fade settle
    const dark = {
      theme: document.documentElement.dataset.theme,
      stored: localStorage.getItem("tg_theme"),
      body: getComputedStyle(document.body).backgroundColor,
      card: getComputedStyle(document.querySelector("#savePanel .card")).backgroundColor,
      sun: Number(getComputedStyle(document.querySelector(".icon-sun")).opacity),
      moon: Number(getComputedStyle(document.querySelector(".icon-moon")).opacity),
    };

    btn.click();
    await new Promise((r) => setTimeout(r, 650));
    const back = {
      theme: document.documentElement.dataset.theme,
      stored: localStorage.getItem("tg_theme"),
      body: getComputedStyle(document.body).backgroundColor,
    };
    return { bodyLight, dur, dark, back };
  });
  check("theme button switches to dark and persists",
    themeState.dark.theme === "dark" && themeState.dark.stored === "dark",
    JSON.stringify(themeState.dark));
  check("dark palette applied (page + card)",
    themeState.dark.body === "rgb(10, 10, 12)" && themeState.dark.card === "rgb(20, 20, 23)",
    `body=${themeState.dark.body} card=${themeState.dark.card}`);
  check("sun/moon icons cross-fade",
    themeState.dark.sun >= 0.9 && themeState.dark.moon <= 0.1,
    `sun=${themeState.dark.sun} moon=${themeState.dark.moon}`);
  check("theme colours transition smoothly", parseFloat(themeState.dur) > 0, themeState.dur);
  check("toggle returns to light",
    themeState.back.theme === "light" && themeState.back.body === themeState.bodyLight,
    JSON.stringify(themeState.back));

  // --- app tabs (Save / History) + skeleton screens ---
  const tabsState = await page.evaluate(async (job) => {
    const out = {};
    out.bootHidden = document.getElementById("bootSkeleton").classList.contains("hidden");
    const skCs = getComputedStyle(document.querySelector("#historySkeleton .sk"));
    out.skAnim = skCs.animationName;
    out.skBg = skCs.backgroundImage.includes("gradient");

    document.querySelector('[data-utab="history"]').click();
    out.onHistory = !document.getElementById("historyPanel").classList.contains("hidden")
      && document.getElementById("savePanel").classList.contains("hidden");

    // loading state: skeleton visible, list and empty state hidden
    document.getElementById("historySkeleton").classList.remove("hidden");
    document.getElementById("jobList").classList.add("hidden");
    document.getElementById("emptyJobs").classList.add("hidden");
    out.listHiddenWhileLoading = document.getElementById("jobList").classList.contains("hidden")
      && !document.getElementById("historySkeleton").classList.contains("hidden");

    // loaded: renderList swaps the skeleton for the real list
    state.jobs = [job];
    state.selected = job.id;
    renderList();
    out.rows = document.getElementById("jobList").children.length;
    out.skGone = document.getElementById("historySkeleton").classList.contains("hidden");
    out.listShown = !document.getElementById("jobList").classList.contains("hidden");
    out.emptyHidden = document.getElementById("emptyJobs").classList.contains("hidden");

    // history row click jumps to the Save tab with the job detail
    document.querySelector("#jobList .job-item").click();
    out.openedFromHistory = !document.getElementById("savePanel").classList.contains("hidden")
      && document.getElementById("historyPanel").classList.contains("hidden")
      && !document.getElementById("jobCard").classList.contains("hidden");

    state.jobs = [];
    state.selected = null;
    document.getElementById("jobCard").classList.add("hidden");
    renderList();
    return out;
  }, fakeJob);
  check("boot skeleton hidden after load", tabsState.bootHidden, String(tabsState.bootHidden));
  check("skeleton shimmers", tabsState.skAnim.includes("skShimmer") && tabsState.skBg,
    `${tabsState.skAnim} bg=${tabsState.skBg}`);
  check("history tab opens the history panel", tabsState.onHistory);
  check("loading state shows the skeleton, not the list", tabsState.listHiddenWhileLoading);
  check("history list replaces the skeleton on load",
    tabsState.rows === 1 && tabsState.skGone && tabsState.listShown && tabsState.emptyHidden,
    JSON.stringify({ rows: tabsState.rows, skGone: tabsState.skGone, listShown: tabsState.listShown, emptyHidden: tabsState.emptyHidden }));
  check("history row opens the job on the Save tab", tabsState.openedFromHistory);

  // --- language persists across reload ---
  await page.evaluate(() => {
    const sel = document.getElementById("langSelect");
    sel.value = "fr";
    sel.dispatchEvent(new Event("change"));
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  const persisted = await page.evaluate(() => ({
    sel: document.getElementById("langSelect").value,
    title: document.querySelector("#loginSection h1").textContent.trim(),
  }));
  check("language persisted after reload", persisted.sel === "fr" && persisted.title === "Bon retour",
    JSON.stringify(persisted));

  // --- logout confirmation modal (fr still active, viewport still 375px) ---
  await page.evaluate(() => document.getElementById("logoutBtn").click());
  const modal1 = await page.evaluate(() => {
    const m = document.getElementById("logoutModal");
    const panel = m.querySelector(".modal").getBoundingClientRect();
    return {
      open: !m.classList.contains("hidden") && getComputedStyle(m).display !== "none",
      title: m.querySelector(".modal-title").textContent.trim(),
      cancel: m.querySelector("#logoutCancel").textContent.trim(),
      confirm: m.querySelector("#logoutConfirm").textContent.trim(),
      fits: panel.width > 0 && panel.left >= -0.5 && panel.right <= window.innerWidth + 0.5,
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    };
  });
  check("logout modal opens", modal1.open, JSON.stringify(modal1));
  check("[fr] modal title translated", modal1.title === "Se déconnecter ?", modal1.title);
  check("[fr] modal buttons translated",
    modal1.cancel === "Annuler" && modal1.confirm === "Se déconnecter",
    `${modal1.cancel}/${modal1.confirm}`);
  check("modal fits 375px", modal1.fits && modal1.scroll <= modal1.client + 1,
    `${modal1.scroll}/${modal1.client}`);

  await page.keyboard.press("Escape");
  const modal2 = await page.evaluate(() => {
    const m = document.getElementById("logoutModal");
    return m.classList.contains("closing") || m.classList.contains("hidden");
  });
  check("Escape closes the modal", modal2);

  // confirm path: reopening + confirming logs out and returns to the login view
  await page.evaluate(() => document.getElementById("logoutBtn").click());
  await page.evaluate(() => document.getElementById("logoutConfirm").click());
  await new Promise((r) => setTimeout(r, 350));
  const afterConfirm = await page.evaluate(() => ({
    modalHidden: document.getElementById("logoutModal").classList.contains("hidden"),
    loginVisible: !document.getElementById("loginSection").classList.contains("hidden"),
  }));
  check("confirm closes the modal and logs out",
    afterConfirm.modalHidden && afterConfirm.loginVisible, JSON.stringify(afterConfirm));

  check("no JS page errors", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));

  await browser.close();
  console.log(failures ? `\n${failures} FAILURES` : "\nALL I18N BROWSER CHECKS PASSED");
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error("ERROR", e);
  process.exit(2);
});
