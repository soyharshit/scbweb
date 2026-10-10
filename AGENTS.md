# AGENTS.md

Single FastAPI app plus a no-build vanilla JS frontend. Serves a browser client
where users log in with phone + OTP and save restricted Telegram files to their
own Saved Messages. `--workers 1` is mandatory: logged-in sessions and running
jobs are held in memory.

## Run and restart

```bash
./run.sh                    # creates .venv, installs requirements.txt, serves :8000
```

`API_ID` and `API_HASH` must be in `.env` at the repository root or
`app/config.py` raises `RuntimeError` at import. `app/config.py` loads `.env`
from the directory above `app/`, then the working directory.

To restart a running server after a Python change:

```bash
pkill -f "[u]vicorn app.main"; sleep 1
cd /root/sc/scbweb && setsid nohup ./run.sh > /tmp/webapp_server.log 2>&1 < /dev/null &
```

`setsid` is required or the process dies when the shell exits. Static file
changes need no restart, only a `?v=` bump in `app/static/index.html`
(currently `?v=15`).

## Verify

There is no linter, typechecker, or CI. This is the full gate, in order:

```bash
.venv/bin/python -m pyflakes app/*.py
.venv/bin/python tests/test_backend.py        # 217 checks, a plain script, not pytest
cd tests/browser && node check_i18n.js         # static only, needs no browser
```

Browser suites need the server running on `:8000`. Run them from
`tests/browser` after `npm install`:

```bash
node i18n.test.js && node layout.test.js && node login.test.js && node batch.test.js
```

`npm test` runs all six including `check_i18n.js`.

### admin.test.js

It asserts the forced first-login PIN dialog, which only appears while
`admin_pin_changed` is false. Flip it, run, restore:

```bash
mongosh --quiet --eval 'db.getSiblingDB("saverestricted").web_settings.updateOne({_id:"config"},{$set:{admin_pin_changed:false}})'
mongosh --quiet --eval 'db.getSiblingDB("saverestricted").web_settings.updateOne({_id:"config"},{$set:{admin_pin_changed:true}})'
```

The suite never submits a matching password pair, so the stored admin PIN is
not modified. Confirm with a snapshot diff of that document if unsure.

## Frontend conventions

- Eight languages live in `app/static/i18n.js`: `en`, `es`, `fr`, `ar` (RTL),
  `ru`, `zh`, `pt`, `id`. A new user-visible string needs a key in **all
  eight** dicts; `check_i18n.js` fails the build otherwise.
- Python returns English error strings. Translation happens in the browser
  through the `SERVER_EXACT` and `SERVER_PATTERNS` tables in `i18n.js`, so a
  new server error needs an entry there too.
- No em dashes and no `--` in any user-visible string.
- Admin signs in by typing a digit-only PIN into the phone number input, not
  through a separate form.

## Things that look like bugs but are not

- `?single` on an album link is deliberately ignored. Telegram stamps it on
  every link you copy for one item of a media group, so honouring it meant an
  album saved as a single file. The whole album is always saved.
- Jobs prefer `forward_messages`, then `copy_message`, then a
  download-and-re-upload fallback. The first two failing is normal for
  protected channels, not an error.

## Gotchas

- `/api/login/start` allows 10 requests per 10 minutes per IP and the table is
  in memory, so a restart clears it. Tests that hit it repeatedly will get 429.
- Puppeteer's `page.click()` can fail on these elements; click through the DOM
  instead.
- `data/jobs/<job>/` holds temp files only while a re-upload is in flight and
  is auto-cleaned. It is git-ignored.
- Mongo is local at `127.0.0.1:27017`, database `saverestricted`. Settings live
  in `web_settings` under the single document `_id: "config"`.

## Git

Branch `stable-BotaCraft`. Two remotes:

| remote | url | notes |
|---|---|---|
| `github` | `soyharshit/scbweb` | the branch upstream; plain `git push` goes here |
| `origin` | `soyharshit/scb` | old repo, retained only for the original bot code |

Never stage `.env`, `*.session`, `*.session-journal`, `.env.bak*`, or
`node_modules/`; all are git-ignored and two of them are live credentials.
