# Telegram File Saver

Save restricted Telegram files into your own **Saved Messages**. Two clients,
one database:

| | what it is | run it |
|---|---|---|
| [`webapp/`](webapp/) | browser client: phone + OTP login, live progress, batch and multi-link saves, admin dashboard | `cd webapp && ./run.sh`, then http://localhost:8000 |
| [`main/`](main/) | the original Telegram bot | `python -m main` |

Both talk to the same MongoDB (`DB_NAME`, default `saverestricted`) and share
the `users` collection, so a session created in one client works in the other
without signing in twice. Nothing else is shared: the web client never imports
anything from `main/`.

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
4. Nothing is downloaded to the browser. Temp files exist on the server only
   while a re-upload is in flight, and are deleted immediately after.

## Layout

```
.
├── main/                  the Telegram bot
├── utils_bot.py           small helpers used by main/plugins/start.py
├── pyproject.toml         bot dependencies, for uv
├── requirements.txt       the same dependencies, for plain pip
├── webapp/                the browser client, self-contained
│   ├── app/               FastAPI application
│   ├── static/            HTML, CSS and JS, no build step
│   ├── tests/             backend and browser test suites
│   └── run.sh             creates a venv and serves on :8000
├── .env                   your secrets, git-ignored
└── .env.example           template covering both clients
```

### Do not move `main/`, `utils_bot.py`, or the root packaging files

`main/utils.py` loads its plugins by the relative path
`main/plugins/<name>.py`, and the bot is started with `python -m main` from the
repository root. Those paths are baked into how the bot runs, so the files have
to stay exactly where they are.

## Configuration

Copy `.env.example` to `.env` and fill it in. The web client reads the same
file, so `API_ID`, `API_HASH`, `MONGODB_URI`, `DB_NAME`, `FORCESUB` and
`AUTH_USERS` are shared between the two clients. `BOT_TOKEN` is needed only by
the bot. `webapp/.env.example` lists the web-only settings: port, cookie flags,
daily quota, cooldown, batch limits.

`API_ID` and `API_HASH` come from https://my.telegram.org/apps. They are read
from the environment and are not baked into the source.

## Running the web client

```bash
cd webapp
./run.sh
```

Always run a single worker. Sessions and running jobs are held in memory, so
more than one worker would lose them.

## Tests

```bash
cd webapp
.venv/bin/python tests/test_backend.py

cd tests/browser
npm install
node check_i18n.js
```

See [`webapp/tests/README.md`](webapp/tests/README.md) for the full set.

## Licence

See [LICENSE](LICENSE).
