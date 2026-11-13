# Clash of Clans Telegram Home Server

## Recommended: separate Home Assistant App (v0.3.0)

This repository installs as a standalone **CoC Telegram Bot** Home Assistant add-on, separate from any smart-home Telegram bot. See [add-on setup, configuration and role model](coc_telegram/DOCS.md). Install the **Beerlogger01** repository in Home Assistant; changes to staging PRs in Alexey3476 are not installed until merged into Beerlogger01/main and the GHCR image is published.

CoC game-token verification is required for each linked account. A verified leader or co-leader on **any** linked CoC account can use the private `/staff` interface without Telegram administrator permissions. Regular KV and CWL opt-in are read from the official game's `warPreference`, not manually duplicated in Telegram. The public CoC API cannot expose player last-login or individual Clan Games points.

A lightweight home-server stack that exposes a FastAPI backend for Clash of Clans data and a Telegram bot that consumes it. All Clash of Clans API traffic is centralized in the backend with bounded in-memory caching to protect rate limits.

## Architecture

```
Telegram Bot
    ↓
Backend (FastAPI)
    ↓
Clash of Clans API
    ↓
```

## Version 0.3.0 — clan headquarters

A consistent Telegram menu now links wars, saved lineups/readiness, ratings,
player cards, personal accounts and clan polls. Lists, equipment and development
changes are paginated. Buttons belong to their creator and current screen;
stale, repeated and foreign callbacks cannot change a newer selection.

- `/lineup`: captains create a named war collection, choose the KV size, edit
  the saved lineup, and publish it to the group. CWL always uses 15 players.
- `/ready`: answer separately for each verified alt: ready, unavailable or heroes
  upgrading. Each new collection starts without previous readiness answers.
- `/rating war|cwl`: full clan ranking with a clickable explanation of each score.
- `/history`: saved completed wars and attacks against lower/equal/higher THs.
- `/cwl`: seven individually accessible rounds and placement by total stars;
  unavailable rounds do not renumber later wars.
- `/poll`: question, editable answers, duration, preview and timed results.
  Published polls can be browsed from the clan menu.

Experience and league remain additional signals, not measurements of defenses
or proof of attack skill. A missed CWL round is flagged for leadership review,
without an automatic season-long ban. See [current rules and delivery limits](coc_telegram/DOCS.md)
and the [offline interface QA report](docs/interface-qa-0.3.0.md).

## Features

- FastAPI backend with bounded in-memory TTL caching
- Telegram bot commands for clan, player, and war data
- Player tag bindings with war reminder mentions
- Dockerized deployment with docker-compose
- IP-restricted CoC API token used only by the backend
- Environment-driven configuration

## Requirements

- Docker + Docker Compose
- Clash of Clans API token
- Telegram bot token

## Setup

1. **Clone and prepare environment**

```bash
git clone <your-repo-url>
cd coc-telegram-home-server
cp .env.example .env
```

⚠️ **Important**: Create `.env` once from `.env.example` and then edit it manually. Do **not** re-run `cp .env.example .env` later, or you'll overwrite your configured secrets with empty values. If you must recreate `.env`, back up the old file first.

2. **Configure environment variables** in `.env`:

- `COC_TOKEN`: Clash of Clans API token
- `COC_CLAN_TAG`: Default clan tag (e.g. `#ABCD1234`)
- `TELEGRAM_BOT_TOKEN`: Telegram bot token
- `BACKEND_URL`: Backend service URL used by the bot
- `BINDINGS_DB_PATH`: SQLite path for bindings (default `/data/bindings.db`)
- `CLAN_GROUP_ID`: Telegram chat ID for the private clan group
- `INVITE_TTL_MINUTES`: Minutes before invite link expires
- `ENFORCE_CLAN_MEMBERSHIP`: Require bound player to be in `COC_CLAN_TAG` (true/false)
- `WAR_REMINDER_ENABLED`: Enable war reminders (true/false)
- `WAR_REMINDER_WINDOW_HOURS`: Reminder window in hours
- `WAR_REMINDER_INTERVAL_MINUTES`: Reminder interval in minutes

3. **Run locally with Docker**

```bash
docker compose up --build
```

4. **View logs**

```bash
docker compose logs -f
```

## Clash of Clans API Token

1. Visit https://developer.clashofclans.com
2. Create an account and log in.
3. Create a new API token.
4. Restrict the token by your home server's public IP address.
5. Copy the token into `.env` as `COC_TOKEN`.

## Telegram Bot Setup

1. Open Telegram and start a chat with `@BotFather`.
2. Create a bot and get the token.
3. Paste the token into `.env` as `TELEGRAM_BOT_TOKEN`.

## Player Bindings & War Reminders

Use these commands inside a private chat with the bot (or press the “Привязать” button shown by `/start`):

- `/bind #PLAYER_TAG`: Link your Telegram account to a Clash of Clans player tag and receive a one-time invite link.
- `/unbind`: Remove your current binding.
- `/mytag`: Display your bound tag.

The clan group must be private. Add the bot as an admin with permission to invite users and remove unbound members. After a successful bind, the bot issues a short-lived, single-use invite link and verifies new members on join.

During an active war, reminders include **every remaining attack**, including
1/2 attacks used in regular wars. All alts belonging to one Telegram user are
listed under a single mention. Persistent claims allow at most one reminder per
war/owner in the configured window and one in the last 30 minutes; restarting
the bot does not repeat them. An ambiguous Telegram delivery error is not
retried automatically. Configure behavior with:

- `BINDINGS_DB_PATH` (default `/data/bindings.db`)
- `WAR_REMINDER_ENABLED` (true/false)
- `WAR_REMINDER_WINDOW_HOURS` (default `4`)
- `WAR_REMINDER_INTERVAL_MINUTES` (default `15`)

## Endpoints

Backend provides:

- `GET /clan`
- `GET /player/{tag}`
- `GET /war`

The bot calls these endpoints and never calls the Clash of Clans API directly.

## Health Check

`GET /health` returns a simple status payload for monitoring.

## Deploy on a Home Server

1. Install Docker and Docker Compose.
2. Copy the repository to your server.
3. Configure `.env`.
4. Run:

```bash
docker compose up --build -d
```

### Optional: systemd service

Use `deploy/systemd/coc-telegram.service` to auto-start Docker Compose on boot:

```bash
sudo cp deploy/systemd/coc-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable coc-telegram.service
sudo systemctl start coc-telegram.service
```

## Troubleshooting

- **Verify configuration**: check required options in Home Assistant or your local `.env`; do not paste tokens or complete environment dumps into logs or chats.
- **401/403 from CoC API**: This often happens when `COC_TOKEN` is empty/invalid or the IP is not whitelisted. Confirm the token value exists in `.env` and is restricted to the correct public IP.
- **CoC token rejected**: Ensure the token is IP-restricted to your home server's public IP, and that you are not running from a different network. Invalid tokens will return 403/404 or 429 from the backend.
- **Docker networking**: Use `BACKEND_URL=http://backend:8000` inside Docker; `localhost` will refer to the bot container itself.
- **Tag formatting**: Tags must use valid Clash of Clans characters (e.g. `#2PRGP0L22`). The backend normalizes tags and URL-encodes `#` as `%23`.
- **Missing `.env` values**: Ensure `.env` is present and populated with `COC_TOKEN`, `COC_CLAN_TAG`, and `TELEGRAM_BOT_TOKEN`.

## Testing Checklist

- `/ping` in private chat replies with `pong`.
- `/start` in private chat shows the “Привязать” button.
- Tap “Привязать” → send tag → receive invite link (if `CLAN_GROUP_ID` is set).
- `/mytag` in private chat returns the stored tag.
- Join the clan group with the invite link and ensure unbound users are removed.

## GitHub Initialization

```bash
git init
git add .
git commit -m "Initial home server stack"
git branch -M main
git remote add origin git@github.com:<your-user>/<your-repo>.git
git push -u origin main
```

## Private Telegram HTML Widgets (0.4.0, opt-in)

Enable `widgets_enabled` in the add-on and send `/widgets` privately. `/classic`
restores the existing interface. The first version provides live data dashboards
and opens established inline flows for operational changes. See the
[architecture, interface map, testing limits and update instructions](docs/widgets/README.md).
