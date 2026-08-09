# Societ — Game Studio Portal & Discord Bot

Dark space-themed studio portal backed by a Discord bot. A single process runs both the
`discord.py` bot and an `aiohttp` REST API that serves the web frontend.

| File | Purpose |
| --- | --- |
| `main.py` | Discord bot, REST API, Discord OAuth2, RBAC, SQLite (`database.db`) |
| `index.html` | Single-page frontend (Tailwind CDN, canvas starfield VFX) |
| `seed_database.py` | Demo operatives, projects and store items |
| `assets/` | `IMG_20260719_104125.png` (logo), `IMG_3555.jpg` (wallpaper) |
| `main(old).py` | Legacy internal ops bot (tickets, meetings, voice master) — unchanged |

## Run

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the values
set -a && source .env && set +a
python seed_database.py --admin-discord-id <your-discord-id>   # optional demo data
python main.py
```

The portal is served at http://localhost:5000. Without `DISCORD_TOKEN` the web portal still
starts (bot disabled), which is handy for frontend work.

In the Discord Developer Portal add `http://localhost:5000/auth/callback` (or your public
URL) as an OAuth2 redirect.

## Serving the standalone Societ-web frontend

[`sunlinko-anito/Societ-web`](https://github.com/sunlinko-anito/Societ-web) is the same portal
hosted separately. Point its `config.js` `API_BASE` at this server and set here:

```bash
ALLOWED_ORIGINS=https://societ-web.example.com
SESSION_COOKIE_SAMESITE=None      # cross-site cookies; browsers require HTTPS (localhost is exempt)
POST_LOGIN_REDIRECT=https://societ-web.example.com
```

## Roles

| Role | Access |
| --- | --- |
| Guest | Landing page, operatives roster, project archives |
| Employee | + store, points balance, redemptions, self-service profile editing |
| Admin (`employees.is_admin = 1`) | + CRUD for staff, games, store items and manual point adjustments |

## Slash commands

* `/test` — health check (latency, portal URL, database)
* `/work <channel> <member>` — post a back-to-work reminder
* `/rd_employee <channel> [position]` — draw a random operative, optionally filtered by position
* `/add_employee <member> <nickname> <position> [bio] [contact_email] [points] [is_admin]` — admin-only upsert
* `/points check [member]` — points balance
* `/points give <member> <amount> [reason]` — admin-only point adjustment

## REST API

Public: `GET /api/employees`, `GET /api/games`, `GET /api/store/items`, `GET /api/me`

Employee: `POST /api/store/redeem`, `GET /api/me/transactions`, `PATCH /api/me/profile`
(the last one also accepts `is_visible` so an operative can hide their own card from the
public roster)

Admin: `POST|DELETE /api/admin/employees`, `POST /api/admin/points`,
`POST|DELETE /api/admin/games`, `GET|POST|DELETE /api/admin/store/items`,
`GET /api/admin/transactions`

`POST /api/store/redeem` deducts points inside a single transaction, decrements stock when it
is finite, records the redemption and posts an embed to `ADMIN_WEBHOOK_URL`.

## Palette

Background `#0b0520 → #081738 → #0f3d1f`, body text `#f1f5f9` / `#cbd5e1` (Inter),
headings in Rajdhani with `#38bdf8` → `#34d399` gradient accents.
