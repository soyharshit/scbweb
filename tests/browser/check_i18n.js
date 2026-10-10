/* Consistency checks for i18n.js + index.html + app.js */
const fs = require("fs");
const vm = require("vm");

const DIR = require("path").join(__dirname, "..", "..", "app", "static");
const failures = [];
const ok = (name, cond, extra) => {
  console.log(`${cond ? "PASS" : "FAIL"} ${name}${!cond && extra ? "  " + extra : ""}`);
  if (!cond) failures.push(name);
};

// --- load i18n.js in a sandbox (it self-applies on load) ---
const ctx = {
  console,
  localStorage: { getItem: () => null, setItem: () => {} },
  navigator: { language: "en-US" },
  document: { documentElement: { lang: "", dir: "" }, querySelectorAll: () => [] },
};
vm.createContext(ctx);
ctx.__export = (o) => Object.assign(ctx, o);
vm.runInContext(
  fs.readFileSync(`${DIR}/i18n.js`, "utf8") +
    "\n;__export({ I18N, I18N_LANGS, SERVER_EXACT, SERVER_PATTERNS, translateServer, setLang });",
  ctx,
  { filename: "i18n.js" }
);

const { I18N, I18N_LANGS, SERVER_EXACT, SERVER_PATTERNS, translateServer, setLang } = ctx;
const html = fs.readFileSync(`${DIR}/index.html`, "utf8");
const appjs = fs.readFileSync(`${DIR}/app.js`, "utf8");

// --- 1. languages ---
ok("8 languages declared", I18N_LANGS.length === 8 && I18N_LANGS.join() === "en,es,fr,ar,ru,zh,pt,id",
  JSON.stringify(I18N_LANGS));

// --- 2. key parity ---
const enKeys = Object.keys(I18N.en).sort();
ok("en has a healthy key set", enKeys.length >= 95, `${enKeys.length} keys`);
for (const lang of I18N_LANGS) {
  if (!I18N[lang]) { ok(`lang ${lang} present`, false); continue; }
  const keys = Object.keys(I18N[lang]).sort();
  const missing = enKeys.filter((k) => !keys.includes(k));
  const extra = keys.filter((k) => !enKeys.includes(k));
  ok(`key parity [${lang}]`, missing.length === 0 && extra.length === 0,
    `missing=${JSON.stringify(missing)} extra=${JSON.stringify(extra)}`);
}

// --- 3. placeholder parity ---
const ph = (s) => (String(s).match(/\{[a-zA-Z0-9_]+\}/g) || []).sort().join(",");
for (const lang of I18N_LANGS) {
  if (lang === "en" || !I18N[lang]) continue;
  const bad = [];
  for (const k of enKeys) {
    if (!(k in I18N[lang])) continue;
    if (ph(I18N.en[k]) !== ph(I18N[lang][k])) {
      bad.push(`${k}: en=[${ph(I18N.en[k])}] ${lang}=[${ph(I18N[lang][k])}]`);
    }
  }
  ok(`placeholder parity [${lang}]`, bad.length === 0, bad.slice(0, 4).join(" | "));
}

// --- 4. index.html hooks all exist ---
const hooked = [];
for (const m of html.matchAll(/data-i18n(?:-html|-placeholder|-aria|-title)?="([^"]+)"/g)) hooked.push(m[1]);
const htmlMissing = [...new Set(hooked)].filter((k) => !(k in I18N.en));
ok(`index.html i18n hooks (${new Set(hooked).size} found)`, htmlMissing.length === 0,
  JSON.stringify(htmlMissing));

// --- 5. app.js t("...") keys exist ---
const used = [];
for (const m of appjs.matchAll(/\bt\(\s*"([^"]+)"/g)) used.push(m[1]);
const usedMissing = [...new Set(used)].filter((k) => !(k in I18N.en));
ok(`app.js t() keys (${new Set(used).size} found)`, usedMissing.length === 0,
  JSON.stringify(usedMissing));

// dynamic status keys used via badgeLabel()
for (const s of ["queued", "running", "done", "error"]) {
  ok(`status key ${s} in all langs`, I18N_LANGS.every((l) => `status.${s}` in (I18N[l] || {})));
}

// --- 6. server maps reference real keys ---
const mapKeys = Object.values(SERVER_EXACT);
const patKeys = SERVER_PATTERNS.map((p) => p[1]).filter((k) => k !== "srv.__fail");
const badMap = [...mapKeys, ...patKeys].filter((k) => !(k in I18N.en));
ok(`SERVER_EXACT/PATTERNS keys (${mapKeys.length}+${patKeys.length})`, badMap.length === 0,
  JSON.stringify([...new Set(badMap)]));

// every server key is covered by every language
const serverKeys = [...new Set([...mapKeys, ...patKeys])];
const serverMissing = [];
for (const lang of I18N_LANGS) {
  for (const k of serverKeys) if (!(k in (I18N[lang] || {}))) serverMissing.push(`${lang}:${k}`);
}
ok("server keys translated in every language", serverMissing.length === 0,
  JSON.stringify(serverMissing.slice(0, 6)));

// --- 7. translations actually translated (spot check) ---
for (const lang of I18N_LANGS.filter((l) => l !== "en")) {
  const differs = enKeys.filter((k) => I18N[lang][k] !== I18N.en[k]).length;
  const ratio = differs / enKeys.length;
  ok(`[${lang}] strings differ from English (${Math.round(ratio * 100)}%)`, ratio > 0.9);
}

// --- 8. no em dashes anywhere on the page ---
for (const f of ["index.html", "app.js", "style.css", "i18n.js"]) {
  const src = fs.readFileSync(`${DIR}/${f}`, "utf8");
  const n = (src.match(/—/g) || []).length;
  ok(`no em dash in ${f}`, n === 0, `${n} found`);
}
// translated copy must not contain " -- " either (visible artifact)
for (const lang of I18N_LANGS) {
  const joined = Object.values(I18N[lang]).join("\n");
  ok(`no "--" in [${lang}] strings`, !joined.includes("--"));
}

// --- 9. server strings translate, including stacked job messages ---
const fill = (s, vals) => {
  let o = s;
  vals.forEach((v, i) => {
    o = o.split("{" + (i + 1) + "}").join(String(v));
  });
  return o;
};

setLang("es");
ok(
  "exact server sentence translated",
  translateServer("Saved to your Saved Messages.") === I18N.es["srv.saved"],
  translateServer("Saved to your Saved Messages.")
);
ok(
  "stacked job message translated sentence by sentence",
  translateServer(
    "Saved to your Saved Messages. Split into 3 parts; join them in order to restore the original file."
  ) === `${I18N.es["srv.saved"]} ${fill(I18N.es["srv.split_n"], [3])}`,
  translateServer(
    "Saved to your Saved Messages. Split into 3 parts; join them in order to restore the original file."
  )
);
ok(
  "upload step with parts translated",
  translateServer("Uploading part 2/5 to Saved Messages") ===
    fill(I18N.es["step.uploading_part"], [2, 5]),
  translateServer("Uploading part 2/5 to Saved Messages")
);
ok(
  "retry step translated",
  translateServer("Retrying in 2s...") === fill(I18N.es["step.retry"], [2]),
  translateServer("Retrying in 2s...")
);
ok(
  "minute flood error translated",
  translateServer("Rate limited by Telegram. Try again in about 6 minutes.") ===
    fill(I18N.es["err.flood_m"], [6]),
  translateServer("Rate limited by Telegram. Try again in about 6 minutes.")
);
ok(
  "second flood wording still translated",
  translateServer("Rate limited by Telegram. Try again in 45 seconds.") ===
    fill(I18N.es["err.flood"], [45]),
  translateServer("Rate limited by Telegram. Try again in 45 seconds.")
);
ok(
  "split note among failures keeps both halves",
  (() => {
    const base = "Saved 1/2 messages to your Saved Messages.";
    const note = "Split into 2 parts; join them in order to restore the original file.";
    return translateServer(`${base} ${note}`) ===
      `${translateServer(base)} ${translateServer(note)}`;
  })(),
  translateServer(
    "Saved 1/2 messages to your Saved Messages. Split into 2 parts; join them in order to restore the original file."
  )
);
ok("unknown server string falls through", translateServer("Some odd text.") === "Some odd text.");
ok(
  "days range error translated",
  translateServer("The number of days must be between 1 and 3650.") ===
    fill(I18N.es["err.days"], [3650]),
  translateServer("The number of days must be between 1 and 3650.")
);
setLang("en");
ok("English returns the original text", translateServer("Split into 2 parts; join them in order to restore the original file.")
  === "Split into 2 parts; join them in order to restore the original file.");

console.log(failures.length ? `\n${failures.length} FAILURES` : "\nALL I18N CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
