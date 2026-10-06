"""Tests for trivia: question bank, scoring, storage, and server/cross-server matches."""

from __future__ import annotations

import asyncio
import random
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.cogs import trivia as trivia_cog  # noqa: E402
from bot.cogs.trivia import AnswerView, JoinView, Trivia, format_leaderboard  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.trivia.engine import BASE_POINTS, SPEED_BONUS, AnswerResult, Match, Mode  # noqa: E402
from bot.trivia.questions import (  # noqa: E402
    BUNDLED_BANK,
    categories,
    load_bank,
    parse_bank,
    pick_questions,
)


def _raw(text: str = "Q?", **kw) -> dict:
    return {"question": text, "correct": "yes", "incorrect": ["no", "maybe"], **kw}


class QuestionBankTests(unittest.TestCase):
    def test_bundled_bank_is_valid(self) -> None:
        bank = load_bank(BUNDLED_BANK)
        self.assertGreaterEqual(len(bank), 80)
        self.assertGreaterEqual(len(categories(bank)), 5)

    def test_rejects_bad_entries(self) -> None:
        cases = {
            "duplicate": [_raw("Same?"), _raw("same? ")],
            "answers must all be different": [_raw(incorrect=["Yes", "no"])],
            "one to three": [_raw(incorrect=[])],
            "difficulty": [_raw(difficulty="extreme")],
            "missing": [{"question": "Q?"}],
            "empty": [],
        }
        for reason, entries in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                parse_bank(entries)

    def test_true_false_questions_are_allowed(self) -> None:
        (q,) = parse_bank([{"question": "Sky is blue?", "correct": "True", "incorrect": ["False"]}])
        self.assertEqual(q.incorrect, ("False",))

    def test_small_bank_reuses_least_recent_questions_first(self) -> None:
        # Memory (300) far larger than the bank (6): matches must still fill,
        # and cycle through the whole bank before any question comes back.
        bank = parse_bank([_raw(f"Q{i}?") for i in range(6)])
        recent: deque[str] = deque(maxlen=300)
        rng = random.Random(7)
        matches = []
        for _ in range(6):
            picked = pick_questions(bank, 3, avoid=recent, rng=rng)
            self.assertEqual(len(picked), 3)
            ids = {a.question.id for a in picked}
            recent.extend(a.question.id for a in picked)
            matches.append(ids)
        for n in range(2, 6):
            self.assertEqual(matches[n], matches[n - 2])
            self.assertFalse(matches[n] & matches[n - 1])

    def test_pick_filters_shuffles_and_avoids_recent(self) -> None:
        bank = parse_bank(
            [_raw(f"S{i}?", category="Science") for i in range(5)]
            + [_raw(f"H{i}?", category="History", difficulty="hard") for i in range(5)]
        )
        rng = random.Random(1)
        science = pick_questions(bank, 10, category="science", rng=rng)
        self.assertEqual({a.question.category for a in science}, {"Science"})
        self.assertEqual(len(science), 5)
        for a in science:
            self.assertEqual(a.correct, "yes")
            self.assertEqual(sorted(a.choices), ["maybe", "no", "yes"])
        recent = {a.question.id for a in pick_questions(bank, 3, category="History", rng=rng)}
        later = pick_questions(bank, 3, category="History", avoid=list(recent), rng=rng)
        self.assertEqual(len(recent & {a.question.id for a in later}), 1)  # only 2 fresh left
        self.assertEqual(len(pick_questions(bank, 9, difficulty="hard", rng=rng)), 5)


def _match(n: int = 2, seconds: float = 10.0, mode: Mode = Mode.SERVER) -> Match:
    bank = parse_bank([_raw(f"Q{i}?") for i in range(n)])
    return Match(mode=mode, questions=pick_questions(bank, n), seconds=seconds)


class MatchTests(unittest.TestCase):
    def _answer(self, match: Match, user: int, choice: int, now: float, guild: str = "g1"):
        return match.answer(user, choice, now, guild_id=guild, guild_name=guild.upper(), name=f"u{user}")

    def test_scoring_one_answer_and_time_limit(self) -> None:
        m = _match()
        right = m.open_round(100.0).answer_index
        wrong = (right + 1) % 3
        self.assertIs(self._answer(m, 1, right, 100.0), AnswerResult.LOCKED)
        self.assertIs(self._answer(m, 1, wrong, 101.0), AnswerResult.ALREADY_ANSWERED)
        self.assertIs(self._answer(m, 2, right, 105.0), AnswerResult.LOCKED)
        self.assertIs(self._answer(m, 3, wrong, 106.0), AnswerResult.LOCKED)
        self.assertIs(self._answer(m, 4, right, 110.5), AnswerResult.CLOSED)
        result = m.close_round()
        self.assertEqual(
            [(p.user_id, pts) for p, pts in result.winners],
            [(1, BASE_POINTS + SPEED_BONUS), (2, BASE_POINTS + SPEED_BONUS // 2)],
        )
        self.assertEqual(result.answered, 3)
        self.assertEqual(sum(result.choice_counts), 3)
        self.assertIs(self._answer(m, 5, right, 100.0), AnswerResult.CLOSED)
        self.assertEqual([p.user_id for p in m.standings()], [1, 2, 3])
        self.assertEqual(m.players[3].answered, 1)
        self.assertEqual(m.players[3].points, 0)

    def test_winners_ties_and_nobody(self) -> None:
        m = _match(n=1)
        self.assertEqual(m.winners(), [])
        right = m.open_round(0.0).answer_index
        self._answer(m, 1, right, 0.0)
        self._answer(m, 2, right, 0.0)
        m.close_round()
        self.assertEqual({p.user_id for p in m.winners()}, {1, 2})
        self.assertFalse(m.has_next)
        with self.assertRaises(RuntimeError):
            m.open_round(1.0)

    def test_everyone_answered_waits_for_last_rounds_players(self) -> None:
        m = _match(n=5)
        m.open_round(0.0)
        self._answer(m, 1, 0, 0.0)
        self._answer(m, 2, 0, 0.0)
        self.assertFalse(m.everyone_answered)  # first question: nobody expected yet
        m.close_round()
        m.open_round(20.0)
        self._answer(m, 1, 0, 20.0)
        self.assertFalse(m.everyone_answered)  # still waiting for player 2
        self._answer(m, 3, 0, 20.0)  # a newcomer doesn't satisfy the wait
        self.assertFalse(m.everyone_answered)
        self._answer(m, 2, 0, 20.0)
        self.assertTrue(m.everyone_answered)
        m.close_round()
        self.assertFalse(m.everyone_answered)  # closed rounds never count
        m.open_round(40.0)
        self._answer(m, 1, 0, 40.0)
        self._answer(m, 3, 0, 40.0)
        self.assertFalse(m.everyone_answered)  # newcomer 3 is expected now
        self._answer(m, 2, 0, 40.0)
        self.assertTrue(m.everyone_answered)
        m.close_round()
        m.open_round(60.0)
        self._answer(m, 1, 0, 60.0)
        m.close_round()  # 2 and 3 skip this one, so they aren't waited for next
        m.open_round(80.0)
        self._answer(m, 1, 0, 80.0)
        self.assertTrue(m.everyone_answered)

    def test_guild_totals_keep_first_server(self) -> None:
        m = _match(mode=Mode.GLOBAL)
        right = m.open_round(0.0).answer_index
        self._answer(m, 1, right, 0.0, guild="a")
        self._answer(m, 2, right, 0.0, guild="b")
        self._answer(m, 3, right, 0.0, guild="b")
        m.close_round()
        right = m.open_round(20.0).answer_index
        self._answer(m, 1, right, 20.0, guild="b")  # same user from another server
        self._answer(m, 2, right, 20.0, guild="b")
        m.close_round()
        self.assertEqual(m.players[1].guild_id, "a")
        totals = m.guild_totals()
        self.assertEqual([(g, n) for g, _, _, n in totals], [("b", 2), ("a", 1)])
        self.assertEqual(totals[1][2], 2 * (BASE_POINTS + SPEED_BONUS))


def _player(guild: str, user: int, points: int, won: bool = False, name: str | None = None) -> dict:
    return {
        "guild_id": guild, "user_id": user, "name": name or f"user{user}",
        "guild_name": f"Server {guild}", "points": points, "correct": points // 100,
        "answered": 3, "won": won,
    }


class TriviaDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_server_board_sums_matches_and_modes(self) -> None:
        await self.db.record_trivia_match("server", [_player("A", 1, 300, won=True), _player("A", 2, 100)])
        await self.db.record_trivia_match("server", [_player("A", 2, 400, won=True, name="New name")])
        await self.db.record_trivia_match("global", [_player("A", 1, 150)])
        await self.db.record_trivia_match("server", [_player("B", 9, 999)])
        rows = await self.db.trivia_leaderboard(guild_id="A")
        self.assertEqual([(r["UserId"], r["Points"]) for r in rows], [("2", 500), ("1", 450)])
        self.assertEqual(rows[0]["DisplayName"], "New name")
        self.assertEqual((rows[0]["Games"], rows[0]["Wins"]), (2, 1))
        self.assertEqual((rows[1]["Games"], rows[1]["Wins"]), (2, 1))

    async def test_cross_server_board_respects_opt_out(self) -> None:
        await self.db.record_trivia_match(
            "global", [_player("A", 1, 300), _player("B", 2, 200), _player("A", 3, 100)]
        )
        await self.db.record_trivia_match("server", [_player("C", 4, 5000)])
        rows = await self.db.trivia_leaderboard(guild_id=None)
        self.assertEqual([r["UserId"] for r in rows], ["1", "2", "3"])
        self.assertEqual(rows[1]["GuildName"], "Server B")
        await self.db.set_trivia_settings("A", allow_global=False, announce_channel_id="55")
        rows = await self.db.trivia_leaderboard(guild_id=None)
        self.assertEqual([r["UserId"] for r in rows], ["2"])

    async def test_settings_defaults_and_announce_channels(self) -> None:
        self.assertEqual(
            await self.db.trivia_settings("A"), {"allow_global": True, "announce_channel_id": None}
        )
        await self.db.set_trivia_settings("A", allow_global=True, announce_channel_id="10")
        await self.db.set_trivia_settings("B", allow_global=False, announce_channel_id="20")
        await self.db.set_trivia_settings("C", allow_global=True, announce_channel_id=None)
        self.assertEqual(await self.db.trivia_announce_channels(), [("A", "10")])

    async def test_reset_and_purge_remove_trivia_data(self) -> None:
        await self.db.record_trivia_match("server", [_player("A", 1, 100), _player("B", 2, 100)])
        await self.db.set_trivia_settings("A", allow_global=False, announce_channel_id=None)
        self.assertEqual(await self.db.guilds_with_data(), {"A", "B"})
        self.assertEqual(await self.db.delete_trivia_scores("B"), 1)
        self.assertEqual(await self.db.guilds_with_data(), {"A"})
        await self.db.purge_guild("A")
        self.assertEqual(await self.db.guilds_with_data(), set())
        self.assertTrue((await self.db.trivia_settings("A"))["allow_global"])  # back to default

    def test_leaderboard_text(self) -> None:
        rows = [{"DisplayName": "*Ann*", "GuildName": "Wolves", "Points": 1200,
                 "Correct": 9, "Answered": 10, "Games": 1, "Wins": 1}]
        self.assertEqual(
            format_leaderboard(rows, cross_server=True),
            "1. **\\*Ann\\*** (Wolves) — 1,200 pts · 9/10 correct · 1 game · 1 win",
        )
        self.assertIn("No trivia scores yet", format_leaderboard([], cross_server=False))


# --- Simulated Discord ------------------------------------------------------


class FakeMessage:
    def __init__(self, channel, embed=None, view=None) -> None:
        self.channel, self.embed, self.view = channel, embed, view
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)
        self.embed = kwargs.get("embed", self.embed)


class FakeChannel(discord.abc.Messageable):
    def __init__(self, cid: int, on_question=None) -> None:
        self.id = cid
        self.sent: list[FakeMessage] = []
        self.on_question = on_question

    async def _get_channel(self):  # pragma: no cover - unused by the fakes
        return self

    async def send(self, content=None, *, embed=None, view=None, **kwargs):
        msg = FakeMessage(self, embed, view)
        self.sent.append(msg)
        if isinstance(view, AnswerView) and self.on_question:
            await self.on_question(self, view)
        return msg

    def titles(self) -> list[str]:
        return [m.embed.title for m in self.sent if m.embed is not None]


class FakeGuild(SimpleNamespace):
    pass


def _interaction(user: int, guild: FakeGuild, channel: FakeChannel, *, manage=False):
    replies: list[tuple] = []
    original = FakeMessage(channel)

    async def send_message(content=None, **kwargs):
        replies.append((content, kwargs))
        if kwargs.get("embed") is not None:
            original.embed = kwargs["embed"]

    return SimpleNamespace(
        user=SimpleNamespace(id=user, display_name=f"user{user}"),
        guild=guild, guild_id=guild.id, channel=channel, channel_id=channel.id,
        app_permissions=SimpleNamespace(send_messages=True, embed_links=True),
        permissions=SimpleNamespace(manage_messages=manage),
        response=SimpleNamespace(send_message=send_message, is_done=lambda: bool(replies)),
        original_response=AsyncMock(return_value=original),
        replies=replies, original=original,
    )


class TriviaCogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        self.channels: dict[int, FakeChannel] = {}
        self.bot = SimpleNamespace(
            settings=SimpleNamespace(trivia_questions_path=None),
            db=self.db, get_channel=lambda cid: self.channels.get(cid),
        )
        self.cog = Trivia(self.bot)  # type: ignore[arg-type]
        self.guild_a = FakeGuild(id=1, name="Wolves")
        self.guild_b = FakeGuild(id=2, name="Bears")
        patches = [
            patch.object(trivia_cog, "SERVER_COUNTDOWN", 0),
            patch.object(trivia_cog, "LOBBY_SECONDS", 0.05),
            patch.object(trivia_cog, "REVEAL_PAUSE", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def _click(self, user: int, guild: FakeGuild, channel: FakeChannel, view: AnswerView, right: bool):
        asked = view.game.match.questions[view.round_index]
        choice = asked.answer_index if right else (asked.answer_index + 1) % len(asked.choices)
        inter = _interaction(user, guild, channel)
        await self.cog.answer(inter, view.game, view.round_index, choice)
        return inter.replies[0][0]

    async def _start(self, inter, **kw):
        await self.cog.start.callback(
            self.cog, inter, kw.pop("mode", None), kw.pop("questions", 3),
            kw.pop("seconds", 0.05), kw.pop("category", None), None,
        )
        return self.cog.games.get(inter.channel_id)

    async def test_server_match_plays_scores_and_records(self) -> None:
        clicks: list[str] = []

        async def on_question(channel, view):
            clicks.append(await self._click(10, self.guild_a, channel, view, right=True))
            clicks.append(await self._click(11, self.guild_a, channel, view, right=False))
            clicks.append(await self._click(10, self.guild_a, channel, view, right=False))

        channel = FakeChannel(100, on_question)
        game = await self._start(_interaction(10, self.guild_a, channel))
        self.assertIsNotNone(game)
        await game.task
        self.assertEqual(channel.titles(), ["Question 1/3", "Question 2/3", "Question 3/3", "🏆 Trivia results"])
        self.assertEqual(sum("Locked in" in c for c in clicks), 6)
        self.assertEqual(sum("already answered" in c for c in clicks), 3)
        # Every question message was revealed with disabled buttons.
        for msg in channel.sent[:3]:
            view = msg.edits[-1]["view"]
            self.assertTrue(all(b.disabled for b in view.children))
        final = channel.sent[-1].embed
        self.assertIn("user10", final.description)
        self.assertNotIn(100, self.cog.games)
        rows = await self.db.trivia_leaderboard(guild_id="1")
        self.assertEqual([(r["UserId"], r["Wins"], r["Correct"]) for r in rows], [("10", 1, 3), ("11", 0, 0)])
        self.assertEqual(await self.db.trivia_leaderboard(guild_id=None), [])  # not cross-server

    async def test_rounds_end_early_once_everyone_has_answered(self) -> None:
        async def on_question(channel, view):
            await self._click(10, self.guild_a, channel, view, right=True)
            if view.round_index < 2:  # 11 skips the last question
                await self._click(11, self.guild_a, channel, view, right=False)

        channel = FakeChannel(100, on_question)
        loop = asyncio.get_running_loop()
        began = loop.time()
        game = await self._start(_interaction(10, self.guild_a, channel), seconds=1.0)
        await game.task
        # Question 1 runs its full second (nobody is expected yet), question 2
        # ends as soon as both have answered, and question 3 waits out its
        # second for 11, who answered question 2 but skips this one.
        elapsed = loop.time() - began
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertLess(elapsed, 2.8)
        self.assertEqual(channel.titles()[-1], "🏆 Trivia results")
        rows = await self.db.trivia_leaderboard(guild_id="1")
        self.assertEqual([(r["UserId"], r["Correct"]) for r in rows], [("10", 3), ("11", 0)])

    async def test_one_match_per_channel_and_stop(self) -> None:
        channel = FakeChannel(100)
        game = await self._start(_interaction(10, self.guild_a, channel), seconds=5)
        busy = _interaction(11, self.guild_a, channel)
        await self._start(busy)
        self.assertIn("already running", busy.replies[0][0])
        stranger = _interaction(12, self.guild_a, channel)
        await self.cog.stop.callback(self.cog, stranger)
        self.assertIn("Only whoever started", stranger.replies[0][0])
        mod = _interaction(13, self.guild_a, channel, manage=True)
        await self.cog.stop.callback(self.cog, mod)
        await game.task
        self.assertEqual(channel.titles()[-1], "Trivia stopped")
        self.assertNotIn(100, self.cog.games)

    async def test_cross_server_match_shares_questions_and_scoreboard(self) -> None:
        await self.db.set_trivia_settings("2", allow_global=True, announce_channel_id="300")
        invite_channel = FakeChannel(300)
        self.channels[300] = invite_channel

        async def answer_a(channel, view):
            await self._click(10, self.guild_a, channel, view, right=True)

        async def answer_b(channel, view):
            await self._click(20, self.guild_b, channel, view, right=True)
            await self._click(21, self.guild_b, channel, view, right=True)

        chan_a = FakeChannel(100, answer_a)
        chan_b = FakeChannel(200, answer_b)
        mode = SimpleNamespace(value="global")
        game = await self._start(_interaction(10, self.guild_a, chan_a), mode=mode)
        self.assertIs(self.cog.lobby, game)

        # Bears got an invitation with a Join button; joining from another channel works too.
        (invite,) = invite_channel.sent
        self.assertIsInstance(invite.view, JoinView)
        joiner = _interaction(20, self.guild_b, chan_b)
        await self.cog.join.callback(self.cog, joiner)
        self.assertEqual(set(game.seats), {100, 200})
        servers = joiner.original.embed.fields[0]
        self.assertEqual(servers.name, "Servers in (2)")
        self.assertIn("Bears", servers.value)

        await game.task
        self.assertIsNone(self.cog.lobby)
        self.assertEqual(invite.edits[-1]["view"], None)  # invitation closed
        for chan in (chan_a, chan_b):
            self.assertEqual(chan.titles()[:3], ["Question 1/3", "Question 2/3", "Question 3/3"])
        # Same questions, in the same order, in both servers.
        self.assertEqual(
            [m.embed.description for m in chan_a.sent[:3]],
            [m.embed.description for m in chan_b.sent[:3]],
        )
        final = chan_b.sent[-1].embed
        server_field = next(f for f in final.fields if f.name == "Servers")
        self.assertTrue(server_field.value.startswith("1. **Bears**"))
        self.assertIn("(Wolves)", final.fields[0].value)
        rows = await self.db.trivia_leaderboard(guild_id=None)
        self.assertEqual({(r["UserId"], r["GuildName"]) for r in rows},
                         {("10", "Wolves"), ("20", "Bears"), ("21", "Bears")})

    async def test_cross_server_respects_opt_out_and_cooldown(self) -> None:
        await self.db.set_trivia_settings("2", allow_global=False, announce_channel_id=None)
        game = await self._start(
            _interaction(10, self.guild_a, FakeChannel(100)),
            mode=SimpleNamespace(value="global"), seconds=5,
        )
        blocked = _interaction(20, self.guild_b, FakeChannel(200))
        await self.cog.join.callback(self.cog, blocked)
        self.assertIn("turned off in this server", blocked.replies[0][0])
        game.stop_event.set()
        await game.task
        again = _interaction(10, self.guild_a, FakeChannel(101))
        await self._start(again, mode=SimpleNamespace(value="global"))
        self.assertIn("recently", again.replies[0][0])

    async def test_stopping_in_the_lobby_updates_the_lobby_message(self) -> None:
        host = _interaction(10, self.guild_a, FakeChannel(100))
        game = await self._start(host, mode=SimpleNamespace(value="global"), seconds=5)
        self.assertIn("First question", host.original.embed.description)
        await self.cog.stop.callback(self.cog, _interaction(10, self.guild_a, host.channel))
        await game.task
        self.assertIn("Stopped by user10 before the first question.", host.original.embed.description)
        self.assertNotIn("First question", host.original.embed.description)

    async def test_leaving_a_cross_server_match_lets_others_continue(self) -> None:
        chan_a, chan_b = FakeChannel(100), FakeChannel(200)
        host = _interaction(10, self.guild_a, chan_a)
        game = await self._start(host, mode=SimpleNamespace(value="global"), seconds=5)
        # Starting cross-server while a lobby is open joins it.
        joiner = _interaction(20, self.guild_b, chan_b)
        await self._start(joiner, mode=SimpleNamespace(value="global"))
        self.assertIn("already open", joiner.replies[0][0])
        leaver = _interaction(10, self.guild_a, chan_a)
        await self.cog.stop.callback(self.cog, leaver)
        self.assertIn("left the cross-server match", leaver.replies[0][0])
        self.assertEqual(set(game.seats), {200})
        self.assertFalse(game.stop_event.is_set())
        self.assertIn("This channel left before the first question", host.original.embed.description)
        self.assertEqual(joiner.original.embed.fields[0].name, "Servers in (1)")
        await self.cog.stop.callback(self.cog, _interaction(20, self.guild_b, chan_b))
        await game.task
        self.assertEqual(chan_b.titles()[-1], "Trivia stopped")
        self.assertEqual(self.cog.games, {})

    async def test_bad_custom_bank_falls_back_to_bundled(self) -> None:
        bad = Path(self._tmp.name) / "bank.json"
        bad.write_text('[{"question": "Q?"}]')
        bot = SimpleNamespace(settings=SimpleNamespace(trivia_questions_path=bad))
        with self.assertLogs("bot.cogs.trivia", level="ERROR"):
            cog = Trivia(bot)  # type: ignore[arg-type]
        self.assertEqual(len(cog.bank), len(load_bank()))

    async def test_unknown_category_is_rejected(self) -> None:
        inter = _interaction(10, self.guild_a, FakeChannel(100))
        await self._start(inter, category="Basket weaving")
        self.assertIn("Unknown category", inter.replies[0][0])
        self.assertEqual(self.cog.games, {})


if __name__ == "__main__":
    unittest.main()
