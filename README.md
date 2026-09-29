# Weekly Metrics Discord Bot

Modular **discord.py** bot that ingests weekly player metrics (Versus Points, Tech Contribution, HQ Level, Power), stores them in SQLite, and generates analytical reports on demand. Designed to run in Docker on a local Mac Studio.

## Features

- Slash + prefix commands via cogs (`admin`, `ingest`, `reports`)
- Message Content intent for OCR channel auto-ingest
- SQLite fact table `WeeklyMetrics` at `/app/data/weekly.db`
- Manual `/add`, pasted CSV/text `/ingest text`, and OCR `/ingest image`
- Reports: weekly summary, player trends, growth, leaderboards, PNG charts

## Quick start (Docker)

1. Create a Discord application/bot and enable **Message Content Intent**.
2. Invite the bot with `applications.commands` + `bot` scopes (Send Messages, Attach Files, Read Message History).
3. Configure env:

```bash
cp .env.example .env
# edit DISCORD_TOKEN (and optional OCR_CHANNEL_ID / DEV_GUILD_ID)
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
| `/ingest image <image> [dataset] [week]` | OCR screenshot |
| `/ingest text <dataset> <data> [week]` | Paste CSV/text rows |

Week accepts `YYYY-MM-DD`, `current`, or `last` (normalized to that week's Monday).

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

Set `OCR_CHANNEL_ID` to a Discord channel ID. Images posted there are parsed automatically.

Optional message text:
- `versus` / `tech` / `general` — force dataset type
- second token can be a week (`current`, `last`, or `YYYY-MM-DD`)

Example: `general current` + attach member-list screenshot.

### Screenshot types

| Dataset | Typical UI | Extracted metrics |
|---------|------------|-------------------|
| **general** | Member cards / profile (HQ icon + Power `65.4M`) | `HQLevel`, `Power` |
| **versus** | Ranked list with points | `VersusPoints` |
| **tech** | Same layout as versus | `TechContribution` |

## Data model

```sql
WeeklyMetrics (
  WeekStart  TEXT,   -- ISO Monday YYYY-MM-DD
  PlayerName TEXT,   -- identity key
  MetricType TEXT,   -- VersusPoints | TechContribution | HQLevel | Power
  Value      REAL,
  PRIMARY KEY (WeekStart, PlayerName, MetricType)
)
```

## Project layout

```
bot/
  main.py           # entrypoint
  config.py         # env settings
  cogs/             # admin, ingest, reports
  db/               # SQLite helpers
  ocr/              # EasyOCR / Tesseract pipeline
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
