# Weekly Metrics Discord Bot

Modular **discord.py** bot that ingests weekly player metrics (Versus Points, Tech Contribution, HQ Level, Power), stores them in SQLite, and generates analytical reports on demand. Designed to run in Docker on a local Mac Studio.

## Features

- Slash + prefix commands via cogs (`admin`, `ingest`, `reports`)
- Message Content intent for OCR channel auto-ingest
- SQLite fact table `WeeklyMetrics` at `/app/data/weekly.db`, **partitioned per Discord server** (`GuildId`)
- Manual `/add`, pasted CSV/text `/ingest text`, OCR `/ingest image` / `/ingest batch`
- Reports: weekly summary, player trends, growth, leaderboards, PNG charts
- **Server-only**: DMs are rejected; every command runs in a guild context

## Quick start (Docker)

1. Create a Discord application/bot and enable **Message Content Intent**.
2. Invite the bot with `applications.commands` + `bot` scopes (Send Messages, Attach Files, Read Message History).
3. Configure env:

```bash
cp .env.example .env
# edit DISCORD_TOKEN (and optional OCR_CHANNEL_ID / DEV_GUILD_ID / LEGACY_GUILD_ID)
```

4. Build and run:

```bash
docker compose up --build -d
docker compose logs -f
```

SQLite persists in `./data/weekly.db` on the host.

## Local run (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_TOKEN=...
export DATABASE_PATH=./data/weekly.db
python -m bot.main
```

Tesseract must be installed on the host if `OCR_ENGINE=tesseract`. EasyOCR downloads models on first use.

### Vision OCR (oMLX / Qwen2.5-VL)

Set `OCR_ENGINE=vision` to use a local OpenAI-compatible vision server (e.g. oMLX) instead of EasyOCR/Tesseract:

| Variable | Default | Notes |
|----------|---------|--------|
| `OCR_VISION_BASE_URL` | `http://127.0.0.1:8000/v1` | Use `http://host.docker.internal:8000/v1` when the bot runs in Docker on Mac |
| `OCR_VISION_MODEL` | `Qwen2.5-VL-7B-Instruct` | Must match the name registered in oMLX |
| `OCR_VISION_API_KEY` | _(empty)_ | Put your oMLX API token here; requests still send `Authorization` if blank |
| `OCR_VISION_TIMEOUT` | `120` | Seconds |

`docker-compose.yml` maps `host.docker.internal` to the host gateway so the container can reach oMLX on the Mac. After setting the key, rebuild/restart:

```bash
docker compose up --build -d
```

Slash commands `/ingest image` and `/ingest batch` are unchanged — only the OCR backend switches.

## Commands

### Admin
| Command | Description |
|---------|-------------|
| `/admin reload <cog>` | Reload a cog |
| `/admin sync` | Sync slash commands |
| `/admin stats` | Datastore stats |

### Ingestion
| Command | Description |
|---------|-------------|
| `/add versus <player> <value> [week]` | Add versus points |
| `/add tech <player> <value> [week]` | Add tech contribution |
| `/add general <player> <hq> <power> [week]` | Add HQ + Power |
| `/ingest image <image…> [dataset] [week]` | OCR up to 10 screenshots on this command |
| `/ingest batch <dataset> [week]` | Collect up to 20 images across messages, then OCR |
| `/ingest text <dataset> <data> [week]` | Paste CSV/text rows |

Week accepts `YYYY-MM-DD`, `current`, or `last` (normalized to that week's Sunday).

`/ingest image` accepts up to **10** attachment slots (`image` … `image10`) — Discord’s per-message limit. Multi-select on a single slot usually only sends the first file; fill slots separately or use **`/ingest batch`** for larger Versus/Tech dumps (send several messages of up to 10, then type `done`).

### Reports
| Command | Description |
|---------|-------------|
| `/report week [week]` | Full weekly summary |
| `/report player <name>` | Player history + chart |
| `/report versus [week]` | Versus leaderboard |
| `/report tech [week]` | Tech leaderboard |
| `/report trend <metric> [weeks]` | Multi-week trends |
| `/report growth <metric> [weeks]` | Growth rates |
| `/report leaderboard <metric>` | Ranked list + chart |

Prefix equivalents use `COMMAND_PREFIX` (default `!`), e.g. `!addversus`, `!reportweek`.

## OCR channel auto-ingest

Set `OCR_CHANNEL_ID` to a Discord channel ID. Images posted there are parsed automatically (up to **10** images per message — Discord’s attachment limit). For up to **20** across messages, use `/ingest batch`.

Optional message text:
- `versus` / `tech` / `general` / `power` — force dataset type
- second token can be a week (`current`, `last`, or `YYYY-MM-DD`)

Example: `general current` + attach member-list screenshots.

### Screenshot types

| Dataset | Typical UI | Extracted metrics |
|---------|------------|-------------------|
| **general** | Member cards / profile (HQ icon + Power `65.4M`) | `HQLevel`, `Power` |
| **versus** | Ranked list with points | `VersusPoints` |
| **tech** | Same layout as versus | `TechContribution` |

## Data model

Metrics are scoped by Discord **server** (`GuildId`) and **channel** (`ChannelId`).
Two servers never share rows; within a server, each channel is its own dataset.

```sql
WeeklyMetrics (
  GuildId    TEXT,   -- Discord guild snowflake (string)
  ChannelId  TEXT,   -- Discord channel snowflake (empty only for legacy leftovers)
  WeekStart  TEXT,   -- Sunday YYYY-MM-DD
  PlayerName TEXT,   -- identity key
  MetricType TEXT,   -- VersusPoints | TechContribution | HQLevel | Power
  Value      REAL,
  PRIMARY KEY (GuildId, ChannelId, WeekStart, PlayerName, MetricType)
)
```

Ingest always writes the current channel’s ID. Default reports filter to the current
channel. Pass `scope: Entire server` on report commands for a multi-channel breakout
(one row per player+channel; values are **not** merged across channels).

### Channel backfill (before Phase 2)

If you still have rows with empty `ChannelId` from Phase 1:

1. Stay on the Phase 1 deploy and run `/admin assign-channel` in each server.
2. Confirm `/admin stats` shows **Unassigned channel rows = 0**.
3. Then deploy this Phase 2 build (strict channel filter; `assign-channel` removed).

Phase 2 logs a startup warning if any unassigned rows remain (they are hidden from
channel-scoped reports).

### Migrating an existing database

On startup, if `WeeklyMetrics` exists without `GuildId`, the bot rebuilds the table and assigns every existing row a guild:

1. Set `LEGACY_GUILD_ID` to your Discord server’s snowflake ID (Developer Mode → right-click server → Copy Server ID) **before** the first upgraded start.
2. If `LEGACY_GUILD_ID` is unset, rows are tagged `GuildId=legacy` and a warning is logged (you can still query them only under that literal id).

If the table has `GuildId` but no `ChannelId`, startup adds `ChannelId` default `''` (unassigned) for all existing rows — backfill those before relying on Phase 2 isolation.

Fresh databases skip migration and create the new schema directly.

## Project layout

```
bot/
  main.py           # entrypoint
  config.py         # env settings
  cogs/             # admin, ingest, reports
  db/               # SQLite helpers
  ocr/              # EasyOCR / Tesseract / vision (oMLX) pipeline
  reporting/        # markdown + matplotlib
  utils/            # logging, parsing
data/               # persistent volume (weekly.db)
Dockerfile
docker-compose.yml
```

## Discord developer checklist

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot
2. Copy token → `DISCORD_TOKEN`
3. Privileged Gateway Intent: **Message Content Intent** = ON
4. OAuth2 URL Generator: scopes `bot` + `applications.commands`
5. Permissions: Send Messages, Embed Links, Attach Files, Read Message History, Use Application Commands
6. After first start, run `/admin sync` in your server if commands are missing (or set `DEV_GUILD_ID` for instant guild sync)
