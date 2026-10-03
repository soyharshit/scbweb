/* Telegram Saver - web client (save to Saved Messages, no browser downloads) */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  me: null,
  jobs: [],
  selected: null,
  handle: null,
  timer: null,
  lastSeen: "",
  codeBusy: false,
  via: "",
  phone: "",
  batchOn: false,
};

// ---------------------------------------------------------------- API helper
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const err = new Error(data.error ? translateServer(data.error)
      : t("req_failed", { code: res.status }));
    err.status = res.status;
    throw err;
  }
  return data;
}

let toastTimer = null;
const TOAST_ICONS = {
  ok: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.8 10A10 10 0 1 1 17 3.34"/><path d="m9 11 3 3L22 4"/></svg>',
  err: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>',
};
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = "";
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.innerHTML = isError ? TOAST_ICONS.err : TOAST_ICONS.ok;
  const text = document.createElement("span");
  text.textContent = message;
  el.append(icon, text);
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

// show a login step with its entrance animation restarted
function showStep(el) {
  el.classList.remove("hidden", "fade-in");
  void el.offsetWidth;   // force reflow so the animation replays
  el.classList.add("fade-in");
}

function humanSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function humanSpeed(bytesPerSec) { return humanSize(bytesPerSec) + "/s"; }

function timeAgo(ts) {
  const s = Math.max(1, Math.floor((Date.now() / 1000) - ts));
  if (s < 60) return t("ago.s", { n: s });
  if (s < 3600) return t("ago.m", { n: Math.floor(s / 60) });
  return t("ago.h", { n: Math.floor(s / 3600) });
}

function badgeLabel(status) {
  const key = "status." + status;
  return I18N.en[key] ? t(key) : status;
}

// stop a running job (batch scan, multi-link run, upload)
async function abortJob() {
  if (!state.selected) return;
  busy($("abortBtn"), true);
  try {
    const r = await api(`/api/jobs/${state.selected}/abort`, { method: "POST" });
    renderJob(r.job);          // the response carries the stopped job already
  } catch (e) {
    toast(translateServer(e.message), true);
  } finally {
    busy($("abortBtn"), false);
    await refreshJobs();
    renderList();
  }
}

const ICONS = {
  photo: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
  video: '<path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
  audio: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  text: '<path d="M17 6.1H3"/><path d="M21 12.1H3"/><path d="M15.1 18H3"/>',
  poll: '<path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/>',
  doc: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
};

function svgIcon(paths, size = 15) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------------------------------------------------------- boot
async function boot() {
  wireEvents();   // attach all listeners first so the UI (modal, tabs) is
                  // responsive even while /api/me is still in flight
  try {
    state.me = await api("/api/me");
  } catch (_) {
    state.me = { logged_in: false };
  }
  $("bootSkeleton").classList.add("hidden");
  renderEnterHint();
  state.me.logged_in ? showApp() : showLogin();
  if (state.me.logged_in) {
    api("/api/visit", { method: "POST" }).catch(() => {});   // dashboard visit stat
  }
}

function showLogin() {
  $("loginSection").classList.remove("hidden");
  $("appSection").classList.add("hidden");
  $("userArea").classList.add("hidden");
  stopTicker();
  renderLoginNotice();
  // desktop starts with the cursor in the number field; a phone keyboard
  // popping up on load would get in the way
  const fine = !!(window.matchMedia && window.matchMedia("(pointer: fine)").matches);
  if (fine) {
    setTimeout(() => {
      const p = $("phoneInput");
      if (p && p.offsetParent) { try { p.focus({ preventScroll: true }); } catch (_) {} }
    }, 80);
  }
}

async function showApp() {
  $("loginSection").classList.add("hidden");
  $("appSection").classList.remove("hidden");
  $("userArea").classList.remove("hidden");
  renderAdminTab();
  activateAppTab(state.me && state.me.is_admin ? "admin" : "save");
  // history keeps a skeleton visible until the first /api/jobs response lands
  $("historySkeleton").classList.remove("hidden");
  $("jobList").classList.add("hidden");
  $("emptyJobs").classList.add("hidden");
  await refreshMe();
  renderAdminTab();
  if (state.me && state.me.must_change_pin) openPinModal(true);
  await refreshJobs();
  renderList();
  const active = state.jobs.find((j) => j.status === "running" || j.status === "queued");
  if (active) {
    state.selected = active.id;
    renderJob(active);
    startTicker();
  } else if (state.jobs.length) {
    const sel = state.jobs.find((j) => j.id === state.selected) || state.jobs[0];
    state.selected = sel.id;
    renderJob(sel);
    renderList();
  }
}

async function refreshMe() {
  try {
    const me = await api("/api/me");
    if (!me.logged_in) { showLogin(); return; }
    state.me = me;
    $("userName").textContent = me.name || `User ${me.uid}`;
    $("userAvatar").textContent = (me.name || "?").trim().charAt(0) || "?";
    renderQuota();
    renderAdminTab();
  } catch (_) { /* transient */ }
}

function renderQuota() {
  const me = state.me || {};
  const pill = $("quota");
  if (me.remaining !== undefined) {
    let label = t("quota.left", { n: me.remaining, m: me.daily_limit });
    if (me.cooldown_remaining > 0) {
      const m = Math.floor(me.cooldown_remaining / 60);
      const s = me.cooldown_remaining % 60;
      label += ` · ` + t("quota.ready", { t: `${m ? m + "m " : ""}${s}s` });
    }
    pill.textContent = label;
    pill.classList.toggle("warn", me.remaining <= 1);
  }

  const sub = $("forceSubBanner");
  if (me.force_sub) {
    const ch = escapeHtml(me.force_sub);
    sub.classList.remove("hidden");
    sub.innerHTML = t("forcesub", {
      channel: `<a href="https://t.me/${ch}" target="_blank" rel="noopener">@${ch}</a>`,
    });
  } else {
    sub.classList.add("hidden");
  }

  const banned = !!me.banned;
  $("banBanner").classList.toggle("hidden", !banned);
  const off = me.saving_enabled === false;
  $("saveOffBanner").classList.toggle("hidden", !(off && !banned));
  updateSaveButton();

  // admin notice banner (visible on the login page and inside the app)
  paintNotice($("noticeBanner"), me.notice, me.notice_color);

  updateLinkHints();
}

// paint one notice banner with its colour
function paintNotice(el, text, color) {
  const msg = String(text || "").trim();
  el.textContent = msg;
  el.className = `alert${msg ? ` alert-${color || "blue"}` : ""}`;
  el.classList.toggle("hidden", !msg);
}

// the login page shows its own notice, meant for visitors who are not signed in
function renderLoginNotice() {
  const me = state.me || {};
  paintNotice($("noticeBanner"), me.notice_login, me.notice_login_color);
}

function setNoticeColor(target, color) {
  document.querySelectorAll(`.swatch[data-target="${target}"]`).forEach((b) => {
    b.classList.toggle("is-active", b.dataset.color === color);
  });
}

function activeNoticeColor(target) {
  const el = document.querySelector(`.swatch[data-target="${target}"].is-active`);
  return el ? el.dataset.color : "blue";
}

// batch / multi hints under the link field
function updateLinkHints() {
  const me = state.me || {};
  const bh = $("batchHint");
  if (bh) {
    const max = me.bulk_max_files || 50;
    const left = me.bulk_remaining;
    let text = t("batch.max", { n: max });
    if (left !== undefined && left !== null) {
      text += " · " + (left > 0 ? t("batch.remaining", { n: left }) : t("batch.no_left"));
    }
    bh.textContent = text;
    bh.classList.toggle("error", left === 0);
  }
  const mh = $("multiHint");
  if (mh) {
    const input = $("linkInput");
    const lines = input
      ? input.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
      : [];
    const max = me.multi_max_links || 5;
    if (!state.batchOn && lines.length > 1) {
      mh.textContent = lines.length > max
        ? t("multi.err_max", { n: max })
        : t("multi.count", { n: lines.length });
      mh.classList.toggle("error", lines.length > max);
      mh.classList.remove("hidden");
    } else {
      mh.classList.add("hidden");
    }
  }
}

// the link field hint: touch devices have no Ctrl+Enter, so keep it simple there
function renderEnterHint() {
  const el = $("enterHint");
  if (!el) return;
  const coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  el.textContent = t(coarse ? "link.enter_hint_mobile" : "link.enter_hint");
}

// grow the link field to fit pasted multi-line content
function autoGrow() {
  const el = $("linkInput");
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight + 2, 220) + "px";
}

// re-render everything after a language change
function rerender() {
  renderQuota();
  renderEnterHint();
  renderList();
  const sel = state.jobs.find((j) => j.id === state.selected);
  if (sel) renderJob(sel);
  if ($("secModal") && !$("secModal").classList.contains("hidden")) fillSecurityNotice();
  if (state.me && state.me.is_admin && !$("adminPanel").classList.contains("hidden")) {
    refreshAdmin();
  }
}

// the Save button mirrors the rules the server enforces: banned, saving off,
// or one of this user's jobs still running
function updateSaveButton() {
  const me = state.me || {};
  const busyJob = (state.jobs || []).some(
    (j) => j.status === "running" || j.status === "queued");
  const el = $("fetchBtn");
  if (el) el.disabled = !!me.banned || me.saving_enabled === false || busyJob;
}

// ---------------------------------------------------------------- login
function wireEvents() {
  document.querySelectorAll(".app-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => activateAppTab(tab.dataset.utab));
  });

  $("sendCodeBtn").addEventListener("click", sendCode);
  $("phoneInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendCode(); });

  $("verifyBtn").addEventListener("click", verifyCode);
  $("codeInput").addEventListener("keydown", (e) => { if (e.key === "Enter") verifyCode(); });
  $("codeInput").addEventListener("input", (e) => {
    const digits = e.target.value.replace(/\D+/g, "").slice(0, 8);
    if (digits !== e.target.value) e.target.value = digits;
    if (digits.length === 5 && !state.codeBusy) verifyCode();
  });

  $("restartBtn").addEventListener("click", resetPhoneLogin);

  $("verifyPwdBtn").addEventListener("click", verifyPassword);
  $("passwordInput").addEventListener("keydown", (e) => { if (e.key === "Enter") verifyPassword(); });

  $("backToCodeBtn").addEventListener("click", backToCode);

  $("logoutBtn").addEventListener("click", openLogoutModal);
  $("logoutCancel").addEventListener("click", closeLogoutModal);
  $("logoutConfirm").addEventListener("click", () => { closeLogoutModal(); logout(); });
  $("logoutModal").addEventListener("click", (e) => { if (e.target === $("logoutModal")) closeLogoutModal(); });
  $("secOk").addEventListener("click", () => { closeSecurityNotice(); focusLinkField(); });
  $("secModal").addEventListener("click", (e) => {
    if (e.target === $("secModal")) { closeSecurityNotice(); focusLinkField(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closeLogoutModal();
    closePinModal();
    closeSecurityNotice();
  });

  $("fetchBtn").addEventListener("click", doFetch);
  $("abortBtn").addEventListener("click", abortJob);
  $("linkInput").addEventListener("input", () => { autoGrow(); updateLinkHints(); });
  $("linkInput").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    // Enter always starts a new line, so several links can be typed or pasted
    // one per line; Ctrl/Cmd + Enter sends the save instead.
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      doFetch();
    }
  });
  $("batchToggle").addEventListener("change", () => {
    state.batchOn = $("batchToggle").checked;
    $("batchBox").classList.toggle("hidden", !state.batchOn);
    updateLinkHints();
    if (state.batchOn) $("batchCount").focus();
  });

  // admin panel
  document.querySelectorAll(".admin-subtabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => activateAdminSub(tab.dataset.atab));
  });
  document.querySelectorAll(".swatch").forEach((btn) => {
    btn.addEventListener("click", () => setNoticeColor(btn.dataset.target, btn.dataset.color));
  });
  $("adminRefreshBtn").addEventListener("click", refreshAdmin);
  $("settingsSaveBtn").addEventListener("click", saveAdminSettings);
  $("changePinBtn").addEventListener("click", () => openPinModal(false));
  $("pinSave").addEventListener("click", savePin);
  $("pinCancel").addEventListener("click", () => closePinModal());
  $("pinModal").addEventListener("click", (e) => { if (e.target === $("pinModal")) closePinModal(); });
  ["pinCurrent", "pinNew", "pinConfirm"].forEach((id) => {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") savePin(); });
  });
}

// switch the app view between the Save, History and Admin tabs
function activateAppTab(name) {
  if (name === "admin" && !(state.me && state.me.is_admin)) name = "save";
  document.querySelectorAll(".app-tabs .tab").forEach((t2) => {
    t2.classList.toggle("active", t2.dataset.utab === name);
  });
  $("savePanel").classList.toggle("hidden", name !== "save");
  $("historyPanel").classList.toggle("hidden", name !== "history");
  $("adminPanel").classList.toggle("hidden", name !== "admin");
  if (name === "admin") {
    activateAdminSub("overview");
    refreshAdmin();
  }
}

// admin panel sub-navigation: Overview / Dashboard settings / Users
function activateAdminSub(name) {
  document.querySelectorAll(".admin-subtabs .tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.atab === name);
  });
  $("adminOverview").classList.toggle("hidden", name !== "overview");
  $("adminSettings").classList.toggle("hidden", name !== "settings");
  $("adminUsers").classList.toggle("hidden", name !== "users");
}

// wired immediately (not inside wireEvents) so the switcher responds even
// before boot() finishes its initial /api/me request
function initLangSwitch() {
  const sel = $("langSelect");
  sel.value = getLang();
  sel.addEventListener("change", (e) => {
    setLang(e.target.value);
    rerender();
  });
}

// theme toggle: the colours cross-fade via the global transition in style.css
function initTheme() {
  $("themeBtn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("tg_theme", next); } catch (_) { /* private mode */ }
  });
  // follow the OS scheme until the user makes an explicit choice
  const mq = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
  if (mq && mq.addEventListener) {
    mq.addEventListener("change", (e) => {
      let stored = null;
      try { stored = localStorage.getItem("tg_theme"); } catch (_) {}
      if (stored !== "dark" && stored !== "light") {
        document.documentElement.dataset.theme = e.matches ? "dark" : "light";
      }
    });
  }
}

function busy(btn, on) {
  btn.disabled = on;
  btn.classList.toggle("is-loading", on);
  if (on && !btn.querySelector(".spinner")) {
    const s = document.createElement("span");
    s.className = "spinner";
    btn.prepend(s);
  } else if (!on) {
    const s = btn.querySelector(".spinner");
    if (s) s.remove();
  }
}

async function sendCode() {
  const phone = $("phoneInput").value.trim();
  if (!phone) { toast(t("toast.enter_phone"), true); return; }
  busy($("sendCodeBtn"), true);
  $("loginHint").textContent = t("toast.sending");
  $("loginHint").classList.remove("hidden");
  try {
    const r = await api("/api/login/start", { method: "POST", body: { phone } });
    if (r.admin) {
      // digit-only password in the phone box -> straight into the admin session
      await onLoggedIn(r);
      return;
    }
    state.handle = r.handle;
    state.via = r.via || "Telegram";
    state.phone = phone;                 // shown with the "code sent" hint
    $("phoneStep").classList.add("hidden");  // OTP card replaces the phone form
    showStep($("codeStep"));
    $("passwordStep").classList.add("hidden");
    $("loginHint").textContent = t("sent_code", { via: state.via, phone });
    $("loginHint").classList.remove("hidden");
    $("codeInput").focus();
  } catch (e) {
    $("loginHint").classList.add("hidden");
    toast(e.message, true);
  } finally {
    busy($("sendCodeBtn"), false);
  }
}

async function verifyCode() {
  const code = $("codeInput").value.trim();
  if (!code) { toast(t("toast.enter_code"), true); return; }
  if (state.codeBusy) return;
  state.codeBusy = true;
  busy($("verifyBtn"), true);
  try {
    const r = await api("/api/login/code", { method: "POST", body: { handle: state.handle, code } });
    if (r.need_password) {
      // 2FA: the OTP card is replaced by the password card (same slot)
      $("codeStep").classList.add("hidden");
      showStep($("passwordStep"));
      $("loginHint").textContent = t("two_step");
      $("loginHint").classList.remove("hidden");
      $("passwordInput").focus();
      return;
    }
    await onLoggedIn(r);
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.codeBusy = false;
    busy($("verifyBtn"), false);
  }
}

async function verifyPassword() {
  const password = $("passwordInput").value;
  if (!password) { toast(t("toast.enter_password"), true); return; }
  busy($("verifyPwdBtn"), true);
  try {
    const r = await api("/api/login/password", { method: "POST", body: { handle: state.handle, password } });
    await onLoggedIn(r);
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy($("verifyPwdBtn"), false);
  }
}

async function onLoggedIn(r) {
  resetPhoneLogin();
  toast(t("logged_as", { name: r.name }));
  state.me = {
    logged_in: true,
    uid: r.uid,
    name: r.name,
    must_change_pin: !!r.must_change_pin,
  };
  await showApp();
  if (!r.admin) openSecurityNotice();   // the admin has no Telegram session
}

function resetPhoneLogin() {
  state.handle = null;
  state.via = "";
  state.phone = "";
  $("phoneStep").classList.remove("hidden");
  $("codeStep").classList.add("hidden");
  $("passwordStep").classList.add("hidden");
  $("codeInput").value = "";
  $("passwordInput").value = "";
  $("loginHint").textContent = "";
  $("loginHint").classList.add("hidden");
}

// from the 2FA card back to the OTP card (code kept, password cleared)
function backToCode() {
  $("passwordStep").classList.add("hidden");
  $("passwordInput").value = "";
  showStep($("codeStep"));
  if (state.via) {
    $("loginHint").textContent = t("sent_code", { via: state.via, phone: state.phone });
    $("loginHint").classList.remove("hidden");
  }
  $("codeInput").focus();
}

// ---------------------------------------------------------------- logout modal
let modalCloseTimer = null;
function openLogoutModal() {
  clearTimeout(modalCloseTimer);
  $("logoutModal").classList.remove("closing", "hidden");
}
function closeLogoutModal() {
  const m = $("logoutModal");
  if (m.classList.contains("hidden") || m.classList.contains("closing")) return;
  m.classList.add("closing");
  modalCloseTimer = setTimeout(() => {
    m.classList.add("hidden");
    m.classList.remove("closing");
  }, 160);
}

async function logout() {
  try { await api("/api/logout", { method: "POST" }); } catch (_) { /* ignore */ }
  state.me = { logged_in: false };
  state.jobs = [];
  state.selected = null;
  renderList();
  $("jobCard").classList.add("hidden");
  showLogin();
}

// ------------------------------------------- new-login notice ("was it you?")
let secCloseTimer = null;

// the replica carries the real moment, formatted the way the reader expects
function securityWhenLabel() {
  const d = new Date();
  try {
    return d.toLocaleString(undefined, {
      day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch (_) {
    return d.toLocaleString();
  }
}

function fillSecurityNotice() {
  const el = $("secWhen");
  if (el) el.textContent = securityWhenLabel();
}

function openSecurityNotice() {
  clearTimeout(secCloseTimer);
  const m = $("secModal");
  if (!m) return;
  fillSecurityNotice();
  if (m.classList.contains("hidden")) {
    m.classList.remove("closing", "hidden");
    setTimeout(() => { try { $("secOk").focus({ preventScroll: true }); } catch (_) {} }, 60);
  }
}

function closeSecurityNotice() {
  const m = $("secModal");
  if (!m || m.classList.contains("hidden") || m.classList.contains("closing")) return;
  m.classList.add("closing");
  secCloseTimer = setTimeout(() => {
    m.classList.add("hidden");
    m.classList.remove("closing");
  }, 160);
}

// after the prompt, the link field is the next thing to touch (desktop only:
// focusing it on a phone would throw up the keyboard for no reason)
function focusLinkField() {
  const fine = !!(window.matchMedia && window.matchMedia("(pointer: fine)").matches);
  if (!fine) return;
  const el = $("linkInput");
  if (el && !$("savePanel").classList.contains("hidden")) {
    try { el.focus({ preventScroll: true }); } catch (_) { /* noop */ }
  }
}

// ---------------------------------------------------------------- admin panel
function renderAdminTab() {
  const admin = !!(state.me && state.me.is_admin);
  // admins land straight on the dashboard: no Save/History tabs, no top bar
  $("saveTab").classList.toggle("hidden", admin);
  $("historyTab").classList.toggle("hidden", admin);
  $("appTabs").classList.toggle("hidden", admin);
  if (admin && $("adminPanel").classList.contains("hidden")) activateAppTab("admin");
  if (!admin && !$("adminPanel").classList.contains("hidden")) activateAppTab("save");
}

async function refreshAdmin() {
  if (!(state.me && state.me.is_admin)) return;
  $("adminSkeleton").classList.remove("hidden");
  $("adminBody").classList.add("hidden");
  try {
    const data = await api("/api/admin/stats");
    renderAdmin(data);
  } catch (e) {
    $("adminSkeleton").classList.add("hidden");
    toast(e.message, true);
    if (e.status === 401) showLogin();
  }
}

function renderAdmin(data) {
  const totals = data.totals || {};
  const set = (id, v) => { const el = $(id); if (el) el.textContent = String(v == null ? 0 : v); };
  set("statUsers", totals.users);
  set("statLoggedIn", totals.logged_in);
  set("statVisited", totals.visited);
  set("statReturning", totals.returning);
  set("statBanned", totals.banned);
  set("statFiles", totals.files_saved);
  set("statJobs", totals.jobs_total);
  set("statSavesToday", totals.saves_today);
  set("statVisitsToday", totals.visits_today);
  set("statActive", data.active_jobs);

  renderAreaChart($("chartFiles"), data.daily || [], "files");
  renderBarChart($("chartVisits"), data.daily || [], "visits");

  // settings form mirrors the state from /api/me
  $("noticeInput").value = (state.me && state.me.notice) || "";
  $("noticeLoginInput").value = (state.me && state.me.notice_login) || "";
  setNoticeColor("notice", (state.me && state.me.notice_color) || "blue");
  setNoticeColor("notice-login", (state.me && state.me.notice_login_color) || "blue");
  $("savingToggle").checked = !state.me || state.me.saving_enabled !== false;

  renderUsers(data.users || []);
  $("adminSkeleton").classList.add("hidden");
  $("adminBody").classList.remove("hidden");
}

function chartEmpty(el) {
  el.innerHTML = "";
  const p = document.createElement("p");
  p.className = "empty muted small";
  p.textContent = t("admin.no_data");
  el.appendChild(p);
}

// simple hand-rolled SVG area chart (no external chart library)
function renderAreaChart(el, daily, key) {
  const vals = daily.map((d) => Number(d[key]) || 0);
  if (daily.length < 2 || !vals.some((v) => v > 0)) { chartEmpty(el); return; }

  const W = 640, H = 170, padL = 38, padR = 10, padT = 12, padB = 24;
  const max = Math.max(1, ...vals);
  const n = vals.length;
  const x = (i) => padL + (i * (W - padL - padR)) / Math.max(1, n - 1);
  const y = (v) => padT + (1 - v / max) * (H - padT - padB);
  const line = vals.map((v, i) =>
    `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(n - 1).toFixed(1)} ${H - padB} L${x(0).toFixed(1)} ${H - padB} Z`;

  const grid = [max, Math.round(max / 2), 0].map((v) =>
    `<line class="chart-grid" x1="${padL}" x2="${W - padR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>` +
    `<text class="chart-lbl" x="${padL - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`
  ).join("");
  const mid = Math.floor(n / 2);
  const xl = [
    `<text class="chart-lbl" x="${padL}" y="${H - 7}">${daily[0].day.slice(5)}</text>`,
    `<text class="chart-lbl" x="${x(mid).toFixed(1)}" y="${H - 7}" text-anchor="middle">${daily[mid].day.slice(5)}</text>`,
    `<text class="chart-lbl" x="${W - padR}" y="${H - 7}" text-anchor="end">${daily[n - 1].day.slice(5)}</text>`,
  ].join("");

  el.innerHTML = "";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.innerHTML = `<path class="chart-area" d="${area}"/><path class="chart-line" d="${line}"/>${grid}${xl}`;
  el.appendChild(svg);
}

// CSS bar chart (theme- and RTL-friendly)
function renderBarChart(el, daily, key) {
  const vals = daily.map((d) => Number(d[key]) || 0);
  if (daily.length < 2 || !vals.some((v) => v > 0)) { chartEmpty(el); return; }
  const max = Math.max(1, ...vals);

  el.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "bars";
  daily.forEach((d, i) => {
    const b = document.createElement("div");
    b.className = "bar" + (vals[i] ? "" : " bar-empty");
    b.style.height = `${vals[i] ? Math.max(5, (vals[i] / max) * 100) : 2}%`;
    b.title = `${d.day}: ${vals[i]}`;
    wrap.appendChild(b);
  });
  const axis = document.createElement("div");
  axis.className = "chart-axis";
  const mid = Math.floor(daily.length / 2);
  axis.innerHTML = [daily[0], daily[mid], daily[daily.length - 1]]
    .map((d) => `<span>${escapeHtml(d.day.slice(5))}</span>`).join("");
  el.append(wrap, axis);
}

function renderUsers(users) {
  const ul = $("userList");
  ul.innerHTML = "";
  const pill = $("adminUserCount");
  pill.classList.toggle("hidden", users.length === 0);
  if (users.length) pill.textContent = t("admin.user_count", { n: users.length });
  $("adminNoUsers").classList.toggle("hidden", users.length > 0);

  users.forEach((u, i) => {
    const li = document.createElement("li");
    li.className = "user-row";
    li.style.animationDelay = `${Math.min(i, 10) * 40}ms`;

    const info = document.createElement("div");
    info.className = "user-info";
    const name = document.createElement("div");
    name.className = "user-name";
    name.textContent = u.name || `User ${u.uid}`;
    name.title = String(u.uid);
    const meta = document.createElement("div");
    meta.className = "user-meta";
    const parts = [];
    if (u.phone) parts.push(escapeHtml(u.phone));
    if (u.ip) parts.push(escapeHtml(u.ip));
    parts.push(t("admin.meta_visits", { n: u.visits || 0 }));
    if (u.saves_today) parts.push(t("admin.meta_saves", { n: u.saves_today }));
    parts.push(u.last_seen ? timeAgo(u.last_seen) : t("admin.never"));
    if (u.banned) parts.push(t("admin.stat_banned"));
    meta.textContent = parts.join("  ·  ");
    info.append(name, meta);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `btn btn-sm ${u.banned ? "btn-outline" : "btn-danger"}`;
    btn.textContent = u.banned ? t("admin.unban") : t("admin.ban");
    btn.addEventListener("click", () => toggleBan(u.uid, !u.banned, btn));

    li.append(info, btn);
    ul.appendChild(li);
  });
}

async function toggleBan(uid, banned, btn) {
  busy(btn, true);
  try {
    await api("/api/admin/ban", { method: "POST", body: { uid, banned } });
    toast(t(banned ? "admin.toast_banned" : "admin.toast_unbanned"));
    await refreshAdmin();
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy(btn, false);
  }
}

async function saveAdminSettings() {
  busy($("settingsSaveBtn"), true);
  try {
    const r = await api("/api/admin/settings", {
      method: "POST",
      body: {
        notice: $("noticeInput").value.trim(),
        notice_login: $("noticeLoginInput").value.trim(),
        notice_color: activeNoticeColor("notice"),
        notice_login_color: activeNoticeColor("notice-login"),
        saving_enabled: $("savingToggle").checked,
      },
    });
    if (state.me) {
      state.me.notice = r.notice;
      state.me.notice_color = r.notice_color;
      state.me.notice_login = r.notice_login;
      state.me.notice_login_color = r.notice_login_color;
      state.me.saving_enabled = r.saving_enabled;
    }
    toast(t("admin.toast_settings"));
    renderQuota();
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy($("settingsSaveBtn"), false);
  }
}

// ---------------------------------------------------------------- admin password
let pinForce = false;
let pinCloseTimer = null;

function openPinModal(force) {
  pinForce = !!force;
  clearTimeout(pinCloseTimer);
  $("pinModal").classList.remove("closing", "hidden");
  $("pinCancel").classList.toggle("hidden", pinForce);
  $("pinCurrentField").classList.toggle("hidden", pinForce);
  $("pinTitle").textContent = t("pin.title");
  $("pinDesc").textContent = pinForce ? t("pin.desc") : t("pin.desc_edit");
  $("pinError").classList.add("hidden");
  const first = pinForce ? $("pinNew") : $("pinCurrent");
  setTimeout(() => { try { first.focus(); } catch (_) {} }, 60);
}

function closePinModal(allowForced) {
  if (pinForce && allowForced !== true) return;   // first login cannot be skipped
  const m = $("pinModal");
  if (m.classList.contains("hidden") || m.classList.contains("closing")) return;
  m.classList.add("closing");
  pinCloseTimer = setTimeout(() => {
    m.classList.add("hidden");
    m.classList.remove("closing");
  }, 160);
}

async function savePin() {
  const err = $("pinError");
  err.classList.add("hidden");
  const current = $("pinCurrent").value.trim();
  const next = $("pinNew").value.trim();
  const confirm = $("pinConfirm").value.trim();
  if (!next || (!pinForce && !current)) {
    err.textContent = t("pin.err_required");
    err.classList.remove("hidden");
    return;
  }
  if (next !== confirm) {
    err.textContent = t("pin.err_mismatch");
    err.classList.remove("hidden");
    return;
  }
  busy($("pinSave"), true);
  try {
    await api("/api/admin/pin", {
      method: "POST",
      body: { new_pin: next, current_pin: pinForce ? null : current },
    });
    $("pinCurrent").value = "";
    $("pinNew").value = "";
    $("pinConfirm").value = "";
    toast(t("pin.changed"));
    closePinModal(true);
    pinForce = false;
    if (state.me) state.me.must_change_pin = false;
    await refreshMe();
  } catch (e) {
    err.textContent = translateServer(e.message);
    err.classList.remove("hidden");
  } finally {
    busy($("pinSave"), false);
  }
}

// ---------------------------------------------------------------- fetching
function doFetch() {
  const raw = $("linkInput").value;
  const lines = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const me = state.me || {};

  let mode = "single";
  let body;
  if (state.batchOn) {
    if (!lines.length) { toast(t("toast.enter_link"), true); return; }
    const n = parseInt($("batchCount").value, 10);
    if (!n || n < 1) {
      toast(t("batch.err_count"), true);
      $("batchCount").focus();
      return;
    }
    const max = me.bulk_max_files || 50;
    if (n > max) { toast(t("batch.err_max", { n: max }), true); return; }
    mode = "batch";
    body = { link: lines[0], count: n };
  } else if (lines.length > 1) {
    const max = me.multi_max_links || 5;
    if (lines.length > max) { toast(t("multi.err_max", { n: max }), true); return; }
    mode = "multi";
    body = { link: lines.join("\n") };
  } else if (!lines.length) {
    toast(t("toast.enter_link"), true);
    return;
  } else {
    body = { link: lines[0] };
  }

  busy($("fetchBtn"), true);
  api("/api/fetch", { method: "POST", body: { ...body, mode } })
    .then(async (r) => {
      state.selected = r.job.id;
      renderJob(r.job);
      activateAppTab("save");
      await refreshJobs();
      renderList();
      startTicker();
    })
    .catch((e) => {
      toast(e.message, true);
      if (e.status === 401) showLogin();
    })
    .finally(() => busy($("fetchBtn"), false));
}

async function refreshJobs() {
  try {
    const r = await api("/api/jobs");
    state.jobs = r.jobs || [];
  } catch (_) { /* transient */ }
}

function startTicker() {
  if (state.timer) return;
  state.timer = setInterval(tick, 1000);
}

function stopTicker() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

async function tick() {
  try {
    await refreshJobs();
    renderList();
    updateSaveButton();
    const sel = state.jobs.find((j) => j.id === state.selected);
    if (sel) renderJob(sel);

    const key = state.jobs.map((j) => `${j.id}:${j.status}`).join(",");
    if (key !== state.lastSeen) {
      const hadActive = state.lastSeen.includes(":running") || state.lastSeen.includes(":queued");
      state.lastSeen = key;
      await refreshMe();
      if (hadActive && !key.includes(":running") && !key.includes(":queued")) {
        const done = state.jobs.find((j) => j.id === state.selected && j.status === "done");
        if (done) toast(t("saved_toast"));
      }
    }

    const anyActive = state.jobs.some((j) => j.status === "running" || j.status === "queued");
    if (!anyActive) stopTicker();
  } catch (_) { /* ignore transient errors */ }
}

// ---------------------------------------------------------------- rendering
function renderJob(job) {
  $("jobCard").classList.remove("hidden");
  $("jobLink").textContent = job.link;
  $("jobLink").href = job.link.startsWith("http") ? job.link : `https://${job.link}`;

  const badge = $("jobStatus");
  badge.textContent = badgeLabel(job.status);
  badge.className = `badge ${job.status}`;

  const kind = $("jobKind");
  if (job.kind && job.kind !== "single") {
    kind.textContent = t(`kind.${job.kind}`);
    kind.classList.remove("hidden");
  } else {
    kind.classList.add("hidden");
  }

  const running = job.status === "running" || job.status === "queued";
  $("abortBtn").classList.toggle("hidden", !running);
  const pct = job.percent || 0;
  $("jobPct").textContent = job.status === "done" ? t("saved_badge") : `${Math.round(pct)}%`;
  $("jobBar").style.width = `${pct}%`;
  $("jobBar").classList.toggle("running", running);
  $("jobBar").classList.toggle("indeterminate", running && pct === 0);

  let step = translateServer(job.step || "");
  if (running && job.total > 0 && job.speed > 0) {
    step += `  ·  ${humanSize(job.downloaded)} / ${humanSize(job.total)}  ·  ${humanSpeed(job.speed)}`;
  }
  $("jobStep").textContent = step;

  const err = $("jobError");
  if (job.error) { err.textContent = translateServer(job.error); err.classList.remove("hidden"); }
  else err.classList.add("hidden");

  // result block: success message + saved items + "Open in Telegram"
  const showResult = job.status === "done" && !job.error && (job.message || job.open_url);
  const result = $("jobResult");
  result.classList.toggle("hidden", !showResult);
  if (showResult) {
    $("jobMessage").textContent = translateServer(job.message || t("srv.saved"));
    renderItems(job);
    const open = $("jobOpen");
    if (job.open_url) {
      open.href = job.open_url;
      open.classList.remove("hidden");
    } else {
      open.classList.add("hidden");
    }
  }

  const text = $("jobText");
  if (job.text && job.status === "done" && !job.error) {
    text.textContent = job.text;
    text.classList.remove("hidden");
  } else text.classList.add("hidden");

  updateSaveButton();
}

function renderItems(job) {
  const box = $("jobItems");
  box.innerHTML = "";
  if (!job.items || !job.items.length) return;

  job.items.forEach((item, i) => {
    const li = document.createElement("li");
    li.style.animationDelay = `${i * 70}ms`;

    const icon = document.createElement("span");
    icon.className = "item-icon";
    icon.innerHTML = svgIcon(ICONS[item.kind] || ICONS.doc, 14);

    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = item.name;
    name.title = item.name;  // full name available on hover / long-press

    const size = document.createElement("span");
    size.className = "item-size";
    size.textContent = item.size ? humanSize(item.size) : "";

    li.append(icon, name, size);
    box.appendChild(li);
  });
}

function renderList() {
  const list = $("jobList");
  const empty = $("emptyJobs");
  const jobs = state.jobs || [];
  $("historySkeleton").classList.add("hidden");
  list.innerHTML = "";
  list.classList.toggle("hidden", jobs.length === 0);
  empty.classList.toggle("hidden", jobs.length > 0);

  jobs.forEach((job, i) => {
    const li = document.createElement("li");
    li.className = "job-item" + (job.id === state.selected ? " selected" : "");
    li.style.animationDelay = `${Math.min(i, 8) * 45}ms`;

    const left = document.createElement("div");
    left.className = "job-item-left";
    const link = document.createElement("div");
    link.className = "job-item-link";
    link.textContent = job.link;
    const time = document.createElement("div");
    time.className = "job-item-time";
    const count = (job.items && job.items.length) || 0;
    let caption = timeAgo(job.created);
    if (count) {
      caption += `  ·  ` + (count === 1 ? t("items_one", { n: count }) : t("items_many", { n: count }));
    }
    time.textContent = caption;
    left.append(link, time);

    const badge = document.createElement("span");
    badge.className = `badge ${job.status}`;
    badge.textContent = badgeLabel(job.status);

    li.append(left);
    if (job.kind && job.kind !== "single") {
      const kind = document.createElement("span");
      kind.className = "pill kind-pill";
      kind.textContent = t(`kind.${job.kind}`);
      li.appendChild(kind);
    }
    li.appendChild(badge);
    li.addEventListener("click", () => {
      state.selected = job.id;
      renderJob(job);
      renderList();
      activateAppTab("save");   // the job detail lives on the Save tab
      if (job.status === "running" || job.status === "queued") startTicker();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    list.appendChild(li);
  });
}

initLangSwitch();
initTheme();
boot();
