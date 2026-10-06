# Monetization Plan — LastZ Assistant

Status: draft, 2026-10-05. Nothing here is implemented yet.

## 1. Where the bot is today

The bot already keeps each server's data separate (`GuildId` + `ChannelId` on every row), so it can serve many servers from one database. It is still built to be run by one operator for one or two servers, though:

| Area | Current state | Why it matters for a paid product |
|---|---|---|
| Hosting | Docker on a home Mac Studio; OCR on local oMLX | Uptime depends on the Mac being awake and online. Paying customers expect more. |
| OCR capacity | One queue for the whole bot, `OCR_MAX_CONCURRENCY=1` | OCR is the only real cost and the bottleneck. On reset day every alliance uploads at once. |
| Operator commands | `/admin reload`, `/admin sync scope:all/global`, `/admin backup` only check **server** Administrator | Once the bot is public, any server admin can reload cogs, resync the whole bot, or fill your backup disk. |
| Per-server settings | None; all settings are global env vars (the bot-wide auto-OCR channel was removed 2026-10-05) | Scheduled reports and role-based access will need per-server settings. |
| Usage tracking | None | You can't enforce quotas or price on usage you don't measure. |
| Data lifecycle | No delete command; nothing cleans up when the bot is removed from a server | Needed for a privacy policy, for downgrades, and for Discord verification. |
| Legal | No Terms of Service or Privacy Policy | Discord requires both for verification and for Premium Apps. |

## 2. Missing requirements

These are gaps that must be closed before or alongside charging money. **P0** means blocking.

### P0: safety and multi-tenancy
1. ✅ *Done 2026-10-05.* **Split operator and server-admin permissions.** Add `BOT_OWNER_IDS` (or use the application owner/team). Move `reload`, `sync all/global`, `backup` and all entitlement commands into an owner-only `/ops` group. Sync that group **only** to a private control server (`CONTROL_GUILD_ID`) so it never shows up in customer servers, and check owner identity again inside every handler.
2. ✅ *Done 2026-10-06: `/setup` (Manage Server) sets a trivia channel, a report channel (`ReportChannelId`; `/ops grant notify:true` posts there and tells the operator when it can't; `bot.utils.report_channel.send_to_report_channel` for gift expiry heads-ups and scheduled reports) and a default week (`DefaultWeek`: current or last, used by `/add`, `/ingest` and `/report week`). Locale left out: the bot has no translations and nothing reads one.* **Per-server settings table** (`GuildSettings`): default week, locale, and a "report channel" for scheduled posts and gift notices. Add a `/setup` command for server admins.
3. ✅ *Counting done 2026-10-05 (`UsageLedger`, `/ops usage`); enforcement comes with tiers.* **Usage metering** (`UsageLedger`): per-server counters per UTC day for OCR batches, images sent to the model, failures, OCR seconds and queue-wait seconds, written once each batch finishes. Daily rows add up to weekly quotas and also show peak days. This drives quotas and tells you your real costs.
4. ✅ *Fair turns done 2026-10-05 (`bot/utils/fair_queue.py`): servers take turns one whole request at a time. Priority lanes done 2026-10-06: smooth weighted round-robin across tier levels (see §3 notes); inactive until `TIERS_ENFORCED` is on.* **Fair OCR queue.** Replace the single FIFO semaphore with per-guild queues and a scheduler that serves guilds round-robin, with a priority lane for paid tiers. Otherwise one free server's 50-image zip blocks a paying customer.
5. ✅ *Written 2026-10-05 in `site/` (operator LastZ Assistant, lastzassistant@gmail.com, governed by Pennsylvania law); reviewed, live on the `lastz-assistant` Cloudflare Pages project, and linked in the Developer Portal 2026-10-06.* **Privacy Policy and Terms of Service**, published at stable URLs. They need to cover what is stored (player names and game stats, *not* Discord user data), how long it's kept, and how to request deletion.
6. ✅ *Done 2026-10-05: 30-day retention after removal (`DATA_RETENTION_DAYS`), cancelled if the bot is re-added; kept indefinitely while a subscription (paid, gift or trial) is active, with the 30 days starting when it ends; `/data delete` for admins; `/ops purges`. Backups are capped at 30 days (`BACKUP_MAX_AGE_DAYS`), so deleted data is gone from them within 30 days.* **Data deletion:** `/data delete` (server admin, with confirmation) and a removal job. When the bot leaves a guild (`on_guild_remove`), mark the guild and purge its data after a grace period (e.g. 30 days).
7. ✅ *Done 2026-10-05: prefix commands retired, `/ingest batch` collects only messages that @mention the bot, and the bot no longer requests the intent.* **Remove the Message Content intent dependency.** Discord requires approval for this privileged intent once a bot is in 100+ servers, and verification starts at 75. The auto-OCR channel has been dropped, so two things still depend on it:
   - `/ingest batch` reads images and `done` from ordinary messages. Change it to collect only messages that @mention the bot, which Discord delivers without the intent.
   - Prefix commands (`!addversus`, `!ingestimage`, …). Retire them in favour of slash commands, or keep them only for messages that @mention the bot.

   Then set `MESSAGE_CONTENT_INTENT=false` by default and turn the intent off in the Developer Portal.

### P1: commercial operations
8. **Payment and entitlement integration.** See §4.
9. **Tier enforcement layer:** feature gates, quotas, and upgrade prompts (§5).
10. **Downgrade and grace rules.** Data is never deleted on downgrade. History beyond the tier's window is hidden, not dropped, for at least 90 days. Payment failure gives a 7-day grace period at the paid tier.
11. ✅ *Done 2026-10-06: `/data export` (Administrator, like `/data delete`; `export` feature on Alliance and Command) sends this channel's or the whole server's metrics as CSV or JSON, optionally limited to a week range, ephemerally, zipped if over the server's upload limit. `/ops export` gives the operator any server's full history, whatever its plan, even after the bot was removed (until the purge). Exports hold `WeeklyMetrics` only: it's the alliance's own game data and what "take my data elsewhere" means. Left out: trivia scores (members' Discord user IDs; a server admin shouldn't get a bulk list of members' IDs, and they're covered by `/trivia reset` and by contacting us), `GuildSettings` (just channel IDs set with `/setup`), entitlements and usage counters (billing and operations records, not the server's data). **History window:** `/data export` shows the same weeks as reports, leaving out weeks older than the plan's window with the same 🔒 note. Otherwise an export would be a way round the window: Alliance shows 26 weeks, and its export would hand over everything. Hidden weeks aren't lost (upgrading shows them, and a downgrade never deletes them), and a server asking for deletion or a full copy gets it from the operator with `/ops export`, which ignores the window so access and deletion requests always cover all data.* **Per-guild export** (CSV/JSON) to make deletion and "take my data elsewhere" requests easy to fulfil. Also a paid feature.
12. **Hosting resilience.** At minimum: a monitored uptime check, alerts on OCR server failure, and an off-machine backup copy (iCloud sync already helps). Longer term: run the bot process on a small VPS and keep the Mac as a remote OCR worker, or add a cloud vision fallback for paid tiers when the Mac is offline.
13. **Support channel:** a support Discord server, linked from `/help` and `/premium`.
14. 🟡 *Tools built 2026-10-06 (`/ops capacity`, `scripts/benchmark_ocr.py`). The numbers aren't in yet: the operator reads them after a few weeks of real uploads, reset days included, and sets the quotas (open decision 3).* **Capacity benchmark.** Measure seconds per image on the Mac Studio and peak demand on reset day. Quotas in §3 are placeholders until this is done.

   **Method.** Two measurements and one sum.
   - *Cost per image.* `scripts/benchmark_ocr.py --runs 5` sends the sample screenshots in `samples/` and `tests/e2e/` to the configured OCR server (same model, prompts and retries as the bot) and prints seconds per image (mean, p50, p95) and images per hour. It never touches the bot's database. Add `--concurrency 2` to see whether a second slot really adds images per hour on this machine before raising `OCR_MAX_CONCURRENCY`. The samples are clean screenshots, so treat the result as a lower bound. The real cost comes from the ledger: `ocr_seconds` is the time each batch held a slot, so it includes retries, failed images and the size of the screenshots people actually upload.
   - *Demand.* `/ops capacity days:28` reads `UsageLedger` for the window: images, batches, OCR seconds per image overall and per server, failure rate, average images per weekday (days without uploads count as zero), the five busiest days, and the queue wait (the average per batch, and the worst day's average; the ledger keeps daily totals, so single-batch peaks don't show). The **peak-day share** is the busiest weekday's share of an average week. It is 1/7 (14%) if demand were even and close to 100% if every alliance uploads on reset day.
   - *The sum* (`bot/utils/capacity.py`, `estimate_capacity`). One slot handles `86,400 / s` images a day, where `s` is seconds per image; `× 7` is the "one slot, flat out" weekly figure. Usable capacity on the peak day is `slots × 86,400 / s × utilization`. The default utilization is 70%: uploads bunch up after the reset, the Mac does other work, and a full queue means long waits. A server that uses its whole weekly quota `q` is assumed to follow the observed pattern, so it sends `q × share` images on the peak day. Servers that fit = usable peak-day images ÷ `(q × share)`, given separately for Free (25), Alliance (250) and Command (1,000), each read from `TierPolicy`.

   **How to read it.**
   - Use 4 weeks or more (`days:28`) so each weekday appears several times. A window with no uploads yet has no peak-day share; the command then assumes 100% (a whole week in one day) and says so.
   - `seconds_per_image:<n>` swaps in another OCR cost, such as the benchmark's mean or p95, or a guess for a faster machine. Use the ledger's figure when it disagrees: it reflects real uploads.
   - "Busiest day so far kept the slots X% busy" is current headroom. When it nears the utilization you chose, reset day will start to queue for real.
   - The per-tier server counts are each "if every server were on that tier". A mix adds up by weight: one Command server at full quota uses as much as 4 Alliance or 40 Free servers. Few servers use their whole quota, so the counts are a floor on how many you can sell.
   - A per-server s/img well above the rest usually means large or unusual screenshots. A high failure rate inflates s/img, because failed images still use the slot.
   - Change the quotas in §3 (`TierPolicy`) only from these numbers, and keep the fallback in the §3 notes in mind: if one Mac can't carry 1,000 images a week per Command server at the peak, cap Command lower or add a cloud fallback before selling more.

### P2: product and growth
15. `/premium` command showing the server's tier, usage this week, renewal date, and an upgrade button.
16. ✅ *Done 2026-10-06: gated commands are read from their `requires_feature` checks; marks and a `/premium` footer show only while tiers are enforced.* `/help` marks locked commands with 🔒 and the tier that unlocks them.
17. Trials: a 14-day Full trial once per guild, recorded as an entitlement with source `trial`.
18. Abuse controls: per-user rate limits on ingest, and a per-owner cap on free servers to stop people farming free quota across many servers.
19. Check the game publisher's terms on commercial companion tools that process game screenshots.
20. Tax and business setup: Discord acts as merchant of record for Premium Apps. If you use Stripe directly you handle sales tax/VAT yourself (Stripe Tax can help).

## 3. Tiers

The unit of sale is **one Discord server**, which is usually one alliance. Alliance leaders (R5/R4) are the buyers, and they often pool money, so a low monthly price with a cheaper annual option suits this audience. Prices are in USD. They are pitched at the range of other mobile-game companion bots and should be validated with 3–5 alliance leaders before launch.

| | **Free** | **Mid — "Alliance"** | **Full — "Command"** |
|---|---|---|---|
| Price | $0 | **$4.99/mo** or **$49/yr** | **$9.99/mo** or **$99/yr** |
| OCR images / week | 25 (roughly one dataset for a small alliance) | 250 | 1,000 (fair use) |
| OCR queue priority | Standard | Priority | Highest |
| Tracked channels (datasets) | 1 | 3 | Unlimited |
| History visible in reports | 4 weeks | 26 weeks | Unlimited |
| Manual `/add`, `/ingest text` | ✅ | ✅ | ✅ |
| `/ingest image` (≤10) | ✅ | ✅ | ✅ |
| `/ingest zip`, `/ingest batch` | — | ✅ | ✅ |
| `/report week`, `versus`, `tech`, `leaderboard` | ✅ | ✅ | ✅ |
| `/report player`, `trend`, `growth` | — | ✅ | ✅ |
| PNG charts | Watermarked | ✅ | ✅ |
| Mix-up flags (plausibility checks) | ✅ | ✅ | ✅ |
| `/admin duplicates`, `rename-player` | — | ✅ | ✅ |
| CSV/JSON export | — | ✅ | ✅ |
| Scheduled weekly report post *(new)* | — | — | ✅ |
| Server-wide (multi-channel) scope reports | — | — | ✅ |
| Role-based command access *(new)* | — | — | ✅ |
| Cross-server alliance linking *(new, later)* | — | — | ✅ |
| `/planner` link | ✅ | ✅ | ✅ |

Notes:
- **Every upload goes through a slash command that names the dataset.** There is no auto-OCR channel; it saved only a few keystrokes once image-type detection was removed, and it depended on the Message Content intent.
- **The channel limit never deletes data.** A write that would start data in a channel past the limit is refused, naming the channels that can take it. A server over its limit (after a downgrade) keeps every channel in reports, and the most recently written channels (by latest `UpdatedAt`) stay writable, up to the limit. Most-recent-first follows what the alliance is actively using, needs no extra setting, and stays stable because read-only channels can't move up the order. The cost: a rename or channel reassignment updates rows too, so it counts as use.
- **Keep data-quality features free.** Mix-up flags and manual entry make free data trustworthy, and trustworthy history is what makes people pay to see more of it.
- **Retention limits hide history, they don't delete it.** Upgrading reveals all history at once, which is a strong conversion moment.
- **OCR queue priority is a weighted share, not a hard jump.** Free is Standard, Mid is Priority and Full is Highest. When a slot frees up, the queue first picks a level by smooth weighted round-robin (weights 1 : 2 : 4), then the server in that level whose last turn was longest ago. Servers still take turns one whole request at a time. While every level has work waiting, every 7 turns go 4 to Full, 2 to Mid and 1 to Free, spread out rather than bunched. Free never gets less than a 1-in-7 share (1-in-5 against Full alone), so paid load can slow Free servers but never starve them. We chose this over strict priority with an aging bound because its guarantee is counted in turns. An aging timer would have to be tuned against requests that range from one image to a 50-image zip, and when it fires, a backlog of aged Free requests would jump ahead of paid ones all at once. A level with nothing waiting builds up no credit. The level is looked up from the server's tier when a request is queued. With `TIERS_ENFORCED` off, every server is Standard and the queue behaves exactly as before. Users still only see how many of their own server's requests are ahead (priority never reorders one server's requests). `/ops queue` shows each waiting server's lane.
- **OCR quota is the main cost lever.** Tune the numbers once the capacity benchmark (§2 item 14) is done. If one Mac can't handle 1,000 images per week per Full server at peak, cap Full at a lower number or add a cloud fallback before selling more.
- Possible launch offer: "Founding Alliance" Full annual at $79 for the first 20 servers, in exchange for feedback.

## 4. Payments

**Recommended: Discord Premium Apps (server subscriptions) as the main channel**, with a provider-neutral entitlement table so gifts, trials and any future Stripe/Patreon customers all go through the same code.

- Create two guild-subscription SKUs (Alliance, Command) in the Developer Portal. Annual pricing depends on what Discord supports for the SKU type; if it isn't available, offer annual through Stripe.
- Listen to `on_entitlement_create` / `on_entitlement_update` / `on_entitlement_delete`. On startup, reconcile with `fetch_entitlements` so missed events don't leave a server on the wrong tier.
- Show the upgrade button with Discord's premium/SKU button component.
- Requirements: a verified app, a team-owned application, ToS and Privacy URLs, and an eligible payout country. Check Discord's current revenue share and policies at signup.
- Fallback if you aren't eligible or prefer it: Stripe Checkout plus a webhook (needs a small always-on HTTPS endpoint, so not the home Mac), or Patreon tiers linked by guild ID. Both write into the same `GuildEntitlement` table.

## 5. Entitlement model and enforcement

### Schema

```sql
CREATE TABLE GuildEntitlement (
  Id          INTEGER PRIMARY KEY,
  GuildId     TEXT NOT NULL,
  Tier        TEXT NOT NULL,          -- 'mid' | 'full'
  Source      TEXT NOT NULL,          -- 'discord' | 'stripe' | 'gift' | 'trial' | 'code'
  ExternalId  TEXT,                   -- Discord entitlement id / Stripe sub id / gift code
  StartsAt    TEXT NOT NULL,
  EndsAt      TEXT,                   -- NULL = no expiry (permanent gift)
  GrantedBy   TEXT,                   -- operator user id for gifts
  Reason      TEXT,
  RevokedAt   TEXT,
  CreatedAt   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_ent_guild ON GuildEntitlement (GuildId, RevokedAt, EndsAt);

-- Built 2026-10-05; see bot/db/database.py.
CREATE TABLE UsageLedger (
  GuildId TEXT, Day TEXT,  -- UTC YYYY-MM-DD
  Kind TEXT,               -- ocr_batches | ocr_images | ocr_failed | ocr_seconds | ocr_wait_seconds
  Amount REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (GuildId, Day, Kind)
);

CREATE TABLE EntitlementAudit (
  Id INTEGER PRIMARY KEY, At TEXT DEFAULT (datetime('now')),
  ActorId TEXT, Action TEXT, GuildId TEXT, Detail TEXT
);
```

The **effective tier** is the highest tier among a guild's active entitlements (not revoked, `StartsAt <= now`, and `EndsAt` empty or in the future), falling back to free. Because of this, a gift and a paid subscription can overlap without conflict, and a lapsed subscription falls back to the gift automatically.

The `GuildEntitlement` table already exists (created 2026-10-05 for data retention); billing and gifting still need to write to it.

### Code layout
- `bot/utils/tiers.py`: a `TierPolicy` dataclass per tier (quotas, retention weeks, feature flags) and `async effective_tier(guild_id)` with a cache of about 60 seconds, cleared when an entitlement event arrives.
- Decorator `@requires_feature("trend")` for slash commands. It replies with an ephemeral upgrade message and does not run the command.
- `reserve_ocr_quota(guild_id, n)` is called before queueing. It sums `ocr_images` since the start of the quota week. It returns how many images are allowed, and the ingest summary explains any that were skipped.
- Report queries take `min_week` from the tier's retention window, applied in one place in `database.py`.
- Tests: one table-driven test per gate, plus quota edge cases (a batch that crosses the limit, a week rollover, a failed OCR refunding its quota).

## 6. Gifting full tiers (operator-only)

Gifts are entitlement rows with `Source='gift'`. They work independently of Discord billing and cost nothing.

### Commands
In the `/ops` group, which is synced only to `CONTROL_GUILD_ID` and checked against `BOT_OWNER_IDS` in every handler:

| Command | Effect |
|---|---|
| `/ops grant guild_id:<id> tier:full duration:<30d\|90d\|1y\|permanent> reason:<text> [notify:true]` | Inserts a gift entitlement. If `notify` is set, posts a thank-you embed in the server's configured report channel. |
| `/ops revoke guild_id:<id> [entitlement_id]` | Sets `RevokedAt`. Never deletes the row. |
| `/ops extend entitlement_id:<id> duration:<…>` | Moves `EndsAt` later. |
| `/ops show guild_id:<id>` | Effective tier, all entitlements with their sources, usage this week, server name and member count. |
| `/ops list [source:gift] [expiring_within:14d]` | Lists active gifts, for review and renewal. |
| `/ops code create tier:full duration:90d uses:1 [expires]` | Creates a redeemable code for giveaways and partners. Shown once; only its hash is stored (table `GiftCode`). |
| `/ops code list` / `/ops code revoke code_id:<n>` | Lists codes with their uses; stops one being redeemed (optionally revoking plans already redeemed). |
| `/redeem code:<code>` *(server admin, public)* | Claims a code for the current server, creating an entitlement with `Source='code'`. |

### Rules
- Every grant, revoke, extend and redeem writes to `EntitlementAudit`, recording who did it and why.
- `guild_id` autocompletes from the servers the bot is in. Granting to a guild the bot isn't in is allowed (pre-provisioning) but shows a warning.
- A daily task DMs the operator about gifts expiring within 7 days and posts a heads-up in the gifted server 3 days before expiry.
- Gifts show in `/premium` as "Full — gifted until 2027-01-01", or "Full — gifted" when permanent.
- Your own alliance's server gets a permanent Full gift at migration time, so nothing changes for current users.

Why not Discord test entitlements? They are meant for testing, and creating them for real gifts is outside their intended use. Keeping gifts in your own table also keeps them auditable and independent of the payment provider.

## 7. Rollout

| Phase | Scope | Exit criteria |
|---|---|---|
| **0. Harden** (P0 items 1–7) | Owner-only `/ops`, `GuildSettings` + `/setup`, usage metering (counting only, no enforcement), fair queue, deletion, ToS/Privacy, `/ingest batch` via @mention, intent off | No server admin can affect another server; a week of real usage data per guild |
| **1. Entitlements without billing** 🟡 *Built 2026-10-06, with `TIERS_ENFORCED` off by default (the operator turns it on later): tiers, feature checks, weekly OCR quota, `/premium`, `/ops grant/revoke/extend/show/list`; history windows (4 / 26 / all weeks via `TierPolicy.history_weeks`, one `min_week` filter in `database.py`, a footer naming hidden weeks); channel limit (1 / 3 / unlimited channels with data; over the limit after a downgrade, the most recently written channels stay writable and the rest are read-only, never deleted); chart watermark on Free (`clean_charts` flag on Alliance and Command; drop it if open decision 4 goes the other way); OCR priority lanes (weighted round-robin, Standard/Priority/Highest); gift codes (`/ops code create/list/revoke`, `/redeem`: hashed codes, use limits, expiry, one redemption per server, rate-limited); gift expiry reminders (operator DM at 7 days, server heads-up in the report channel at 3 days, sent once per end date); `/data export` (`export` flag on Alliance and Command, follows the history window; `/ops export` for the operator). Still to do: permanent Full gift for the operator's own server, friendly-alliance trial, then switching enforcement on.* | `GuildEntitlement`, tier policy, gates and quotas, `/premium`, gifting commands, permanent Full gift for existing servers | Gates covered by tests; your own server unaffected; 2–3 friendly alliances on gifted Full and Mid |
| **2. Billing** | Discord SKUs and entitlement events, reconciliation on startup, downgrade/grace handling, trials | A test purchase upgrades, cancels and downgrades a server correctly |
| **3. Launch** | Support server, App Directory listing, founding-alliance offer | First 10 paying servers |
| **4. Full-tier extras** | Scheduled weekly report, role-based access, cross-server alliance view, cloud OCR fallback | Driven by what paying servers ask for |

## 8. Open decisions

1. Discord Premium Apps vs Stripe. This depends on eligibility, revenue share and whether annual plans are available.
2. Whether to keep everything on the Mac Studio or move the bot process to a VPS before taking money.
3. Final quota numbers after the capacity benchmark.
4. Whether watermarked charts on Free are worth the goodwill cost, versus not offering charts on Free at all.
