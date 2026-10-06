---
name: discord-e2e
description: Run end-to-end tests of LastZ Assistant in real Discord, through the built-in browser pane signed in as the user, then verify in the bot's logs and database and clean up. Use when the user asks to test the bot "for real", "in Discord", "end to end", or "as me in the browser".
---

# End-to-end tests in Discord (as the user)

The browser pane is signed in to Discord as the user. Anything sent there is
sent **as them**, so these rules are not optional.

## Rules

1. **Only in the test channel.** Send messages, run commands and attach files
   only in the test channel below. Anywhere else (an alliance server, a DM,
   another channel) needs the user's explicit OK in chat first.
2. **Only when asked.** Run tests only when the user asks for a test run in this
   conversation. Each run is a fresh permission; don't carry one over.
3. **Never type credentials.** If Discord shows a login page, MFA prompt or
   CAPTCHA, stop and ask the user to sign in. QR login works best: widen the
   page (`resize_window` 1280x800) so Discord shows the code, and they scan it
   with the Discord app (You → Settings → Scan QR Code).
4. **Clean up.** End every run with `/data delete` (scope: This channel) in the
   test channel, and leave no draft attachments behind.
5. **Report honestly.** A test passes only if the logs or database show it.
   What appears in Discord alone isn't proof.

## Where

- Server: **LastZ Bot Control Server** (`1556779779280347236`)
- Test channel: **#bot-testing**, `https://discord.com/channels/1556779779280347236/1556864301262831666`
- The bot is **LastZ Assistant** (user ID `1554267047918047252`). `/ops`
  commands only exist in this server.

## Setup each run

1. `resize_window` to 1280x800 (the app resets the viewport between turns;
   narrow layouts hide parts of Discord).
2. Navigate to the test channel URL and take a screenshot. If it isn't the
   channel view, see rule 3.
3. Note the time (`date -u`) so log checks can start from it.

## How to drive Discord

- **Slash command:** click the message box, type the command (e.g.
  `/ingest batch`), wait for the command list, and pick **LastZ Assistant**'s
  entry. Fill each option by clicking it or pressing Tab, typing the value, and
  choosing from the list, then press Enter. Take a screenshot before Enter to
  check the options.
- **@mention:** type `@LastZ` and click the **LastZ Assistant** member entry
  (the bot user, `LastZ Assistant#7201`), **not** the `@LastZ Metrics` role
  below it. A role mention doesn't count.
- **Before pressing Enter on a message that @mentions the bot**, press
  **Escape**: Discord opens a "Commands matching @LastZ Assistant …" list,
  and Enter would run the highlighted command (e.g. `/ops purges`) instead of
  sending the message. Screenshot to check the list is gone.
- **Optional slash options:** when Discord shows an "Options" list you don't
  need (e.g. `scope` on `/data delete`), press Escape, then Enter, to use the
  defaults.
- **Coordinates:** with the 1280x800 viewport the page is drawn scaled into
  the top-left of an 800x500 screenshot frame; the message box sits around
  (260, 253). Take a screenshot before clicking and use what it shows.
- **Attach a screenshot.** The pane blocks pages from fetching files from this
  Mac, so the image goes inside the script. Encode the fixture:

  ```bash
  base64 -i tests/e2e/general_profile_small.jpg | tr -d '\n'
  ```

  then run with `javascript_tool`, pasting the output in place of `B64`. The
  script refuses to attach unless the bytes match the fixture exactly: copying
  15,000 characters by hand can drop one, and a damaged image gives a
  misleading OCR result.

  ```js
  const bytes = Uint8Array.from(atob("B64"), c => c.charCodeAt(0));
  const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map(b => b.toString(16).padStart(2, "0")).join("");
  const expected = "3a7149042b7e29c9cccf425cedf1ed960ca3979b9b2148d5502e4920e58ca374";  // shasum -a 256 tests/e2e/general_profile_small.jpg
  if (bytes.length !== 11688 || hash !== expected) {
    ({ok: false, bytes: bytes.length, hash});  // re-encode and paste again
  } else {
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], "general_profile_small.jpg", {type: "image/jpeg"}));
    const input = document.querySelector('input[type=file]');
    input.files = dt.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    ({ok: true});
  }
  ```

  The file appears as a draft attachment above the message box; type the
  message text (e.g. the @mention) and press Enter to send both. To discard a
  draft, click its trash icon.
  The fixture is player **PrincessPea: HQ 24, Power 65.4M** (dataset `general`).

## Verify

Bot logs for the run. Use a relative window, or end the timestamp with `Z`:
without it Docker reads the time as local, not UTC, and returns nothing.

```bash
docker compose logs --since 20m --no-color | grep -vE "voice will NOT|OCR batch progress"
```

Database (read-only):

```bash
docker compose exec -T discord-bot python -c "
import sqlite3; c = sqlite3.connect('file:/app/data/weekly.db?mode=ro', uri=True)
print(c.execute(\"select PlayerName, MetricType, Value, WeekStart from WeeklyMetrics where GuildId='1556779779280347236' order by UpdatedAt desc limit 10\").fetchall())
print(c.execute(\"select * from UsageLedger where GuildId='1556779779280347236'\").fetchall())"
```

## Test cases

Run the ones the user asks for; "all" means this list in order.

1. **Help:** `/help` replies with the command list ("LastZ Assistant — Commands").
2. **Batch, happy path:** `/ingest batch dataset:general week:current`, then a
   message with the fixture attached and an @mention of the bot, then
   `@LastZ Assistant done`.
   - Discord: the "Batch OCR armed" message, ✅ on the upload message, a batch summary.
   - Logs: `Batch OCR starting`, `OCR batch start: 1 image(s)`,
     `Vision OCR kind=general rows=1 metrics=2`, `OCR saved 2 rows`.
   - DB: PrincessPea `HQLevel` 24 and `Power` 65400000 for this week; a
     `UsageLedger` row with `ocr_images` 1.
3. **Batch, missed mention:** `/ingest batch dataset:general`, then the fixture
   **without** an @mention, then `@LastZ Assistant done`.
   - Discord: one private hint "I can't see messages that don't @mention me…",
     then "No images received… 1 message(s) were ignored…".
   - Logs: `Batch ignored a message without a bot mention`.
4. **Operator views:** `/ops queue` shows "0/1 slot(s) busy"; `/ops usage`
   lists LastZ Bot Control Server after test 2.
5. **Cleanup (always):** `/data delete` (default scope: this channel). Check
   the confirmation names #bot-testing and only the test rows, then click
   **Delete permanently**. Logs: `Database backup written` then
   `/data delete by …`. DB: no rows left for the control server, and the other
   servers' row counts unchanged.

Last full run: 2026-10-06, all five passed.

Finish with a short pass/fail table per case, quoting the log line or database
row that proves each result.
