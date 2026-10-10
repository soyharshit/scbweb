# Telegram File Saver

Save restricted Telegram files into your own **Saved Messages**.

Paste a link, watch it save, open it in Telegram. Nothing is downloaded to the
browser: the server does the work with the user's own Telegram account and
keeps no file once the job finishes.

```
https://188.137.182.106:8000
```

## How a file is saved

1. The user signs in with a phone number and an OTP (2FA supported), or by
   pasting a Pyrogram session string. The session is stored hashed in Mongo,
   never written to disk.
2. They paste a message link: public, private `t.me/c/...`, `t.me/b/...`, an
   invite link, or an album.
3. The server saves it with the user's own account, taking the cheapest path
   that works: **forward**, then **copy**, then **download and re-upload** for
   content that forbids both. Files larger than the account's upload limit are
   split into numbered parts.
4. An album is always saved whole, even when the link carries `?single`
   (Telegram adds that when you copy one item of it).

## Features

- Simple one-page UI in a neutral grey theme, in 8 languages with RTL support.
- Phone and OTP login, 2FA, or a pasted session string.
- Live progress, an abort button, and a FloodWait countdown that retries by
  itself.
- Batch saves (keep saving forward until N files) and multi-link saves
  (newline separated).
- History of recent jobs with an "Open in Telegram" button.
- An admin dashboard: stats and charts, per-user quota, ban and unban,
  a saving on/off switch, and two independently coloured notices.
- Jobs never leave temp files on disk and never hang forever on a dead
  connection.

## Running it

```bash
cp .env.example .env      # fill in API_ID and API_HASH at minimum
./run.sh                  # creates .venv, installs deps, serves on :8000
```

Always run a single worker. Sessions and running jobs are held in memory, so
more than one worker would lose them.

`API_ID` and `API_HASH` come from https://my.telegram.org/apps. `BOT_TOKEN` is
not needed: this app is a userbot, not a bot.

## Layout

```
.
├── app/
│   ├── config.py          env settings
│   ├── db.py              Mongo: users, tokens, quota, stats
│   ├── auth.py            phone/OTP and session-string login
│   ├── tg.py              userbot lifecycle, force-sub, joining chats
│   ├── jobs.py            link parsing, resolution, saving to Saved Messages
│   ├── main.py            FastAPI routes and static hosting
│   └── static/            index.html, style.css, app.js, i18n.js, no build step
├── tests/
│   ├── test_backend.py    217 checks with a fake Mongo and a fake userbot
│   └── browser/           puppeteer suites plus a static i18n check
├── data/jobs/<job>/       temp files during a re-upload, git-ignored
├── .env.example           every setting, with comments
├── Dockerfile
└── run.sh
```

## HTTP API

| Method | Path                   | Description                                  |
|--------|------------------------|----------------------------------------------|
| POST   | `/api/login/start`     | `{phone}` sends the OTP and returns a handle |
| POST   | `/api/login/code`      | `{handle, code}`, or asks for a password     |
| POST   | `/api/login/password`  | `{handle, password}` for 2FA                 |
| POST   | `/api/login/session`   | `{session}` to paste a session string        |
| POST   | `/api/logout`          | revokes the cookie and the stored session    |
| GET    | `/api/me`              | login state, quota, cooldown, force-sub      |
| POST   | `/api/fetch`           | `{link}` starts a save and returns the job   |
| GET    | `/api/jobs`            | your recent jobs                             |
| GET    | `/api/jobs/{id}`       | progress, saved items, and an open URL       |
| GET    | `/api/stats`           | admin stats                                   |

Authentication is an `HttpOnly` `SameSite=lax` cookie backed by a random token
stored hashed in Mongo.

## Tests

```bash
.venv/bin/python tests/test_backend.py
cd tests/browser && npm install && node check_i18n.js
```

Details in [`tests/README.md`](tests/README.md).
