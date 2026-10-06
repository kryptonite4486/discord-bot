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
2. **Per-server settings table** (`GuildSettings`): default week, locale, and a "report channel" for scheduled posts and gift notices. Add a `/setup` command for server admins.
3. ✅ *Counting done 2026-10-05 (`UsageLedger`, `/ops usage`); enforcement comes with tiers.* **Usage metering** (`UsageLedger`): per-server counters per UTC day for OCR batches, images sent to the model, failures, OCR seconds and queue-wait seconds, written once each batch finishes. Daily rows add up to weekly quotas and also show peak days. This drives quotas and tells you your real costs.
4. ✅ *Fair turns done 2026-10-05 (`bot/utils/fair_queue.py`): servers take turns one whole request at a time. Priority lanes done 2026-10-06: smooth weighted round-robin across tier levels (see §3 notes); inactive until `TIERS_ENFORCED` is on.* **Fair OCR queue.** Replace the single FIFO semaphore with per-guild queues and a scheduler that serves guilds round-robin, with a priority lane for paid tiers. Otherwise one free server's 50-image zip blocks a paying customer.
5. 🟡 *Written 2026-10-05 in `site/` (operator LastZ Assistant, lastzassistant@gmail.com, governed by Pennsylvania law) for the `lastz-assistant` Cloudflare Pages project; needs a review, publishing, and the URLs added in the Developer Portal.* **Privacy Policy and Terms of Service**, published at stable URLs. They need to cover what is stored (player names and game stats, *not* Discord user data), how long it's kept, and how to request deletion.
6. ✅ *Done 2026-10-05: 30-day retention after removal (`DATA_RETENTION_DAYS`), cancelled if the bot is re-added; kept indefinitely while a subscription (paid, gift or trial) is active, with the 30 days starting when it ends; `/data delete` for admins; `/ops purges`. Backups are capped at 30 days (`BACKUP_MAX_AGE_DAYS`), so deleted data is gone from them within 30 days.* **Data deletion:** `/data delete` (server admin, with confirmation) and a removal job. When the bot leaves a guild (`on_guild_remove`), mark the guild and purge its data after a grace period (e.g. 30 days).
7. ✅ *Done 2026-10-05: prefix commands retired, `/ingest batch` collects only messages that @mention the bot, and the bot no longer requests the intent.* **Remove the Message Content intent dependency.** Discord requires approval for this privileged intent once a bot is in 100+ servers, and verification starts at 75. The auto-OCR channel has been dropped, so two things still depend on it:
   - `/ingest batch` reads images and `done` from ordinary messages. Change it to collect only messages that @mention the bot, which Discord delivers without the intent.
   - Prefix commands (`!addversus`, `!ingestimage`, …). Retire them in favour of slash commands, or keep them only for messages that @mention the bot.

   Then set `MESSAGE_CONTENT_INTENT=false` by default and turn the intent off in the Developer Portal.

### P1: commercial operations
8. **Payment and entitlement integration.** See §4.
9. **Tier enforcement layer:** feature gates, quotas, and upgrade prompts (§5).
10. **Downgrade and grace rules.** Data is never deleted on downgrade. History beyond the tier's window is hidden, not dropped, for at least 90 days. Payment failure gives a 7-day grace period at the paid tier.
11. **Per-guild export** (CSV/JSON) to make deletion and "take my data elsewhere" requests easy to fulfil. Also a paid feature.
12. **Hosting resilience.** At minimum: a monitored uptime check, alerts on OCR server failure, and an off-machine backup copy (iCloud sync already helps). Longer term: run the bot process on a small VPS and keep the Mac as a remote OCR worker, or add a cloud vision fallback for paid tiers when the Mac is offline.
13. **Support channel:** a support Discord server, linked from `/help` and `/premium`.
14. **Capacity benchmark.** Measure seconds per image on the Mac Studio and peak demand on reset day. Quotas in §3 are placeholders until this is done.

### P2: product and growth
15. `/premium` command showing the server's tier, usage this week, renewal date, and an upgrade button.
16. `/help` marks locked commands with 🔒 and the tier that unlocks them.
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
| `/ops code create tier:full duration:90d uses:1 [expires]` | Creates a redeemable code for giveaways and partners. |
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
| **1. Entitlements without billing** 🟡 *slice 1 built 2026-10-06: tiers, feature checks, weekly OCR quota, `/premium`, `/ops grant/revoke/extend/show/list`, `TIERS_ENFORCED` off by default. Priority lane built 2026-10-06 (weighted round-robin, Standard/Priority/Highest). Still to do: history windows, channel limit, chart watermark, codes/`/redeem`, expiry reminders* | `GuildEntitlement`, tier policy, gates and quotas, `/premium`, gifting commands, permanent Full gift for existing servers | Gates covered by tests; your own server unaffected; 2–3 friendly alliances on gifted Full and Mid |
| **2. Billing** | Discord SKUs and entitlement events, reconciliation on startup, downgrade/grace handling, trials | A test purchase upgrades, cancels and downgrades a server correctly |
| **3. Launch** | Support server, App Directory listing, founding-alliance offer | First 10 paying servers |
| **4. Full-tier extras** | Scheduled weekly report, role-based access, cross-server alliance view, cloud OCR fallback | Driven by what paying servers ask for |

## 8. Open decisions

1. Discord Premium Apps vs Stripe. This depends on eligibility, revenue share and whether annual plans are available.
2. Whether to keep everything on the Mac Studio or move the bot process to a VPS before taking money.
3. Final quota numbers after the capacity benchmark.
4. Whether watermarked charts on Free are worth the goodwill cost, versus not offering charts on Free at all.
