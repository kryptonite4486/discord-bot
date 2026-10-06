"""Trivia match state and scoring, independent of Discord.

A match is a list of questions asked one at a time. While a round is open,
each player may lock in one answer; the first answer counts and can't be
changed. A correct answer scores ``BASE_POINTS`` plus a speed bonus of up to
``SPEED_BONUS`` that shrinks linearly over the answer window.

Players are keyed by Discord user ID. In a cross-server match a player keeps
the server they first answered from, so their points count for that server.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from bot.trivia.questions import AskedQuestion

BASE_POINTS = 100
SPEED_BONUS = 50


class Mode(str, enum.Enum):
    SERVER = "server"
    GLOBAL = "global"


class AnswerResult(enum.Enum):
    LOCKED = "locked"
    ALREADY_ANSWERED = "already"
    CLOSED = "closed"


@dataclass
class Player:
    user_id: int
    guild_id: str
    guild_name: str
    name: str
    points: int = 0
    correct: int = 0
    answered: int = 0


@dataclass
class RoundResult:
    index: int
    asked: AskedQuestion
    # (player, points earned) for correct answers, fastest first.
    winners: list[tuple[Player, int]]
    choice_counts: list[int]
    answered: int


@dataclass
class Match:
    mode: Mode
    questions: list[AskedQuestion]
    seconds: float
    players: dict[int, Player] = field(default_factory=dict)
    index: int = -1
    _opened_at: float | None = None
    # user_id -> (choice, seconds after the round opened)
    _answers: dict[int, tuple[int, float]] = field(default_factory=dict)

    @property
    def current(self) -> AskedQuestion | None:
        if 0 <= self.index < len(self.questions):
            return self.questions[self.index]
        return None

    @property
    def round_open(self) -> bool:
        return self._opened_at is not None

    @property
    def has_next(self) -> bool:
        return self.index + 1 < len(self.questions)

    def open_round(self, now: float) -> AskedQuestion:
        if self.round_open:
            raise RuntimeError("Previous round is still open")
        if not self.has_next:
            raise RuntimeError("No questions left")
        self.index += 1
        self._opened_at = now
        self._answers = {}
        return self.questions[self.index]

    def answer(
        self,
        user_id: int,
        choice: int,
        now: float,
        *,
        guild_id: str,
        guild_name: str,
        name: str,
    ) -> AnswerResult:
        if self._opened_at is None:
            return AnswerResult.CLOSED
        elapsed = now - self._opened_at
        if elapsed > self.seconds:
            return AnswerResult.CLOSED
        if user_id in self._answers:
            return AnswerResult.ALREADY_ANSWERED
        player = self.players.get(user_id)
        if player is None:
            player = self.players[user_id] = Player(user_id, guild_id, guild_name, name)
        else:
            player.name = name  # keep the latest display name
        self._answers[user_id] = (choice, max(0.0, elapsed))
        return AnswerResult.LOCKED

    def points_for(self, elapsed: float) -> int:
        remaining = max(0.0, 1.0 - elapsed / self.seconds) if self.seconds else 0.0
        return BASE_POINTS + round(SPEED_BONUS * remaining)

    def close_round(self) -> RoundResult:
        asked = self.current
        if asked is None or self._opened_at is None:
            raise RuntimeError("No round is open")
        self._opened_at = None
        counts = [0] * len(asked.choices)
        winners: list[tuple[Player, int, float]] = []
        for user_id, (choice, elapsed) in self._answers.items():
            player = self.players[user_id]
            player.answered += 1
            if 0 <= choice < len(counts):
                counts[choice] += 1
            if choice == asked.answer_index:
                earned = self.points_for(elapsed)
                player.points += earned
                player.correct += 1
                winners.append((player, earned, elapsed))
        winners.sort(key=lambda w: w[2])
        return RoundResult(
            index=self.index,
            asked=asked,
            winners=[(p, pts) for p, pts, _ in winners],
            choice_counts=counts,
            answered=len(self._answers),
        )

    def standings(self) -> list[Player]:
        """Players by points, then correct answers; ties keep join order."""
        return sorted(
            self.players.values(), key=lambda p: (-p.points, -p.correct)
        )

    def winners(self) -> list[Player]:
        """Top scorer(s), or nobody when no one scored."""
        table = self.standings()
        if not table or table[0].points <= 0:
            return []
        return [p for p in table if p.points == table[0].points]

    def guild_totals(self) -> list[tuple[str, str, int, int]]:
        """(guild_id, guild_name, points, players) per server, best first."""
        totals: dict[str, list] = {}
        for p in self.players.values():
            row = totals.setdefault(p.guild_id, [p.guild_name, 0, 0])
            row[1] += p.points
            row[2] += 1
        return sorted(
            ((gid, name, pts, n) for gid, (name, pts, n) in totals.items()),
            key=lambda r: -r[2],
        )
