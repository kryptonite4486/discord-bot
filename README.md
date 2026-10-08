# LastZ Assistant

Discord bot for Last Z alliances: a modular **discord.py** bot that ingests weekly player metrics (Versus Points, Tech Contribution, HQ Level, Power, Arena Power, Kills), stores them in SQLite, and generates analytical reports on demand. Designed to run in Docker on a local Mac Studio.

## Features

- Slash commands only, via cogs (`admin`, `data`, `ingest`, `ops`, `reports`, `trivia`)
- No privileged intents: the bot can't read ordinary messages; `/ingest batch` collects messages that @mention it
- SQLite fact table `WeeklyMetrics` at `/app/data/weekly.db`, **partitioned per Discord server** (`GuildId`)
- Manual `/add`, pasted CSV/text `/ingest text`, OCR `/ingest image` / `/ingest zip` / `/ingest batch`
- Reports: weekly summary, player trends, growth, leaderboards, PNG charts
- **Server-only**: DMs are rejected; every command runs in a guild context
- Trivia matches in one channel, or across every server that joins (`/trivia`)

## Quick start (Docker)

1. Create a Discord application/bot. No privileged intents are needed; leave **Message Content Intent** off.
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
| `BOT_OFFSITE_BACKUP_DIR` (optional), e.g. an iCloud Drive, Dropbox or NAS folder | `/app/offsite-backups` | A second copy of each daily backup |

`docker-compose.yml` refuses to start if either variable is unset. Inside the container, the bot also refuses to start if `/app/data` isn't a mounted folder (override with `ALLOW_UNMOUNTED_DATA=1`), and disables backups if `/app/backups` isn't mounted.

**Backups.** The bot writes `weekly-YYYYMMDD-HHMMSS-daily.db` once a day (checked hourly, so restarts or a sleeping Mac delay it by at most an hour) and keeps the newest `BACKUP_KEEP` (default 14). `/ops backup` (operator only) writes a `-manual.db` copy on demand, as do the safety copies taken before renames and deletions; the newest 10 are kept. **No backup is kept longer than `BACKUP_MAX_AGE_DAYS` (default 30)**, checked hourly, except that the single newest backup is never removed, so a stretch without new backups can't leave none at all. Backups go through SQLite's `VACUUM INTO`, so they're consistent even while the bot is writing, and appear only once complete. Timestamps are UTC.

**Offsite copy (optional).** After each daily backup the bot can copy it to a second folder that leaves the machine: iCloud Drive, Dropbox, or a mounted NAS share. No cloud SDK or credentials are involved; the sync client or the mount does the moving. The same retention applies there (newest `BACKUP_KEEP` daily copies, nothing older than `BACKUP_MAX_AGE_DAYS`, checked hourly), so data deleted from the bot is gone from the offsite copies within the same 30 days. The copy is retried every hour until it succeeds, and a failure DMs the operator (see [Monitoring](#monitoring)). With Docker:

1. Create the folder on the Mac, e.g. `mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/DiscordBot-offsite` (iCloud Drive) or `~/Dropbox/DiscordBot-offsite`. For a NAS, mount the share in Finder first. Docker Desktop must be allowed to share the location (Settings → Resources → File sharing; `/Users` is shared by default).
2. Set `BOT_OFFSITE_BACKUP_DIR=/Users/you/...` in `.env` (absolute path; quote it if it has spaces).
3. Uncomment the `BOT_OFFSITE_BACKUP_DIR` volume line in `docker-compose.yml`, then `docker compose up -d`.

The folder must already exist: if the drive isn't mounted the bot reports an error rather than quietly writing the "offsite" copy to the local disk. Without Docker, set `OFFSITE_BACKUP_DIR` to the folder instead.

**Don't open or copy the live `weekly.db` from the Mac while the bot is running.** The database uses WAL mode, whose locking doesn't work across Docker Desktop's VM boundary, and a plain copy can miss recent writes. Open a backup instead, or stop the bot first.

**Restore a backup:**

```bash
docker compose down
cd ~/DiscordBot/data
mkdir -p ../replaced && mv weekly.db* ../replaced/   # keep the current files, just in case
cp ~/Documents/DiscordBot-dataBackup/weekly-YYYYMMDD-HHMMSS-daily.db weekly.db
cd - && docker compose up -d
```

## Monitoring

The bot runs on a home Mac, so it watches itself and tells the operator when something breaks. Nothing here sends screenshots, OCR replies or player data anywhere: alerts name the failing part and a short reason (an HTTP status or error type).

**Outside uptime check (heartbeat).** Set `HEARTBEAT_URL` to a dead-man's-switch ping URL and the bot GETs it every 5 minutes, but only while it's connected to Discord and its database answers. If the Mac sleeps or goes offline, Docker stops, or the bot crashes or loses Discord, the pings stop and the service alerts you (email, phone push, Slack, etc.). Empty means off. To set up a free check on [healthchecks.io](https://healthchecks.io) (the free plan covers this; Better Stack and similar services work the same way):

1. Sign up and create a project, then **Add Check**.
2. Set **Period** to 5 minutes and **Grace** to 10 minutes (a sleeping Mac or a short Wi-Fi drop within the grace time won't alert).
3. Copy the check's ping URL (`https://hc-ping.com/<uuid>`) into `.env` as `HEARTBEAT_URL=...`, then `docker compose up -d`.
4. Under **Integrations**, choose how you want to be told (email is on by default).

The ping URL is a secret: anyone with it can keep your check green. The bot never logs or shows it.

**Docker healthcheck.** Every minute that the bot is connected and its database works, it touches `/tmp/lastz-assistant.healthy` in the container; `docker-compose.yml`'s healthcheck fails once that file is 3 minutes old, so `docker compose ps` shows `(unhealthy)`. It only reports; Compose doesn't restart unhealthy containers (`restart: unless-stopped` already restarts a crashed bot).

**Alert DMs.** Users in `BOT_OWNER_IDS` get a DM when:

- the OCR server fails 3 checks or OCR calls in a row (the bot probes `GET {OCR_VISION_BASE_URL}/models` every 2 minutes and checks `OCR_VISION_MODEL` is listed; real OCR calls that fail after their retries count too, unreadable model replies don't), and again when it recovers;
- the daily backup or the offsite copy fails, and again when it works;
- the bot comes back after being away at least 10 minutes: disconnected from Discord, the Mac asleep, or the container down (measured from the health file at startup).

Each problem alerts once and sends one "recovered" message. Messages of the same kind are at least 30 minutes apart, so a flapping server can't spam: if it fails again within 30 minutes of the last message, the incident is recorded quietly and the alert goes out when the 30 minutes are up, only if it's still failing. A DM that can't be delivered isn't retried; `/ops health` still shows the incident.

**`/ops health`** (operator only, in the control server) shows uptime, Discord latency, the OCR server's status and last error, the last daily backup and offsite copy, the heartbeat's last ping, and open incidents.

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

**Reliability.** oMLX batches concurrent requests, and with Qwen2.5-VL that garbles replies (broken JSON, runaway generations) and can exhaust GPU memory (`metal::malloc` errors), failing every in-flight request at once. So the bot keeps one bot-wide OCR queue and images go to the model one at a time. Each upload command is one request (a single image, an `/ingest image` set, a zip, or an `/ingest batch`), and a request runs to the end before the next one starts. Servers take turns: when a request finishes, the next one comes from the next server with something waiting, so one server's backlog can't hold up another's. Within a server, requests run in the order they were sent. Users only see their own server's backlog: an upload waiting behind earlier uploads from the same server shows "N queued ahead", and one waiting only on other servers shows nothing extra. Other servers' requests are never mentioned. `/ops queue` shows the whole queue. Each request is capped at 1024 output tokens, and dropped connections, server errors and unreadable replies are retried twice. Batch summaries count images as saved, no players found, or failed, and list failed files to re-upload. Unreadable replies are logged (first 500 characters) for diagnosis.

`docker-compose.yml` maps `host.docker.internal` to the host gateway so the container can reach oMLX on the Mac. After setting the key, rebuild/restart:

```bash
docker compose up --build -d
```

## Commands

### Admin (server administrators; affects only this server)

`/admin` and `/data` are hidden from members without Administrator, and each command also checks the permission, so widening access under Server Settings → Integrations doesn't let others run them.

| Command | Description |
|---------|-------------|
| `/admin stats` | Datastore stats |
| `/admin duplicates` | List player names stored under several spellings |
| `/admin rename-player` | Move a player's rows to the correct spelling |
| `/data delete [scope]` | Permanently delete this channel's metrics, or the entire server's with `scope:Entire server`. Shows what will be deleted and asks for confirmation; a backup is taken first |
| `/data export [scope] [format] [from_week] [to_week]` | Download this channel's metrics, or the entire server's with `scope:Entire server`, as CSV (default) or JSON: player, metric, value, week, channel name and ID, and when the row was last updated. Weeks are inclusive and accept `YYYY-MM-DD`, `current` or `last`. Sent privately to you; zipped if it's over the server's upload limit. Alliance and Command plans; it shows the same weeks as reports, so older weeks outside the plan's history window are left out and named |

### Setup (Manage Server)

| Command | Description |
|---------|-------------|
| `/setup` | Show this server's settings |
| `/setup trivia_channel:<#channel>` | Make that the trivia channel |
| `/setup clear_trivia_channel:True` | Remove the trivia channel |
| `/setup report_channel:<#channel>` | Where the bot posts notices for this server, such as a gifted plan |
| `/setup clear_report_channel:True` | Remove the report channel; the bot then never posts on its own |
| `/setup default_week:<Current week\|Last week\|Bot default>` | The week `/add`, `/ingest` and `/report week` use when you don't give one |

**We recommend a dedicated trivia channel** (e.g. `#trivia`), so matches stay out of the channels that hold your alliance's stats. Once one is set:

- `/trivia start` and `/trivia join` work only there (or in a thread inside it); elsewhere they reply privately with a pointer to it.
- `/add` and `/ingest` are refused there, so stats are never added to the trivia channel. Reports still work, since they only read data.
- Invitations to other servers' cross-server matches are posted there.

With no trivia channel, trivia runs anywhere and nothing is restricted. Commands still appear in the `/` menu in every channel; the bot can't hide them per channel. Server admins can, under **Server Settings → Integrations → LastZ Assistant**: pick a command and add channel overrides. `/setup` is hidden from members without Manage Server and also checks the permission. **Report channel.** The bot posts there only when it has something for the server on its own: the thank-you when a plan is gifted (`/ops grant ... notify:True`) and a heads-up 3 days before a gifted plan or free trial ends; later, scheduled reports. With none set it never posts unprompted. `/setup` checks the bot can view the channel, send messages and embed links there before saving it, and every post checks again.

**Default week.** Weeks start on Sunday. With no `week` given, `/add` and `/ingest` save into, and `/report week` shows, the server's default week: the current week, or last week for alliances that post stats after the weekly reset. Unset, it's the current week (for `/add` and `/ingest`, the operator's `DEFAULT_WEEK_START` comes first if set). Leaderboards (`/report versus`, `/report tech`, `/report leaderboard`) still default to the latest week that has data.

Settings are stored in `GuildSettings (GuildId, TriviaChannelId, ReportChannelId, DefaultWeek, UpdatedAt)`; `DefaultWeek` is `current`, `last` or NULL.

### Operator (`/ops`)
These act on the whole bot, so only users in `BOT_OWNER_IDS` can run them, and the `/ops` group appears only in the private `CONTROL_GUILD_ID` server. If either variable is unset, `/ops` is disabled.

| Command | Description |
|---------|-------------|
| `/ops reload <cog>` | Reload a cog |
| `/ops sync` | Register commands globally, `/ops` in the control server, and remove duplicate per-server copies everywhere |
| `/ops backup` | Write a database backup now |
| `/ops health` | Uptime, Discord latency, OCR server status and last error, last daily backup and offsite copy, heartbeat, open incidents. See [Monitoring](#monitoring) |
| `/ops queue` | Live OCR queue: requests running and waiting per server, and how long the oldest has waited |
| `/ops purges` | Servers that removed the bot and when their data will be deleted |
| `/ops export <server> [format]` | Every week and channel of a server's metrics as CSV or JSON, whatever its plan, for deletion and access requests. Works after the bot has been removed, until the data is purged |
| `/ops grant <server> <tier> <duration> <reason> [notify]` | Gift Alliance or Command to a server for 30 days, 90 days, 1 year or permanently. `notify:True` posts a thank-you in the server's report channel (the reason isn't shown); if it can't (no report channel, or no permission), the reply says why and the gift still applies |
| `/ops revoke <server> <reason> [entitlement]` | Revoke a server's active gifts (or one entitlement); paid subscriptions can't be revoked here |
| `/ops extend <entitlement> <duration>` | Extend a gift, or make it permanent |
| `/ops show <server>` | A server's plan, all its entitlements (trials included), whether it has used its free trial, this week's screenshots and recent changes |
| `/ops trial-reset <server> <reason>` | Let a server start the free trial again (audited). A trial still running isn't touched; `/ops revoke` it if needed |
| `/ops list [expiring_within]` | Active entitlements across servers, soonest-ending first |
| `/ops code create <tier> <duration> <uses> [expires] [note]` | Make a gift code that up to `uses` servers can redeem with `/redeem`; `expires` is the number of days it stays redeemable (default: until used up or revoked). The code is shown once: only its hash is stored |
| `/ops code list [include_inactive]` | Codes by number and last four characters, with uses and status |
| `/ops code revoke <code_id> <reason> [revoke_redeemed]` | Stop a code being redeemed; `revoke_redeemed` also revokes the plans servers already got from it |
| `/ops usage [days]` | OCR usage per server for the last N days (default 7): batches, images, failures, OCR minutes, seconds per image, average queue wait, and the busiest day |
| `/ops abuse` | Abuse controls: the people who started the most OCR requests in the current rate-limit window (requests, images, refusals; per server), and people who use OCR in more than `FREE_SERVERS_PER_OWNER` Free servers, with those servers |
| `/ops capacity [days] [utilization] [seconds_per_image]` | OCR capacity from the usage ledger (default 28 days): seconds per image overall and per server, images per weekday, busiest days, queue wait, failure rate, and how many Free, Alliance and Command servers at full quota fit at the peak day. `scripts/benchmark_ocr.py` times the OCR on the sample screenshots offline. See `docs/monetization-plan.md` §2 item 14 |

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
| `/ingest batch <dataset> [week]` | Collect images or `.zip` files from messages that @mention the bot (20 images; 50 once a zip is included), then OCR |
| `/ingest text <dataset> <data> [week]` | Paste CSV/text rows |

Week accepts `YYYY-MM-DD`, `current`, or `last` (normalized to that week's Sunday).

**Rate limit.** Screenshot reading runs on one shared OCR server, so each person can start up to **6** OCR requests (`/ingest image`, `zip` or `batch`) and **60** screenshots per **10** minutes in each server (`INGEST_RATE_REQUESTS`, `INGEST_RATE_IMAGES`, `INGEST_RATE_WINDOW_MINUTES`; 0 turns a cap off). One full `/ingest image` per dataset on reset day fits comfortably; a single 50-image zip always fits an empty window. Members with Manage Server get 3× as much (`INGEST_RATE_ADMIN_MULTIPLIER`) rather than no limit, because they're the ones uploading the whole roster but a server's own admins can also be the ones hammering it. Operators (`BOT_OWNER_IDS`) are exempt. The check runs before anything is downloaded or queued, and the ephemeral reply says when to try again. `/ingest zip` and `/ingest batch` are checked again once the image count is known. This applies whether or not tiers are enforced. `/add` and `/ingest text` aren't limited: they never reach the OCR server and each is one small database write. The counts are kept in memory, so a restart clears them.

`/ingest image` accepts up to **10** attachment slots (`image` … `image10`) — Discord’s per-message limit. Multi-select on a single slot usually only sends the first file; fill slots separately or use **`/ingest batch`** for larger Versus/Tech dumps: send several messages of up to 10 images, each @mentioning the bot (on a phone you can pick 10 photos at once), then send `@LastZ Assistant done`. Messages that don't @mention the bot are ignored: without the Message Content intent, Discord only shows the bot messages that mention it. Mention the bot itself, not its role.

**Zip uploads.** `/ingest zip` and `/ingest batch` accept `.zip` archives of screenshots. Images are processed in filename order, and folders, `__MACOSX/`, dotfiles and non-image files are skipped. Limits: 50 images per run, 20 MB per image and 200 MB in total after unzipping. The zip itself must fit your server's Discord upload limit (10 MB without boosts). Screenshots barely compress, so large sets may need splitting across several zips in `/ingest batch`. Progress is posted as a channel message, and results that finish after Discord's 15-minute interaction window are posted to the channel with a mention.

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

### Plans

| Command | Description |
|---------|-------------|
| `/premium` | This server's plan, screenshots used this week, channels with data, and what each plan includes. Server admins (Manage Server) also get a **Start free trial** button while the server can have one |
| `/redeem <code>` | Redeem a gift code for this server (Manage Server) |

Each server is on **Free**, **Alliance** or **Command**: the highest plan among its active entitlements (paid, gifted, trial or from a gift code, in `GuildEntitlement`). Plans differ in screenshots per week (25 / 250 / 1,000, Sunday to Sunday UTC), in how many weeks of history reports show (the last 4 / 26 weeks including this one / all), in how many channels can hold data (1 / 3 / any), and in these features:

| Feature | Plan needed |
|---|---|
| `/ingest zip`, `/ingest batch` | Alliance |
| `/report player`, `/report trend`, `/report growth` | Alliance |
| `/admin duplicates`, `/admin rename-player` | Alliance |
| `/data export` | Alliance |
| Reports over more than one channel | Command |

Older weeks are hidden from reports, never deleted: upgrading shows them again. When a report leaves weeks out it ends with a 🔒 note saying how many and which plan shows them. `/data export` follows the same window (`/ops export` doesn't).

**Channel limit.** Each channel is its own dataset. `/add` and every `/ingest` command refuse to start data in a channel past the plan's limit, and the reply names the channels that can take data. Data is never deleted for being over the limit. A server with more channels than its plan allows (after a downgrade, or data added before limits were on) keeps reports for all of them, but only its most recently written channels take new data: the 1 (Free) or 3 (Alliance) whose latest row is newest. Upgrading makes every channel writable again. Rows without a channel (legacy leftovers) don't count.

**Free servers per person.** To stop one person farming the Free quota across many servers, someone can use screenshot reading in at most `FREE_SERVERS_PER_OWNER` (default 3) Free servers at a time, counting both the servers they own (`guild.owner_id`) and the ones where they run the OCR commands. Servers on a paid, gifted, code or trial plan don't count. A person's servers are taken in order of first OCR use, so the newest is the one refused, with a message pointing to `/premium`; a server drops out after 30 days without OCR. The default of 3 covers an R5 with a main and a farm alliance plus a spare; anyone with a real need for more (say an officer who reads screenshots for several alliances) can be gifted a plan on the extra servers, which takes them out of the count. Operators are exempt. Who used OCR where is kept in `FreeOcrLink (UserId, GuildId, Role, FirstAt, LastAt)`: just the Discord user IDs of the owner and the person running the command, for Free servers only; rows are deleted 30 days after their last use, and a server's rows go with `/data delete` (whole server) and the removal purge.

Everything else is free. **Limits are only enforced when `TIERS_ENFORCED=true`.** Until then nothing is blocked; the log records what would have been (`Tier check (not enforced)`, `OCR quota (not enforced)`, `Channel limit (not enforced)`, `Free-server cap (not enforced)`), so you can gift plans with `/ops grant` and check the log before switching enforcement on. Every grant, revoke and extend, every gift code created, revoked or redeemed, and every trial started or reset is recorded in `EntitlementAudit`.

**Free trial.** Each server can try **Command** free for 14 days, once, and each server owner gets one trial across all their servers. While `TIERS_ENFORCED` is on, it **starts by itself when the bot joins a new server** (`AUTO_TRIAL=true`, the default): the bot posts a welcome in the server's system channel (or the first text channel it can post in) saying what's unlocked, when it ends and what to run first. A server that has had the bot before, or whose owner already had a trial on another server, gets no trial and no post. The bot only sees joins while it's running, so a server added while it's down can still start the trial from `/premium`, like servers that joined before this. The owner is the server's owner when the trial starts (`TrialClaim.OwnerId`); operators (`BOT_OWNER_IDS`) are exempt, so test servers don't use up anything. Otherwise a server admin starts it with the **Start free 14-day Command trial** button on `/premium`, which only appears to members with Manage Server while the server can start one (and the click checks the permission again). A button rather than a `/premium trial` subcommand keeps `/premium` a single command and puts the offer next to what it unlocks. Starting it creates a `GuildEntitlement` with `Source='trial'`, `Tier='full'` and `EndsAt` 14 days out, records the server in `TrialClaim (GuildId, ClaimedAt, OwnerId)` and writes an `EntitlementAudit` row (`trial`), all in one transaction; `TrialClaim`'s key means two admins clicking at once still get one trial. `/premium` then shows "Command — trial until YYYY-MM-DD (N days left)". A server that already has an active Command plan (paid, gifted or from a code) is told the trial would add nothing and keeps it for later; a server on Alliance can start it and goes back to Alliance afterwards. Revoking a trial doesn't make the server eligible again, and neither does `/data delete` or the purge after the bot is removed: `TrialClaim` is kept, holding only the server ID, the start date and the owner's user ID. Only `/ops trial-reset` clears it. Three days before a trial ends the server gets a heads-up in its report channel listing what changes (screenshots, history, channels and features); there's no operator DM for trials. While `TIERS_ENFORCED` is off, trials are still recorded and shown, and `/premium` says a trial changes nothing yet.

**Gift codes.** Codes look like `ABCD-EFGH-JKMN-PQRS`: 16 random characters (80 bits) without I, L, O or U, and `/redeem` ignores case, spaces and dashes and reads O as 0 and I or L as 1. The `GiftCode` table keeps only a SHA-256 hash of each code (and its last four characters, for `/ops code list`). Each server can redeem a code once, and a redemption creates a `GuildEntitlement` with `Source='code'` and `ExternalId` set to the code's hash. Use counts and expiry are checked in the same transaction that records the redemption, so two servers redeeming the last use at once can't both get it. To stop codes being guessed, a user who makes 5 failed `/redeem` attempts within 15 minutes must wait before trying again.

### Planner
| Command | Description |
|---------|-------------|
| `/planner` | Post a button linking the [territory planner](https://lastz-territory-planner.pages.dev) web app |
| `/planner plan:<link>` | Repost a plan from the planner's **Share link** button so the channel can open it |

The planner is a separate static site (repo `territory-planner`, hosted on Cloudflare Pages); the bot only links to it. Plans live in the link itself, so the bot stores nothing. `plan` only accepts links on the planner's own address. Override the address with `PLANNER_URL`.

### Trivia
| Command | Description |
|---------|-------------|
| `/trivia start [mode] [questions] [seconds] [category] [difficulty]` | Start a match in this channel: 3–20 questions (default 10), 10–60 s each (default 20) |
| `/trivia join` | Join the open cross-server match from this channel |
| `/trivia stop` | Stop the match here. In a cross-server match only this channel leaves; the rest play on |
| `/trivia leaderboard [scope]` | All-time totals for this server, or `scope:Cross-server` |
| `/trivia settings [cross_server]` | Turn cross-server play on or off (Manage Server); no options shows the current settings |
| `/trivia reset` | Delete this server's trivia scores, with confirmation (Administrator) |

**Two ways to play, chosen by `mode`:**

- **This server** (default): the match runs in the channel where it was started, 5 seconds after the command.
- **Cross-server**: opens a lobby for 45 seconds. Other servers join with `/trivia join` (or `/trivia start mode:Cross-server`, which joins the open lobby instead of opening another), from their trivia channel if they've set one with `/setup` or any channel if not. Servers with a trivia channel get an invitation with a **Join** button there. Every joined channel then gets the same questions at the same moment, answers from all of them go into one scoreboard, and the results also total points per server. Only one lobby is open at a time, a match holds up to 20 channels, and each server can open a lobby once every 5 minutes so invitations can't be spammed.

Everyone in a joined channel can answer. Answers are buttons (A–D), so the bot still needs no Message Content intent; the first press counts and can't be changed, and the reply is visible only to the person who pressed. A correct answer scores 100 points plus up to 50 for speed, shrinking over the answer window. A question ends early once everyone who answered the previous question has answered this one, across every channel in the match. The first question always runs its full time, since nobody is known to be playing yet, and someone who skips a question isn't waited for on the next. Whoever started or joined the match in a channel, or anyone with Manage Messages there, can stop it. Matches live in memory: a restart ends them with a notice, and nothing is recorded for them.

Cross-server play shows players' Discord display names and server names to the other servers in the match and on the cross-server leaderboard. A server that turns it off with `/trivia settings cross_server:False` can't open or join cross-server matches and is left off that leaderboard.

**Plans.** Free servers get the 25 Last Z questions (heroes, factions, troop types, buildings) and play in their own server. Alliance adds the standard bank (89 general-knowledge questions across 8 categories). Command adds the extended bank and cross-server play: opening, joining and being invited to cross-server matches; a cross-server match asks the host's questions. `/premium` shows each plan's question count, and the category list only offers the server's own categories. While `TIERS_ENFORCED` is off, every server gets every question and cross-server play.

**Questions** are bundled with the bot (no third-party service at run time): our own in `bot/trivia/questions.json`, and Command's extended bank in `bot/trivia/questions_opentdb.json`, a filtered copy of [Open Trivia DB](https://opentdb.com). Each entry's `tier` is the cheapest plan that gets it (`free`, `mid` for Alliance, `full` for Command; untagged means `mid`).

**Open Trivia DB credit.** Its questions are under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), which needs credit where players see them and keeps our edited copy under the same license. So each one carries `"source": "opentdb"`, which adds "via Open Trivia DB (CC BY-SA 4.0)" to its footer; `/help`, the site's home page and its Terms carry the full credit with links; and the file starts with the source, license, retrieval date and what we changed. Don't add terms that stop people reusing those questions. Our own questions aren't affected.

**Refreshing it.** `.venv/bin/python scripts/import_opentdb.py --save-raw /tmp/opentdb_raw.json` fetches every verified question (about ten minutes: the API allows one request per 5 seconds), keeps the ones that work as Discord buttons (answers up to 76 characters, no "all of the above" or picture questions, no repeats of our own), renames the categories (e.g. "Entertainment: Video Games" becomes Gaming) and rewrites the file in a stable order, so the diff shows only real changes. `--from-raw` reruns the conversion on a saved download. To drop a question for good, add `{"id": "<id>", "question": "...", "reason": "..."}` to `bot/trivia/opentdb_exclude.json` (the id is `_question_id` in `bot/trivia/questions.py`) and run it again. Each server remembers its last 300 questions asked: unasked ones come first, and once a server has seen them all, the ones asked longest ago are reused first, so a small bank cycles through every question before repeating one. To use your own bank, set `TRIVIA_QUESTIONS_PATH` to a JSON file in the same format (one to three wrong answers per question, so true/false works; a bank that tags nothing `free` is used whole on every plan). The file is checked at startup; if it's missing or any entry is invalid, the error is logged and the bundled questions are used instead.

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

### Data retention and deletion

When the bot is removed from a server (kicked, banned, or the server deleted), that server's metrics are **kept for `DATA_RETENTION_DAYS` (default 30) days**, then deleted. Adding the bot back within that window cancels the deletion and all history is still there, so removing it by accident loses nothing.

**Subscriptions extend this.** While the server has an active subscription (paid, gifted or trial, from `GuildEntitlement`), its data is kept however long the bot has been gone. The 30 days start from the later of the removal and the end of the last subscription, so a subscription that lapses while the bot is removed starts the clock on the day it lapses.

`PendingPurge (GuildId, RemovedAt)` records removals; the deletion date is worked out from it and the server's subscriptions at each hourly check, so renewals and cancellations apply without rescheduling. A backup is taken before each scheduled deletion, and if the backup fails the deletion waits for the next hour. Servers that removed the bot while it was offline are found at startup and treated as removed from then. `/ops purges` lists removed servers and their deletion dates ("kept: subscribed" while a subscription is active).

Server admins can delete data immediately with `/data delete` (one channel, or the whole server), and download it first with `/data export`. Exports hold only metrics (`WeeklyMetrics`); trivia scores, settings, entitlements and usage counters aren't included. For a deletion or access request, `/ops export` gives the operator the full history of any server.

Trivia keeps totals per player per server in `TriviaScore (GuildId, UserId, Mode, DisplayName, GuildName, Points, Correct, Answered, Games, Wins)`, with `Mode` `server` or `global` (a cross-server player's points count for the server they played from), and the cross-server switch in `TriviaSettings (GuildId, AllowGlobal)`; the trivia channel is in `GuildSettings` (see `/setup`). All three follow the same removal retention as metrics. `/trivia reset` deletes a server's trivia scores; `/data delete` doesn't touch them.

Deleted data still exists in backups until they age out, at most `BACKUP_MAX_AGE_DAYS` (default 30) days after the deletion, while the bot is running. So with the defaults, a removed server's data is fully gone within about 60 days of the bot's removal (30 days' retention plus 30 days of backups), or 30 days after `/data delete`. Usage counters (`UsageLedger`) are not deleted; they hold only per-day counts, no player data. Nor are subscription records (`GuildEntitlement`, `EntitlementAudit`) or `TrialClaim` (server ID, trial start date and the owner's user ID), which keeps the free trial to once per server and once per owner. `FreeOcrLink` (who used OCR in which Free server, for the Free-server cap) is deleted with the server's data, and otherwise 30 days after its last use.

OCR usage is counted per server in `UsageLedger (GuildId, Day, Kind, Amount)`, one row per UTC day per measure: `ocr_batches`, `ocr_images` (every image sent to the vision model, failed ones included), `ocr_failed`, `ocr_seconds` and `ocr_wait_seconds` (time batches spent queued behind other batches). `/ops usage` reports it. The weekly screenshot quota reads it (when tiers are enforced); the per-user rate limit and Free-server cap are separate (see Ingestion and Plans) and `/ops abuse` shows them.

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

## Custom emojis

`assets/emojis/` holds the bot's own emoji art (128x128 PNGs, listed with previews in its `README.md`), all drawn by `scripts/make_emojis.py`. They're uploaded to the bot's application, not to any server, so the bot can use them everywhere. At startup it fetches them by name (`bot/utils/emojis.py`). Reports put a short headline above their code block, with metric icons, a 1-2-3 podium and growth arrows. Custom emojis don't render inside code blocks, so the tables stay plain. If an emoji is missing, the headline drops it or uses a standard emoji instead.

To add or redraw emojis, edit the generator, rerun it, then upload:

```bash
.venv/bin/python scripts/make_emojis.py
.venv/bin/python scripts/upload_emojis.py --dry-run
.venv/bin/python scripts/upload_emojis.py              # new names only
.venv/bin/python scripts/upload_emojis.py --replace kills,zombie   # redrawn ones
```

The upload uses `DISCORD_TOKEN` from `.env` (`--env-file .env.testing` for the testing bot). Restart the bot afterwards to pick up new IDs.

## Discord SKUs

The paid plans are sold as two guild-subscription SKUs (Alliance, Command). Discord's API can list SKUs but not create or edit them, so they're kept by hand in the Developer Portal (Monetization → Manage SKUs). `bot/utils/skus.json` records what each one should be: SKU ID, name, monthly price, description and up to 6 benefits. Each benefit names the tier limits and features it promises, and the tests fail if those stop matching `bot/utils/tiers.py`, if the text breaks Discord's length limits, or if the site's prices differ.

To change a listing, edit `skus.json` first, then copy it into the portal and check:

```bash
.venv/bin/python scripts/sync_skus.py portal                 # what to type into the portal
.venv/bin/python scripts/sync_skus.py                        # portal matches skus.json?
.venv/bin/python scripts/sync_skus.py --require-published    # ...and both are on sale
.venv/bin/python scripts/sync_skus.py entitlements grant --guild <server id> --tier mid
.venv/bin/python scripts/sync_skus.py entitlements list
.venv/bin/python scripts/sync_skus.py entitlements remove --id <entitlement id>
```

The check sees only names, types and published state; price, description and benefits are compared by eye. A published SKU has to be unpublished to edit anything but its price, and a price change applies to new subscribers only. Test entitlements give a server a SKU without paying; the bot doesn't act on Discord entitlements yet.

## Policy site

`site/` holds the public website: a home page, the Privacy Policy (`privacy.html`) and the Terms of Service (`terms.html`). It's plain static HTML with no build step, published as the Cloudflare Pages project **`lastz-assistant`** (`https://lastz-assistant.pages.dev`). Pages serves `privacy.html` at `/privacy`; use those URLs in the Discord Developer Portal (General Information → Privacy Policy URL and Terms of Service URL).

**Before publishing**, replace every highlighted placeholder (`[OPERATOR NAME]`, `[CONTACT EMAIL]`, `[COUNTRY]`). This lists any that are left:

```bash
grep -rn 'class="todo"' site/
```

**Deploy:** either connect this repo in the Cloudflare dashboard (Workers & Pages → Create → Pages → Connect to Git, framework preset *None*, no build command, build output directory `site`), or upload directly:

```bash
npx wrangler pages deploy site --project-name lastz-assistant
```

**Keep it in step with the bot.** The policy describes real behaviour (30-day retention after removal, subscriptions extending it, 30-day backups, size-limited logs). `tests/test_site.py` fails if the retention defaults change without the policy changing too; update the "Last updated" date with any edit.

## Tests

```bash
python -m unittest discover -s tests
```

The suite includes `tests/test_vision_samples.py`, which runs every screenshot in `samples/` through the configured vision model (the same entry point and prompts the bot uses) and checks the extracted names and values. It reads `OCR_VISION_*` from `.env` and swaps `host.docker.internal` for `127.0.0.1` when run outside Docker (or set `OCR_VISION_TEST_BASE_URL`). If the server is unreachable those tests are skipped with a reason; set `RUN_VISION_TESTS=0` to skip them deliberately. Everything else runs offline.

### End-to-end tests in Discord

`.claude/skills/discord-e2e/SKILL.md` lets Claude Code test the bot in real Discord through its built-in browser pane, signed in as you, in the control server's test channel only. It runs `/help`, `/ingest batch` (with and without the @mention), the `/ops` views and a clean-up, then checks each result against the bot's logs and database. Uploads use the small fixture `tests/e2e/general_profile_small.jpg` (PrincessPea, HQ 24, Power 65.4M). Ask Claude to "run the Discord end-to-end tests"; sign in yourself if asked, with the QR code from the Discord app.

## Project layout

```
bot/
  main.py           # entrypoint
  config.py         # env settings
  cogs/             # admin, health, help, ingest, ops, planner, reports, trivia
  db/               # SQLite helpers
  ocr/              # Vision-model OCR (oMLX) and response parsing
  reporting/        # markdown + matplotlib
  trivia/           # question bank (questions.json) and match scoring
  utils/            # logging, parsing
data/               # placeholder only; the database lives in BOT_DATA_DIR
Dockerfile
docker-compose.yml
```

## Discord developer checklist

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot
2. Copy token → `DISCORD_TOKEN`
3. Privileged Gateway Intents: leave all **off** (the bot requests none)
4. OAuth2 URL Generator: scopes `bot` + `applications.commands`
5. Permissions: Send Messages, Embed Links, Attach Files, Read Message History, Use Application Commands
6. Commands register globally on every start. If they are missing or listed twice, run `/ops sync` in the control server. Set `DEV_GUILD_ID` only on a development bot: commands then go to that one server instead, for instant updates
