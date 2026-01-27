# Clash of Clans Telegram Home Server

A lightweight home-server stack that exposes a FastAPI backend for Clash of Clans data and a Telegram bot that consumes it. All Clash of Clans API traffic is centralized in the backend with Redis caching to protect rate limits.

## Architecture

```
Telegram Bot
    ↓
Backend (FastAPI)
    ↓
Clash of Clans API
    ↓
Redis (cache layer)
```

## Features

- FastAPI backend with Redis caching
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

2. **Configure environment variables** in `.env`:

- `COC_TOKEN`: Clash of Clans API token
- `COC_CLAN_TAG`: Default clan tag (e.g. `#ABCD1234`)
- `TELEGRAM_BOT_TOKEN`: Telegram bot token
- `REDIS_URL`: Redis connection string (default in `.env.example`)
- `BACKEND_URL`: Backend service URL used by the bot

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

Use these commands inside a group or supergroup chat:

- `/bind #PLAYER_TAG`: Link your Telegram account to a Clash of Clans player tag.
- `/unbind`: Remove your current binding for the group.
- `/mytag`: Display your bound tag for the group.

When a war is active and the war end time is within the reminder window, the bot checks the clan war roster for members with 0 attacks used. If a bound player is found, the bot posts a single reminder message mentioning each bound user by ID. Each user is cooled down for one hour between reminders. Configure behavior with:

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

- **CoC token rejected**: Ensure the token is IP-restricted to your home server's public IP, and that you are not running from a different network. Invalid tokens will return 403/404 or 429 from the backend.
- **Docker networking**: Use `BACKEND_URL=http://backend:8000` inside Docker; `localhost` will refer to the bot container itself.
- **Tag formatting**: Tags must use valid Clash of Clans characters (e.g. `#2PRGP0L22`). The backend normalizes tags and URL-encodes `#` as `%23`.
- **Missing `.env` values**: Ensure `.env` is present and populated with `COC_TOKEN`, `COC_CLAN_TAG`, and `TELEGRAM_BOT_TOKEN`.

## GitHub Initialization

```bash
git init
git add .
git commit -m "Initial home server stack"
git branch -M main
git remote add origin git@github.com:<your-user>/<your-repo>.git
git push -u origin main
```
