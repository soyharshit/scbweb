# Tests

Two layers, both runnable from a clean checkout. Nothing here touches the real
Telegram account except where a suite says so.

## Backend — `test_backend.py`

217 checks against the real FastAPI app with a fake Mongo and a fake userbot.
No network, no Telegram, no browser.

```bash
cd webapp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install httpx
.venv/bin/python tests/test_backend.py
```

Covers link parsing, phone/PIN login, quota and cooldown, ban and force-sub,
batch and multi-link jobs, abort, FloodWait countdowns, file splitting over
2 GB, forward/copy/re-upload fallback and the resolve diagnostics.

## Browser — `browser/`

Puppeteer suites drive the page at `http://127.0.0.1:8000/`, plus one static
check that needs no browser at all.

```bash
cd webapp
./run.sh &                     # serve on :8000 first
cd tests/browser
npm install                    # puppeteer, downloads Chromium once
node check_i18n.js             # no browser: i18n.js / index.html / app.js agree
node i18n.test.js              # 8 languages, RTL, OTP, mobile overflow
node layout.test.js            # long filenames across 6 viewports
node login.test.js             # login notice + new-login security prompt
node batch.test.js             # batch toggle, multi-link field, hints
node admin.test.js             # admin PIN, dashboard, users, banners
```

`npm test` runs all six in order.

## Notes

- The suites read the admin PIN from `webapp/.env`, which is git-ignored. On a
  fresh clone, copy `webapp/.env.example` to `webapp/.env` and set one.
- The browser suites assume the server is already running locally. They change
  nothing permanent except the notices and the saving toggle, which they restore.
- `admin.test.js` expects a first admin login so it can see the forced PIN
  change dialog. Flip `admin_pin_changed` to `False` in Mongo before the run if
  it is currently `True`, and set it back afterwards.
