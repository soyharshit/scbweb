# Telegram File Saver — Web Client

A browser-based replacement for the Telegram bot client in this repo.
The old bot code lives in `../main` untouched — this app is standalone and only
reuses the same MongoDB (same users, same sessions, same daily quota).

**How it works**

1. The user logs in from the web page (phone + OTP, 2FA supported, or by pasting
   a Pyrogram session string).
2. The user pastes a Telegram message link (public, private, `t.me/c/...`, invite, album…).
3. The server saves the message to the user's **Saved Messages** using their
   userbot session (forward → copy → download + re-upload fallback for
   protected content — same strategy as the old bot).
4. The page shows progress while it works, then lists what was saved plus an
   **Open in Telegram** button (`tg://openmessage?…`) that jumps straight to it.

Nothing is downloaded to the browser; temp files exist on the server only
while a re-upload is in flight and are deleted right after.

## Quick start

```bash
cd webapp
./run.sh                 # creates .venv, installs deps, serves on :8000
```

Or manually:

```bash
cd webapp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Docker:

```bash
cd webapp
docker build -t tg-file-saver-web .
docker run --env-file .env -p 8000:8000 tg-file-saver-web
```

Open http://localhost:8000

Configuration comes from `webapp/.env` (see `.env.example`) or the repo-root
`.env` — the same file the bot uses, so `API_ID`, `API_HASH`, `MONGODB_URI`,
`FORCESUB` etc. are shared. **`BOT_TOKEN` is not required** for the web client.

> Always run with `--workers 1` (the default in `run.sh`): logged-in sessions
> and running jobs are kept in memory.

## Features

- Simple one-page UI (shadcn-style neutral/grey theme): paste link → watch
  progress → open it in Telegram.
- Phone/OTP login with 2FA, or paste an existing Pyrogram session string.
- Supports public links, private `t.me/c/…` links, `t.me/b/…`, invite links
  (joins the chat), albums/media groups, text messages, polls, stickers,
  voice, video notes. An album is always saved whole, even when the link
  carries `?single` (Telegram adds that when you copy one item of it).
- Saves only to your Telegram **Saved Messages** — no browser download; the
  server keeps nothing after the save (forward → copy → re-upload fallback,
  same as the bot).
- Daily quota (20 by default, shared with the bot), optional cooldown,
  optional force-subscribe check, ban check.
- Recent jobs list with one-click “Open in Telegram”; jobs auto-expire
  (default 1 h).

## HTTP API

| Method | Path                         | Description                                  |
|--------|------------------------------|----------------------------------------------|
| POST   | `/api/login/start`           | `{phone}` → `{handle}` (sends the OTP)       |
| POST   | `/api/login/code`            | `{handle, code}` → login or `need_password`  |
| POST   | `/api/login/password`        | `{handle, password}` (2FA)                   |
| POST   | `/api/login/session`         | `{session}` — paste a session string         |
| POST   | `/api/logout`                | Revokes the cookie + stored session          |
| GET    | `/api/me`                    | Login state, quota, cooldown, force-sub      |
| POST   | `/api/fetch`                 | `{link}` → `{job}` (saves to Saved Messages) |
| GET    | `/api/jobs`                  | Your recent jobs                             |
| GET    | `/api/jobs/{id}`             | Job status/progress + `items` + `open_url`   |
| GET    | `/api/stats`                 | Admin stats (`AUTH_USERS` only)              |

Auth is an `HttpOnly` cookie (`SameSite=lax`) backed by a random token stored
(hashed) in MongoDB.

## Project layout

```
webapp/
├── run.sh                 # one-command launcher (venv + uvicorn)
├── requirements.txt
├── .env.example
├── data/jobs/<job>/       # temp re-upload files (auto-cleaned, git-ignored)
├── tests/
│   ├── test_backend.py    # 217 checks, fake Mongo + fake userbot
│   └── browser/           # puppeteer suites + the static i18n check
└── app/
    ├── config.py          # env settings
    ├── db.py              # Mongo: users, web tokens, quota, stats
    ├── auth.py            # phone/OTP + session-string login flows
    ├── tg.py              # userbot lifecycle, force-sub, join invite
    ├── jobs.py            # link parsing, message resolution, Saved Messages
    ├── main.py            # FastAPI routes + static hosting
    └── static/            # index.html / style.css / app.js (no build step)
```

## Notes

- The Telegram bot in `../main` keeps working exactly as before; both share the
  `users` collection, so someone who logged in with the bot can log in on the
  web (and vice versa) without re-authenticating both places.
- Serve behind HTTPS in production and set `COOKIE_SECURE=true`.
- Files over 2 GB that cannot be forwarded need Telegram Premium on the
  account (Telegram's own upload limit — same as with the bot).
