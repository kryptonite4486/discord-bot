# Weekly Metrics Discord Bot

Modular **discord.py** bot that ingests weekly player metrics (Versus Points, Tech Contribution, HQ Level, Power, Arena Power, Kills), stores them in SQLite, and generates analytical reports on demand. Designed to run in Docker on a local Mac Studio.

## Features

- Slash + prefix commands via cogs (`admin`, `ingest`, `ops`, `reports`)
- Message Content intent for prefix commands and `/ingest batch`
- SQLite fact table `WeeklyMetrics` at `/app/data/weekly.db`, **partitioned per Discord server** (`GuildId`)
- Manual `/add`, pasted CSV/text `/ingest text`, OCR `/ingest image` / `/ingest zip` / `/ingest batch`
- Reports: weekly summary, player trends, growth, leaderboards, PNG charts
- **Server-only**: DMs are rejected; every command runs in a guild context

## Quick start (Docker)

1. Create a Discord application/bot and enable **Message Content Intent**.
2. Invite the bot with `applications.commands` + `bot` scopes (Send Messages, Attach Files, Read Message History).
3. Configure env:

```bash
cp .env.example .env
# edit DISCORD_TOKEN (and optional DEV_GUILD_ID / LEGACY_GUILD_ID)
# set BOT_DATA_DIR and BOT_BACKUP_DIR to absolute host paths outside the repo
mkdir -p ~/DiscordBot/data ~/Documents/DiscordBot-dataBackup
```

4. Build and run:

```bash
docker compose up --build -d
docker compose logs -f
```

## Data and backups

Runtime data lives **outside the repo**, so deleting or re-cloning the code never touches it:

| Host folder (`.env`) | In container | Contents |
|---|---|---|
| `BOT_DATA_DIR`, e.g. `~/DiscordBot/data` | `/app/data` | Live database `weekly.db` (+ `-wal`/`-shm` while running) |
| `BOT_BACKUP_DIR`, e.g. `~/Documents/DiscordBot-dataBackup` | `/app/backups` | Backup copies; safe to sync (e.g. iCloud) |

`docker-compose.yml` refuses to start if either variable is unset. Inside the container, the bot also refuses to start if `/app/data` isn't a mounted folder (override with `ALLOW_UNMOUNTED_DATA=1`), and disables backups if `/app/backups` isn't mounted.

**Backups.** The bot writes `weekly-YYYYMMDD-HHMMSS-daily.db` once a day (checked hourly, so restarts or a sleeping Mac delay it by at most an hour) and keeps the newest `BACKUP_KEEP` (default 14). `/ops backup` (operator only) writes a `-manual.db` copy on demand; the newest 10 are kept. Backups go through SQLite's `VACUUM INTO`, so they're consistent even while the bot is writing, and appear only once complete. Timestamps are UTC.

**Don't open or copy the live `weekly.db` from the Mac while the bot is running.** The database uses WAL mode, whose locking doesn't work across Docker Desktop's VM boundary, and a plain copy can miss recent writes. Open a backup instead, or stop the bot first.

**Restore a backup:**

```bash
docker compose down
cd ~/DiscordBot/data
mkdir -p ../replaced && mv weekly.db* ../replaced/   # keep the current files, just in case
cp ~/Documents/DiscordBot-dataBackup/weekly-YYYYMMDD-HHMMSS-daily.db weekly.db
cd - && docker compose up -d
```

## Local run (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_TOKEN=...
export DATABASE_PATH=./data/weekly.db
python -m bot.main
```

### Vision OCR (oMLX / Qwen2.5-VL)

Screenshots are read by a local OpenAI-compatible vision server (e.g. oMLX). This is the only OCR engine; `OCR_ENGINE` is no longer needed, and values other than `vision` are rejected at startup.

| Variable | Default | Notes |
|----------|---------|--------|
| `OCR_VISION_BASE_URL` | `http://127.0.0.1:8000/v1` | Use `http://host.docker.internal:8000/v1` when the bot runs in Docker on Mac |
| `OCR_VISION_MODEL` | `Qwen2.5-VL-7B-Instruct` | Must match the name registered in oMLX |
| `OCR_VISION_API_KEY` | _(empty)_ | Put your oMLX API token here; requests still send `Authorization` if blank |
| `OCR_VISION_TIMEOUT` | `120` | Seconds |
| `OCR_MAX_CONCURRENCY` | `1` | Images sent to the vision server at once, across all uploads |

**Reliability.** oMLX batches concurrent requests, and with Qwen2.5-VL that garbles replies (broken JSON, runaway generations) and can exhaust GPU memory (`metal::malloc` errors), failing every in-flight request at once. So the bot keeps one bot-wide OCR queue: uploads that arrive together wait their turn ("Queued behind N other OCR batch(es)") and images go to the model one at a time. Each request is capped at 1024 output tokens, and dropped connections, server errors and unreadable replies are retried twice. Batch summaries count images as saved, no players found, or failed, and list failed files to re-upload. Unreadable replies are logged (first 500 characters) for diagnosis.

`docker-compose.yml` maps `host.docker.internal` to the host gateway so the container can reach oMLX on the Mac. After setting the key, rebuild/restart:

```bash
docker compose up --build -d
```

## Commands

### Admin (server administrators; affects only this server)
| Command | Description |
|---------|-------------|
| `/admin stats` | Datastore stats |
| `/admin duplicates` | List player names stored under several spellings |
| `/admin rename-player` | Move a player's rows to the correct spelling |

### Operator (`/ops`)
These act on the whole bot, so only users in `BOT_OWNER_IDS` can run them, and the `/ops` group appears only in the private `CONTROL_GUILD_ID` server. If either variable is unset, `/ops` is disabled.

| Command | Description |
|---------|-------------|
| `/ops reload <cog>` | Reload a cog |
| `/ops sync` | Register commands globally, `/ops` in the control server, and remove duplicate per-server copies everywhere |
| `/ops backup` | Write a database backup now |

`!reload <cog>` and `!sync` do the same from any server, for operators only.

### Ingestion
| Command | Description |
|---------|-------------|
| `/add versus <player> <value> [week]` | Add versus points |
| `/add tech <player> <value> [week]` | Add tech contribution |
| `/add arena <player> <value> [week]` | Add Arena Power |
| `/add kills <player> <value> [week]` | Add total Kills |
| `/add general <player> <hq> <power> [week]` | Add HQ + Power |
| `/ingest image <image…> <dataset> [week]` | OCR up to 10 screenshots on this command |
| `/ingest zip <archive> <dataset> [week]` | OCR every image inside one `.zip` (up to 50) |
| `/ingest batch <dataset> [week]` | Collect images or `.zip` files across messages (20 images; 50 once a zip is included), then OCR |
| `/ingest text <dataset> <data> [week]` | Paste CSV/text rows |

Week accepts `YYYY-MM-DD`, `current`, or `last` (normalized to that week's Sunday).

`/ingest image` accepts up to **10** attachment slots (`image` … `image10`) — Discord’s per-message limit. Multi-select on a single slot usually only sends the first file; fill slots separately or use **`/ingest batch`** for larger Versus/Tech dumps (send several messages of up to 10, then type `done`).

**Zip uploads.** `/ingest zip` (and `/ingest batch` and `!ingestimage`) accept `.zip` archives of screenshots. Images are processed in filename order, and folders, `__MACOSX/`, dotfiles and non-image files are skipped. Limits: 50 images per run, 20 MB per image and 200 MB in total after unzipping. The zip itself must fit your server's Discord upload limit (10 MB without boosts). Screenshots barely compress, so large sets may need splitting across several zips in `/ingest batch`. Progress is posted as a channel message, and results that finish after Discord's 15-minute interaction window are posted to the channel with a mention.

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

### Planner
| Command | Description |
|---------|-------------|
| `/planner` | Post a button linking the [territory planner](https://lastz-territory-planner.pages.dev) web app |
| `/planner plan:<link>` | Repost a plan from the planner's **Share link** button so the channel can open it |

The planner is a separate static site (repo `territory-planner`, hosted on Cloudflare Pages); the bot only links to it. Plans live in the link itself, so the bot stores nothing. `plan` only accepts links on the planner's own address. Override the address with `PLANNER_URL`.

Prefix equivalents use `COMMAND_PREFIX` (default `!`), e.g. `!addversus`, `!addarena`, `!addkills`, `!reportweek`. `!ingestimage <dataset> [week]` requires the dataset.

## Dataset is always named

Every image import names its dataset: `versus`, `tech`, `general`, `power`, `arena` or `kills`. There is no channel that reads posted screenshots automatically; use `/ingest image`, `/ingest zip` or `/ingest batch`.

### Screenshot types

| Dataset | Typical UI | Extracted metrics |
|---------|------------|-------------------|
| **general** | Member cards / profile (HQ icon + Power `65.4M`) | `HQLevel`, `Power` |
| **versus** | Ranked list with points | `VersusPoints` |
| **tech** | Same layout as versus | `TechContribution` |
| **power** | Ranked list with total power | `Power` |
| **kills** | Same layout as power; lifetime kill totals | `Kills` |
| **arena** | Same member cards as general, lower power figure (arena power) | `ArenaPower` (HQ level is ignored) |

There is no auto-detect: every image import names its dataset. Two pairs of screens look identical (general vs arena member cards, and power vs kills leaderboards), so after OCR the bot compares each upload with stored history and flags (🚩) likely mix-ups. Arena Power is part of total Power, so within the same week it can never be higher. Whichever of a week's General/Power and Arena uploads arrives second is checked against the other: a player whose Arena Power is more than 5% above their total Power is named as a likely misread (value or name), and many players with equal values (within 5%) are flagged as a probable dataset mix-up. Comparisons stay within one week, so player growth can't cause false alarms. Across weeks, it flags Power that drops 75%+ or jumps 5x or more (usually a dropped decimal, e.g. 38.5M read as 385M; real weekly growth has peaked around +75%), and Kills totals that go down or jump 5x or more. A screenshot is flagged when at least 3 rows (or 30% of comparable rows) trip a check. Data is still saved. If a flag is right, re-upload under the correct dataset; the rows saved under the wrong metric remain until removed from the database by hand (there is no delete command yet).

`ArenaPower` and `Kills` are snapshots like `Power`: each week stores the latest value, and multi-week reports show the change. For Kills, that change is kills gained over the period.

## Data model

Metrics are scoped by Discord **server** (`GuildId`) and **channel** (`ChannelId`).
Two servers never share rows; within a server, each channel is its own dataset.

```sql
WeeklyMetrics (
  GuildId    TEXT,   -- Discord guild snowflake (string)
  ChannelId  TEXT,   -- Discord channel snowflake (empty only for legacy leftovers)
  WeekStart  TEXT,   -- Sunday YYYY-MM-DD
  PlayerName TEXT,   -- identity key
  MetricType TEXT,   -- VersusPoints | TechContribution | HQLevel | Power | ArenaPower | Kills
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

## Tests

```bash
python -m unittest discover -s tests
```

The suite includes `tests/test_vision_samples.py`, which runs every screenshot in `samples/` through the configured vision model (the same entry point and prompts the bot uses) and checks the extracted names and values. It reads `OCR_VISION_*` from `.env` and swaps `host.docker.internal` for `127.0.0.1` when run outside Docker (or set `OCR_VISION_TEST_BASE_URL`). If the server is unreachable those tests are skipped with a reason; set `RUN_VISION_TESTS=0` to skip them deliberately. Everything else runs offline.

## Project layout

```
bot/
  main.py           # entrypoint
  config.py         # env settings
  cogs/             # admin, help, ingest, ops, planner, reports
  db/               # SQLite helpers
  ocr/              # Vision-model OCR (oMLX) and response parsing
  reporting/        # markdown + matplotlib
  utils/            # logging, parsing
data/               # placeholder only; the database lives in BOT_DATA_DIR
Dockerfile
docker-compose.yml
```

## Discord developer checklist

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot
2. Copy token → `DISCORD_TOKEN`
3. Privileged Gateway Intent: **Message Content Intent** = ON
4. OAuth2 URL Generator: scopes `bot` + `applications.commands`
5. Permissions: Send Messages, Embed Links, Attach Files, Read Message History, Use Application Commands
6. Commands register globally on every start. If they are missing or listed twice, run `/ops sync` in the control server (or `!sync` anywhere, operator only). Set `DEV_GUILD_ID` only on a development bot: commands then go to that one server instead, for instant updates
