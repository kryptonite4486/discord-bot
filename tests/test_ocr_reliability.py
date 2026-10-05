"""OCR reliability: retries, reply-length cap, bot-wide queue, batch outcomes.

The vision server is faked here; tests/test_vision_samples.py covers the real one.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Allow `python tests/test_ocr_reliability.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from bot.cogs.ingest import Ingest, _summary_head  # noqa: E402
from bot.ocr import vision  # noqa: E402
from bot.utils.archive import ImageSource  # noqa: E402

SAMPLE = ROOT / "samples" / "kills_leaderboard.webp"
GOOD_REPLY = '[{"player": "EnemyHelicopter", "value": "1,078,263"}]'


def _extract():
    return vision.extract_metrics_via_vision(
        SAMPLE, kind="kills", base_url="http://x/v1", model="m", timeout=5
    )


@patch.object(vision.time, "sleep", lambda _s: None)
class RetryTests(unittest.TestCase):
    def test_dropped_connection_is_retried(self) -> None:
        calls = [vision.VisionTransientError("peer closed connection"), GOOD_REPLY]
        with patch.object(vision, "_chat_completions", side_effect=calls) as chat:
            result = _extract()
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(result.metrics[0].value, 1_078_263.0)

    def test_unreadable_reply_is_retried_then_fails(self) -> None:
        with patch.object(
            vision, "_chat_completions", return_value='[{"player": "Enem'
        ) as chat, self.assertLogs("bot.ocr.vision", "WARNING") as logs:
            with self.assertRaises(vision.VisionReplyError):
                _extract()
        self.assertEqual(chat.call_count, 1 + len(vision.RETRY_DELAYS))
        # The bad reply is logged for diagnosis.
        self.assertTrue(any("Enem" in line for line in logs.output))

    def test_unreadable_then_good_reply(self) -> None:
        with patch.object(
            vision, "_chat_completions", side_effect=["not json", GOOD_REPLY]
        ):
            self.assertEqual(len(_extract().metrics), 1)

    def test_empty_list_is_not_retried(self) -> None:
        # A readable "no players" reply is an answer, not a failure.
        with patch.object(vision, "_chat_completions", return_value="[]") as chat:
            result = _extract()
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(result.metrics, [])

    def test_client_errors_are_not_retried(self) -> None:
        with patch.object(
            vision, "_chat_completions", side_effect=vision.VisionOCRError("HTTP 401")
        ) as chat:
            with self.assertRaises(vision.VisionOCRError):
                _extract()
        self.assertEqual(chat.call_count, 1)


def _fake_client(response: httpx.Response | Exception) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    if isinstance(response, Exception):
        client.post.side_effect = response
    else:
        client.post.return_value = response
    return client


class ChatCompletionsTests(unittest.TestCase):
    def _call(self, response: httpx.Response | Exception) -> tuple[str, MagicMock]:
        client = _fake_client(response)
        with patch("httpx.Client", return_value=client):
            text = vision._chat_completions(
                base_url="http://x/v1",
                model="m",
                api_key="",
                timeout=5,
                prompt="p",
                data_url="data:,",
            )
        return text, client

    def test_request_caps_reply_length(self) -> None:
        ok = httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})
        text, client = self._call(ok)
        self.assertEqual(text, "[]")
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["max_tokens"], vision.MAX_TOKENS)
        self.assertEqual(body["temperature"], 0)

    def test_error_classification(self) -> None:
        cases = [
            (httpx.RemoteProtocolError("peer closed connection"), vision.VisionTransientError),
            (httpx.Response(500, text="metal::malloc"), vision.VisionTransientError),
            (httpx.Response(200, json={"choices": []}), vision.VisionTransientError),
            (httpx.Response(401, text="API key required"), vision.VisionOCRError),
        ]
        for response, expected in cases:
            with self.subTest(response=response):
                with self.assertRaises(expected) as ctx:
                    self._call(response)
                if expected is vision.VisionOCRError:
                    self.assertNotIsInstance(ctx.exception, vision.VisionTransientError)


def _cog(max_concurrency: int = 1) -> Ingest:
    settings = SimpleNamespace(
        ocr_vision_base_url="http://x/v1",
        ocr_vision_model="m",
        ocr_vision_api_key="",
        ocr_vision_timeout=5.0,
        ocr_max_concurrency=max_concurrency,
    )
    usage: list[tuple[str, str, dict]] = []

    async def add_usage(guild_id, day, amounts):
        usage.append((guild_id, day, amounts))

    db = SimpleNamespace(add_usage=add_usage, usage=usage)
    return Ingest(SimpleNamespace(settings=settings, db=db))  # type: ignore[arg-type]


def _images(prefix: str, n: int) -> list[ImageSource]:
    return [ImageSource(key=f"{prefix}{i}", filename=f"{prefix}{i}.png", data=b"") for i in range(n)]


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_batches_run_one_at_a_time(self) -> None:
        cog = _cog()
        running = 0
        peak = 0

        async def fake_process(image, *args):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return "detail", 1, {"P"}, []

        queued: list[str] = []

        async def progress(index: int, total: int, text: str) -> None:
            if index == 0:
                queued.append(text)

        with patch.object(cog, "_process_attachment", side_effect=fake_process):
            await asyncio.gather(
                *(
                    cog._process_attachments(
                        _images(p, 3), "kills", "2026-10-04", "g", "c", progress=progress
                    )
                    for p in "abc"
                )
            )
        self.assertEqual(peak, 1)
        # The two later batches were told they were queued.
        self.assertEqual(len(queued), 2)
        self.assertEqual((cog.ocr_queue.running, cog.ocr_queue.waiting), (0, 0))
        # One usage record per batch; batches that queued record the wait.
        usage = cog.bot.db.usage
        self.assertEqual(len(usage), 3)
        self.assertEqual(sum(a["ocr_images"] for _, _, a in usage), 9)
        waits = sorted(a["ocr_wait_seconds"] for _, _, a in usage)
        self.assertLess(waits[0], 0.01)
        self.assertGreater(waits[-1], 0.03)  # waited behind two 3-image batches

    async def test_servers_take_turns_and_requests_stay_whole(self) -> None:
        cog = _cog()
        started: list[str] = []
        release = asyncio.Event()

        async def fake_process(image, kind, week, guild_id, *args):
            await release.wait()
            started.append(image.filename)
            await asyncio.sleep(0)
            return "detail", 1, {"P"}, []

        with patch.object(cog, "_process_attachment", side_effect=fake_process):
            tasks = []
            for guild, prefix in (("A", "a"), ("A", "x"), ("A", "y"), ("B", "b")):
                tasks.append(
                    asyncio.create_task(
                        cog._process_attachments(_images(prefix, 3), "kills", "w", guild, "c")
                    )
                )
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*tasks)
        # A's first request runs whole, then B's, then A's backlog.
        self.assertEqual(
            started,
            [f"{p}{i}.png" for p in ("a", "b", "x", "y") for i in range(3)],
        )

    async def test_queued_message_never_mentions_other_servers(self) -> None:
        cog = _cog()
        release = asyncio.Event()
        notices: dict[str, list[str]] = {"A": [], "B": []}

        async def fake_process(*args):
            await release.wait()
            return "detail", 1, {"P"}, []

        def progress_for(guild):
            async def progress(index, total, text):
                if index == 0:
                    notices[guild].append(text)
            return progress

        with patch.object(cog, "_process_attachment", side_effect=fake_process):
            tasks = []
            for guild in ("A", "A", "B"):
                tasks.append(
                    asyncio.create_task(
                        cog._process_attachments(
                            _images(guild, 1), "kills", "w", guild, "c",
                            progress=progress_for(guild),
                        )
                    )
                )
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(notices["A"], ["1 queued ahead"])
        self.assertEqual(notices["B"], [])  # waiting only on another server

    async def test_concurrency_setting_is_respected(self) -> None:
        cog = _cog(max_concurrency=2)
        running = 0
        peak = 0

        async def fake_process(image, *args):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return "detail", 1, {"P"}, []

        with patch.object(cog, "_process_attachment", side_effect=fake_process):
            await asyncio.gather(
                *(
                    cog._process_attachments(_images(p, 2), "kills", "w", "g", "c")
                    for p in "abc"
                )
            )
        self.assertEqual(peak, 2)

    async def test_summary_separates_saved_empty_and_failed(self) -> None:
        cog = _cog()
        outcomes = {
            "ok.png": ("detail", 8, {"A"}, []),
            "empty.png": ("detail", 0, set(), []),
        }

        async def fake_process(image, *args):
            if image.filename in outcomes:
                return outcomes[image.filename]
            raise vision.VisionReplyError("model reply was not valid JSON")

        images = [
            ImageSource(key=name, filename=name, data=b"")
            for name in ("ok.png", "empty.png", "shots.zip/bad.png")
        ]
        with patch.object(cog, "_process_attachment", side_effect=fake_process):
            summary = await cog._process_attachments(images, "kills", "w", "g", "c")
        head = _summary_head(summary)
        self.assertIn("✅ 1 saved · ⚠️ 1 no players found · ❌ 1 failed", head)
        self.assertIn("Re-upload the failed image(s): `bad.png`", head)
        self.assertNotIn("Per file", head)
        self.assertIn("— ⚠️ no players found", summary)
        self.assertIn("— ❌ **failed**", summary)
        (guild_id, _, amounts), = cog.bot.db.usage
        self.assertEqual(guild_id, "g")
        self.assertEqual(amounts["ocr_images"], 3)  # failed images still cost OCR time
        self.assertEqual(amounts["ocr_failed"], 1)
        self.assertEqual(amounts["ocr_batches"], 1)


if __name__ == "__main__":
    unittest.main()
