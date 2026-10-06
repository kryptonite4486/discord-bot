"""The bot runs without the Message Content intent and has no text commands."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.admin import Admin  # noqa: E402
from bot.cogs.data import Data  # noqa: E402
from bot.cogs.help_cmd import HelpCmd  # noqa: E402
from bot.cogs.ingest import BATCH_DONE_WORDS, Ingest, batch_word, mentions_user  # noqa: E402
from bot.cogs.ops import Ops  # noqa: E402
from bot.cogs.planner import Planner  # noqa: E402
from bot.cogs.reports import Reports  # noqa: E402
from bot.cogs.server_setup import ServerSetup  # noqa: E402
from bot.cogs.trivia import Trivia  # noqa: E402
from bot.main import LastZAssistant  # noqa: E402
from bot.utils.archive import ImageSource  # noqa: E402

BOT_ID = 1554267047918047252


def _message(content: str, mention_ids=()) -> SimpleNamespace:
    return SimpleNamespace(
        content=content, mentions=[SimpleNamespace(id=i) for i in mention_ids]
    )


class IntentTests(unittest.TestCase):
    def test_message_content_intent_is_off(self) -> None:
        settings = SimpleNamespace(
            app_id=None, database_path=Path(":memory:"), legacy_guild_id=None
        )
        bot = LastZAssistant(settings)  # type: ignore[arg-type]
        self.assertFalse(bot.intents.message_content)
        self.assertFalse(bot.intents.members)
        self.assertTrue(bot.intents.guild_messages)  # needed for /ingest batch
        self.assertIsNone(bot.help_command)

    def test_no_cog_has_text_commands(self) -> None:
        settings = SimpleNamespace(
            bot_owner_ids=frozenset(), control_guild_id=1, data_retention_days=30,
            planner_url="https://example.test",
        )
        bot = SimpleNamespace(settings=settings)
        for cls in (Admin, Data, HelpCmd, Ops, Planner, Reports, ServerSetup, Trivia):
            with self.subTest(cog=cls.__name__):
                self.assertEqual(cls(bot).get_commands(), [])  # type: ignore[arg-type]
        self.assertEqual(_ingest_cog().get_commands(), [])


def _ingest_cog(**bot_attrs) -> Ingest:
    settings = SimpleNamespace(
        ocr_vision_base_url="http://x/v1", ocr_vision_model="m", ocr_vision_api_key="",
        ocr_vision_timeout=5.0, ocr_max_concurrency=1, default_week_start=None,
    )
    return Ingest(SimpleNamespace(settings=settings, **bot_attrs))  # type: ignore[arg-type]


class BatchMessageTests(unittest.TestCase):
    def test_only_direct_mentions_of_the_bot_count(self) -> None:
        self.assertTrue(mentions_user(_message(f"<@{BOT_ID}>", [BOT_ID]), BOT_ID))
        self.assertFalse(mentions_user(_message("no mention"), BOT_ID))
        self.assertFalse(mentions_user(_message("<@123>", [123]), BOT_ID))

    def test_done_word_ignores_the_mention(self) -> None:
        for content in (f"<@{BOT_ID}> done", f"done <@{BOT_ID}>", f"<@!{BOT_ID}>  DONE "):
            with self.subTest(content=content):
                self.assertIn(batch_word(content, BOT_ID), BATCH_DONE_WORDS)
        self.assertEqual(batch_word(f"<@{BOT_ID}>", BOT_ID), "")
        self.assertEqual(batch_word(None, BOT_ID), "")


def _attachment(aid: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=aid, filename=name, content_type="image/png", size=100)


class BatchFlowTests(unittest.IsolatedAsyncioTestCase):
    USER = SimpleNamespace(id=7, mention="<@7>")
    CHANNEL = SimpleNamespace(id=55)

    def _msg(self, content, mention_ids=(), attachments=(), author=None):
        return SimpleNamespace(
            content=content, author=author or self.USER, channel=self.CHANNEL,
            mentions=[SimpleNamespace(id=i) for i in mention_ids],
            attachments=list(attachments), add_reaction=AsyncMock(),
        )

    async def _run_batch(self, incoming):
        """Run /ingest batch over ``incoming``; return (processed, armed, followups)."""
        bot_user = SimpleNamespace(id=BOT_ID, mention=f"<@{BOT_ID}>", display_name="LastZ Assistant")

        async def wait_for(event, check, timeout):
            while incoming:
                m = incoming.pop(0)
                if check(m):
                    return m
            raise asyncio.TimeoutError

        cog = _ingest_cog(user=bot_user, wait_for=wait_for)

        async def load_sources(attachments):
            return [ImageSource(key=str(a.id), filename=a.filename, data=b"") for a in attachments], []

        processed: list[str] = []

        async def process(images, *args, **kwargs):
            processed.extend(i.filename for i in images)
            return "Batch complete"

        armed: list[str] = []
        followups: list[str] = []

        async def followup_send(text, **kwargs):
            followups.append(text)

        interaction = SimpleNamespace(
            guild_id=1, channel_id=55, channel=SimpleNamespace(id=55, send=AsyncMock(return_value=None)),
            user=self.USER,
            response=SimpleNamespace(send_message=AsyncMock(side_effect=lambda text, **k: armed.append(text))),
            followup=SimpleNamespace(send=followup_send),
            is_expired=lambda: False,
        )
        with patch.object(cog, "_load_sources", load_sources), patch.object(
            cog, "_process_attachments", process
        ), patch.object(cog, "_send_long_followup", AsyncMock()):
            await cog.ingest_batch.callback(
                cog, interaction, SimpleNamespace(value="kills"), "2026-10-04", 10
            )
        return processed, armed, followups

    async def test_batch_collects_only_mentioning_messages_until_done(self) -> None:
        incoming = [
            self._msg("", attachments=[_attachment(1, "ignored.png")]),  # no mention
            self._msg(f"<@{BOT_ID}>", [BOT_ID], [_attachment(2, "a.png")], author=SimpleNamespace(id=8)),  # someone else
            self._msg(f"<@{BOT_ID}>", [BOT_ID], [_attachment(3, "b.png"), _attachment(4, "c.png")]),
            self._msg("done"),  # "done" without the mention is ignored too
            self._msg(f"<@{BOT_ID}> done", [BOT_ID]),
            self._msg(f"<@{BOT_ID}>", [BOT_ID], [_attachment(5, "late.png")]),  # after done
        ]
        with self.assertLogs("bot.cogs.ingest", level="INFO") as logs:
            processed, armed, followups = await self._run_batch(incoming)
        self.assertEqual(processed, ["b.png", "c.png"])
        self.assertIn(f"@mentioning <@{BOT_ID}>", armed[0])
        self.assertIn("@LastZ Assistant done", armed[0])
        self.assertEqual(len(incoming), 1)  # stopped at "done"
        # Two missed mentions: one hint, every miss logged, plus a total.
        hints = [f for f in followups if "can't see messages" in f]
        self.assertEqual(len(hints), 1)
        self.assertIn("not the role", hints[0])
        misses = [r for r in logs.output if "without a bot mention" in r]
        self.assertEqual(len(misses), 2)
        self.assertTrue(any("2 message(s) ignored" in r for r in logs.output))

    async def test_batch_with_only_missed_mentions_explains_why_it_got_nothing(self) -> None:
        incoming = [
            self._msg("", attachments=[_attachment(1, "a.png")]),
            self._msg("", attachments=[_attachment(2, "b.png")]),
        ]
        with self.assertLogs("bot.cogs.ingest", level="INFO"):
            processed, _, followups = await self._run_batch(incoming)
        self.assertEqual(processed, [])
        self.assertEqual(sum("can't see messages" in f for f in followups), 1)
        self.assertIn(
            "No images received — batch cancelled. 2 message(s) were ignored because "
            "they didn't @mention me.",
            followups,
        )

    async def test_correct_batch_gets_no_hint(self) -> None:
        incoming = [
            self._msg(f"<@{BOT_ID}>", [BOT_ID], [_attachment(3, "b.png")]),
            self._msg(f"<@{BOT_ID}> done", [BOT_ID]),
        ]
        processed, _, followups = await self._run_batch(incoming)
        self.assertEqual(processed, ["b.png"])
        self.assertFalse(any("can't see messages" in f for f in followups))


if __name__ == "__main__":
    unittest.main()
