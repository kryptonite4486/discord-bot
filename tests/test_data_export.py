"""Tests for /data export, /ops export and the export helpers."""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.data import Data  # noqa: E402
from bot.cogs.ops import NOT_OPERATOR_MESSAGE, Ops  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.export import fit_upload  # noqa: E402
from bot.utils.guild import channel_display_name  # noqa: E402
from bot.utils.tiers import FREE, FULL, MID, Tiers, ensure_feature, quota_week_start  # noqa: E402

G, OTHER = "123456789012345678", "223456789012345678"
C1, C2 = "1001", "1002"
W1, W2, W3 = "2026-09-20", "2026-09-27", "2026-10-04"
OPERATOR = 7
TS = "%Y-%m-%d %H:%M:%S"


def _guild(gid=G):
    names = {int(C1): "versus", int(C2): "tech"}
    return SimpleNamespace(
        id=int(gid),
        name="Alpha",
        filesize_limit=10 * 1024 * 1024,
        get_channel=lambda cid: SimpleNamespace(name=names[cid]) if cid in names else None,
    )


class _Interaction:
    """Enough of discord.Interaction for the export handlers."""

    def __init__(self, *, guild_id=G, channel_id=C1, guild=None, user_id=5, client=None):
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.guild = guild if guild is not None else _guild(guild_id)
        self.user = SimpleNamespace(id=user_id)
        self.client = client
        self.sent: list[str] = []  # response.send_message
        # (text, (filename, bytes) or None)
        self.followups: list[tuple[str, tuple[str, bytes] | None]] = []
        self._done = False
        outer = self

        class Response:
            def is_done(self):
                return outer._done

            async def send_message(self, text, ephemeral=False):
                outer.sent.append(text)
                outer._done = True

            async def defer(self, ephemeral=False, thinking=False):
                outer._done = True

        class Followup:
            async def send(self, text, ephemeral=False, file=None):
                data = None
                if file is not None:
                    data = (file.filename, file.fp.read())
                outer.followups.append((text, data))

        self.response = Response()
        self.followup = Followup()


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()
        for week, channel, player, metric, value in (
            (W1, C1, "Ann", "VersusPoints", 1200.0),
            (W2, C1, "Ann", "VersusPoints", 1500.0),
            (W3, C1, "Bob", "Power", 65.4e6),
            (W3, C2, "Zoë", "TechContribution", 12.5),
            (W3, "", "Old", "Power", 1.0),  # unassigned legacy row
        ):
            await self.db.upsert_metric(G, week, player, metric, value, channel_id=channel)
        await self.db.upsert_metric(OTHER, W3, "Eve", "Power", 9.0, channel_id="2001")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    def _bot(self, tiers=None, owners=(OPERATOR,)):
        settings = SimpleNamespace(
            data_retention_days=30, bot_owner_ids=frozenset(owners), control_guild_id=999
        )
        return SimpleNamespace(
            db=self.db, settings=settings, tiers=tiers, guilds=[],
            get_guild=lambda gid: _guild(str(gid)) if str(gid) == G else None,
        )

    async def _export(self, *, tiers=None, scope=None, fmt=None, from_week=None, to_week=None):
        cog = Data(self._bot(tiers))  # type: ignore[arg-type]
        inter = _Interaction()
        await cog.export.callback(
            cog, inter,
            SimpleNamespace(value=scope) if scope else None,
            SimpleNamespace(value=fmt) if fmt else None,
            from_week, to_week,
        )
        return inter

    @staticmethod
    def _csv(inter):
        text, (name, data) = inter.followups[-1]
        return text, name, list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


class DataExportTests(_DbCase):
    async def test_csv_this_channel(self) -> None:
        inter = await self._export()
        text, name, rows = self._csv(inter)
        self.assertEqual(name, f"lastz-{G}-channel-{C1}.csv")
        self.assertIn("**3** row(s) for #versus", text)
        self.assertEqual(
            list(rows[0]),
            ["player", "metric", "value", "week", "channel_name", "channel_id", "updated_at"],
        )
        self.assertEqual(
            [(r["player"], r["metric"], r["value"], r["week"]) for r in rows],
            [
                ("Ann", "VersusPoints", "1200", W1),
                ("Ann", "VersusPoints", "1500", W2),
                ("Bob", "Power", "65400000", W3),
            ],
        )
        self.assertTrue(all(r["channel_name"] == "#versus" and r["channel_id"] == C1 for r in rows))
        self.assertTrue(all(r["updated_at"] for r in rows))

    async def test_json_entire_server(self) -> None:
        inter = await self._export(scope="server", fmt="json")
        text, (name, data) = inter.followups[-1]
        self.assertEqual(name, f"lastz-{G}-server.json")
        self.assertIn("**5** row(s) for this **entire server**", text)
        doc = json.loads(data)
        self.assertEqual(doc["guild_id"], G)
        self.assertEqual(doc["scope"], "server")
        self.assertEqual(doc["row_count"], 5)
        by_player = {r["player"]: r for r in doc["rows"]}
        self.assertNotIn("Eve", by_player)  # other servers never leak in
        self.assertEqual(by_player["Zoë"]["value"], 12.5)
        self.assertEqual(by_player["Zoë"]["channel_name"], "#tech")
        self.assertEqual(by_player["Bob"]["value"], 65400000)
        self.assertEqual(by_player["Old"]["channel_id"], "")
        self.assertEqual(by_player["Old"]["channel_name"], "(unassigned)")

    async def test_week_filter(self) -> None:
        inter = await self._export(scope="server", from_week="2026-09-29", to_week=W3)
        text, name, rows = self._csv(inter)
        # 2026-09-29 is a Tuesday: normalized to its Sunday, W2.
        self.assertIn(f"for weeks {W2} to {W3}", text)
        self.assertEqual(name, f"lastz-{G}-server-{W2}_to_{W3}.csv")
        self.assertEqual({r["week"] for r in rows}, {W2, W3})
        inter = await self._export(to_week=W1)
        self.assertEqual([r["week"] for r in self._csv(inter)[2]], [W1])

    async def test_bad_weeks_are_refused(self) -> None:
        inter = await self._export(from_week="soon")
        self.assertIn("Invalid week", inter.sent[0])
        inter = await self._export(from_week=W3, to_week=W1)
        self.assertIn("after", inter.sent[0])
        self.assertFalse(inter.followups)

    async def test_nothing_stored(self) -> None:
        inter = await self._export(from_week="2027-01-03")
        self.assertIn("Nothing is stored for #versus", inter.followups[-1][0])
        self.assertIsNone(inter.followups[-1][1])

    async def test_too_big_even_zipped(self) -> None:
        cog = Data(self._bot())  # type: ignore[arg-type]
        guild = _guild()
        guild.filesize_limit = 50
        inter = _Interaction(guild=guild)
        await cog.export.callback(cog, inter, None, None, None, None)
        self.assertIn("upload limit even zipped", inter.followups[-1][0])
        self.assertIsNone(inter.followups[-1][1])


class HistoryWindowTests(_DbCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.this_week = quota_week_start().isoformat()
        self.old_week = (quota_week_start() - timedelta(weeks=40)).isoformat()
        for week in (self.this_week, self.old_week):
            await self.db.upsert_metric(G, week, "Ann", "Power", 5.0, channel_id=C1)
        now = datetime.now(timezone.utc)
        await self.db.add_entitlement(
            G, "mid", "gift", starts_at=(now - timedelta(days=1)).strftime(TS),
            ends_at=None, granted_by="op", reason="test",
        )

    async def test_export_follows_the_plan_window_when_enforced(self) -> None:
        inter = await self._export(tiers=Tiers(self.db, enforced=True))
        text, _, rows = self._csv(inter)
        weeks = {r["week"] for r in rows}
        self.assertIn(self.this_week, weeks)
        self.assertNotIn(self.old_week, weeks)
        self.assertIn("1 older week hidden", text)
        self.assertIn("**Command** plan", text)

    async def test_not_enforced_exports_everything(self) -> None:
        inter = await self._export(tiers=Tiers(self.db, enforced=False))
        weeks = {r["week"] for r in self._csv(inter)[2]}
        self.assertIn(self.old_week, weeks)
        self.assertNotIn("🔒", inter.followups[-1][0])

    async def test_range_entirely_outside_window(self) -> None:
        inter = await self._export(tiers=Tiers(self.db, enforced=True), to_week=self.old_week)
        text, data = inter.followups[-1]
        self.assertIsNone(data)
        self.assertIn("Nothing is stored", text)
        self.assertIn("1 older week hidden", text)

    async def test_operator_export_ignores_the_window(self) -> None:
        cog = Ops(self._bot(Tiers(self.db, enforced=True)))  # type: ignore[arg-type]
        inter = _Interaction(guild_id="999", user_id=OPERATOR)
        with self.assertLogs("bot.cogs.ops", level="WARNING"):
            await cog.export.callback(cog, inter, G, None)
        _, (_, data) = inter.followups[-1]
        weeks = {r["week"] for r in csv.DictReader(io.StringIO(data.decode("utf-8-sig")))}
        self.assertIn(self.old_week, weeks)
        self.assertIn(self.this_week, weeks)


class GateTests(_DbCase):
    def test_export_feature_on_paid_plans_only(self) -> None:
        self.assertFalse(FREE.allows("export"))
        self.assertTrue(MID.allows("export"))
        self.assertTrue(FULL.allows("export"))

    def test_export_command_is_gated(self) -> None:
        cog = Data(self._bot())  # type: ignore[arg-type]
        (cmd,) = [c for c in cog.walk_app_commands() if c.qualified_name == "data export"]
        names = [c.__qualname__ for c in cmd.checks]
        self.assertTrue(any("requires_feature" in n for n in names), names)
        self.assertTrue(any("has_permissions" in n for n in names), names)

    async def test_gate_blocks_free_only_when_enforced(self) -> None:
        for enforced, allowed in ((True, False), (False, True)):
            with self.subTest(enforced=enforced):
                inter = _Interaction(client=SimpleNamespace(tiers=Tiers(self.db, enforced=enforced)))
                self.assertEqual(await ensure_feature(inter, "export"), allowed)
                if not allowed:
                    self.assertIn("CSV and JSON exports", inter.sent[0])
                    self.assertIn("**Alliance** plan", inter.sent[0])

    async def test_alliance_can_export_when_enforced(self) -> None:
        now = datetime.now(timezone.utc)
        await self.db.add_entitlement(
            G, "mid", "gift", starts_at=(now - timedelta(days=1)).strftime(TS),
            ends_at=None, granted_by="op", reason="test",
        )
        inter = _Interaction(client=SimpleNamespace(tiers=Tiers(self.db, enforced=True)))
        self.assertTrue(await ensure_feature(inter, "export"))


class ChannelNameTests(unittest.TestCase):
    def test_thread_names_resolve(self) -> None:
        # Data added in a thread: get_channel misses threads, get_channel_or_thread finds them.
        thread = SimpleNamespace(name="e2e-thread")
        guild = SimpleNamespace(
            get_channel=lambda cid: None,
            get_channel_or_thread=lambda cid: thread if cid == 99 else None,
        )
        self.assertEqual(channel_display_name(guild, "99"), "#e2e-thread")
        self.assertEqual(channel_display_name(guild, "1234"), "#unknown-1234")


class OpsExportTests(_DbCase):
    async def test_operator_gets_every_row(self) -> None:
        cog = Ops(self._bot())  # type: ignore[arg-type]
        inter = _Interaction(guild_id="999", user_id=OPERATOR)
        with self.assertLogs("bot.cogs.ops", level="WARNING"):
            await cog.export.callback(cog, inter, f"Alpha ({G})", SimpleNamespace(value="json"))
        text, (name, data) = inter.followups[-1]
        self.assertEqual(name, f"lastz-{G}-server.json")
        self.assertIn("**5** row(s) for **Alpha**", text)
        self.assertEqual(json.loads(data)["row_count"], 5)

    async def test_server_the_bot_left(self) -> None:
        cog = Ops(self._bot())  # type: ignore[arg-type]
        inter = _Interaction(guild_id="999", user_id=OPERATOR)
        with self.assertLogs("bot.cogs.ops", level="WARNING"):
            await cog.export.callback(cog, inter, OTHER, None)
        text, (_, data) = inter.followups[-1]
        self.assertIn("Unknown server", text)
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual([(r["player"], r["channel_name"]) for r in rows], [("Eve", "#unknown-2001")])

    async def test_non_operator_refused_in_handler(self) -> None:
        cog = Ops(self._bot())  # type: ignore[arg-type]
        inter = _Interaction(guild_id="999", user_id=123)
        with self.assertLogs("bot.cogs.ops", level="WARNING"):
            await cog.export.callback(cog, inter, G, None)
        self.assertEqual(inter.sent, [NOT_OPERATOR_MESSAGE])
        self.assertFalse(inter.followups)

    async def test_bad_guild_id(self) -> None:
        cog = Ops(self._bot())  # type: ignore[arg-type]
        inter = _Interaction(guild_id="999", user_id=OPERATOR)
        await cog.export.callback(cog, inter, "not a server", None)
        self.assertIn("isn't a server ID", inter.sent[0])

    def test_ops_export_is_registered_with_autocomplete(self) -> None:
        cog = Ops(self._bot())  # type: ignore[arg-type]
        (cmd,) = [c for c in cog.walk_app_commands() if c.qualified_name == "ops export"]
        self.assertTrue(cmd._params["guild_id"].autocomplete)


class ZipTests(_DbCase):
    def test_small_file_is_sent_as_is(self) -> None:
        self.assertEqual(fit_upload("a.csv", b"x" * 10, 100), ("a.csv", b"x" * 10, False))

    def test_large_file_is_zipped(self) -> None:
        data = b"player,metric\n" + b"Ann,Power\n" * 10_000
        name, zipped, was_zipped = fit_upload("a.csv", data, 10_000)
        self.assertTrue(was_zipped)
        self.assertEqual(name, "a.csv.zip")
        self.assertLessEqual(len(zipped), 10_000)
        with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
            self.assertEqual(zf.namelist(), ["a.csv"])
            self.assertEqual(zf.read("a.csv"), data)

    def test_incompressible_file_over_limit(self) -> None:
        self.assertIsNone(fit_upload("a.csv", os.urandom(5000), 1000))

    async def test_command_zips_over_the_server_limit(self) -> None:
        for i in range(300):
            await self.db.upsert_metric(G, W3, f"Player{i:03d}", "Kills", i, channel_id=C1)
        cog = Data(self._bot())  # type: ignore[arg-type]
        guild = _guild()
        guild.filesize_limit = 4000  # the CSV is ~20 KB; zipped it fits
        inter = _Interaction(guild=guild)
        await cog.export.callback(cog, inter, None, None, None, None)
        text, (name, data) = inter.followups[-1]
        self.assertIn("zipped", text)
        self.assertEqual(name, f"lastz-{G}-channel-{C1}.csv.zip")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            body = zf.read(f"lastz-{G}-channel-{C1}.csv").decode("utf-8-sig")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(body)))), 303)


if __name__ == "__main__":
    unittest.main()
