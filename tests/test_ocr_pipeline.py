"""Unit + optional integration tests for OCR leaderboard parsing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.ocr.pipeline import (  # noqa: E402
    _clean_player_name,
    _cluster_rows,
    _pick_best_score,
    _recover_false_five_commas,
    _score_string_candidates,
    _split_merged_name_score,
    _try_join_score_parts,
    parse_leaderboard_rows,
)

SAMPLES = ROOT / "samples"


def _tok(text: str, x: float, y: float, half_h: float = 8.0) -> tuple[str, float, tuple]:
    """Build a synthetic OCR token (text, conf, bbox meta)."""
    return (text, 0.95, (x, y, y - half_h, y + half_h))


def _have_easyocr() -> bool:
    try:
        import cv2  # noqa: F401
        import easyocr  # noqa: F401
    except ImportError:
        return False
    return SAMPLES.joinpath("power_leaderboard.png").is_file()


class SplitMergedScoreTests(unittest.TestCase):
    def test_full_score_glued_to_name(self) -> None:
        name, score = _split_merged_name_score("EnemyHelicopter112,257,938")
        self.assertEqual(name, "EnemyHelicopter")
        self.assertEqual(score, "112,257,938")

    def test_partial_score_with_junk(self) -> None:
        name, score = _split_merged_name_score("EnemyHlelicopiej12,257=")
        self.assertEqual(name, "EnemyHlelicopie")
        self.assertEqual(score, "112,257")

    def test_trailing_name_digits_not_split(self) -> None:
        name, score = _split_merged_name_score("zena75")
        self.assertIsNone(name)
        self.assertIsNone(score)

    def test_clean_strips_equals(self) -> None:
        self.assertEqual(_clean_player_name("EnemyHelicopter="), "EnemyHelicopter")


class ScoreRecoveryTests(unittest.TestCase):
    def test_false_five_commas(self) -> None:
        self.assertEqual(_recover_false_five_commas("1275560"), "127,560")
        self.assertEqual(_recover_false_five_commas("3056585131"), "30,658,131")
        self.assertEqual(_recover_false_five_commas("1615180"), "161,180")

    def test_glued_tail_after_comma(self) -> None:
        best = _pick_best_score(_score_string_candidates("112,2579938"))
        self.assertIsNotNone(best)
        self.assertEqual(best[1], "112,257,938")
        self.assertEqual(best[0], 112_257_938.0)

    def test_len7_junk_separator(self) -> None:
        best = _pick_best_score(_score_string_candidates("1673040"))
        self.assertEqual(best[1], "167,040")
        best = _pick_best_score(_score_string_candidates("1675040"))
        self.assertEqual(best[1], "167,040")

    def test_duplicated_fours_with_false_five(self) -> None:
        best = _pick_best_score(_score_string_candidates("3356444447"))
        self.assertEqual(best[1], "33,644,447")
        self.assertEqual(best[0], 33_644_447.0)

    def test_join_score_fragments(self) -> None:
        self.assertEqual(_try_join_score_parts("112,257", "25938"), "112,257,938")
        self.assertEqual(_try_join_score_parts("112,257", "73938"), "112,257,938")
        self.assertEqual(_try_join_score_parts("112,257", "'9938"), "112,257,938")


class PowerLeaderboardParseTests(unittest.TestCase):
    def test_clean_tokens_five_rows(self) -> None:
        # Ground-truth layout: name + alliance under name + large score on the right
        items = [
            _tok("1", 20, 40),
            _tok("EnemyHelicopter", 120, 40),
            _tok("[BNG]BANG", 120, 58),
            _tok("112,257,938", 520, 40),
            _tok("2", 20, 110),
            _tok("KBeezCONC", 120, 110),
            _tok("[BNG]BANG", 120, 128),
            _tok("65,521,967", 520, 110),
            _tok("3", 20, 180),
            _tok("EscapefromNY", 120, 180),
            _tok("[BNG]BANG", 120, 198),
            _tok("56,705,138", 520, 180),
            _tok("4", 20, 250),
            _tok("TRACEofBUFFALO", 120, 250),
            _tok("[BNG]BANG", 120, 268),
            _tok("33,644,447", 520, 250),
            _tok("5", 20, 320),
            _tok("zena75", 120, 320),
            _tok("[BNG]BANG", 120, 338),
            _tok("30,658,131", 520, 320),
        ]
        metrics = parse_leaderboard_rows(items, "VersusPoints")
        by_name = {m.player_name: m.value for m in metrics}
        self.assertEqual(by_name["EnemyHelicopter"], 112_257_938.0)
        self.assertEqual(by_name["KBeezCONC"], 65_521_967.0)
        self.assertEqual(by_name["EscapefromNY"], 56_705_138.0)
        self.assertEqual(by_name["TRACEofBUFFALO"], 33_644_447.0)
        self.assertEqual(by_name["zena75"], 30_658_131.0)
        self.assertEqual(len(metrics), 5)

    def test_merged_name_score_recovers_enemy_helicopter(self) -> None:
        # Production failure: name glued to partial score + leftover digits
        items = [
            _tok("1", 20, 40),
            _tok("EnemyHlelicopiej12,257=", 140, 40),
            _tok("[BNG]BANG", 120, 58),
            _tok("73938", 520, 40),
            _tok("2", 20, 110),
            _tok("KBeezCONC", 120, 110),
            _tok("[BNG]BANG", 120, 128),
            _tok("65,521,967", 520, 110),
        ]
        metrics = parse_leaderboard_rows(items, "VersusPoints")
        top = next(m for m in metrics if "Enemy" in m.player_name)
        self.assertEqual(top.player_name, "EnemyHlelicopie")
        self.assertEqual(top.value, 112_257_938.0)
        self.assertFalse(re_search_digits_in_name(top.player_name))

    def test_real_easyocr_token_layout_power_board(self) -> None:
        """Synthetic tokens matching observed EasyOCR output (upscaled crop)."""
        # Y gaps mirror alliance-under-name boards that used to yield 0 rows
        h = 28
        items = [
            _tok("EnemyHelicopter", 400, 67, half_h=h),
            _tok("112,2579938", 900, 106, half_h=h),
            _tok("[BNG]BANG", 400, 139, half_h=20),
            _tok("KBeezCONC", 400, 272, half_h=h),
            _tok("2", 50, 310, half_h=h),
            _tok("65,521,967", 900, 312, half_h=h),
            _tok("[BNG]BANG", 400, 347, half_h=20),
            _tok("EscapeftomNY", 400, 483, half_h=h),
            _tok("3", 50, 516, half_h=h),
            _tok("56,705,138", 900, 522, half_h=h),
            _tok("[BNG]BANG", 400, 554, half_h=20),
            _tok("TRACEoTBUFFALO", 400, 687, half_h=h),
            _tok("4", 50, 727, half_h=h),
            _tok("3356444447", 900, 727, half_h=h),
            _tok("[BNG]BANG", 400, 762, half_h=20),
            _tok("zena75", 400, 895, half_h=h),
            _tok("5", 50, 934, half_h=h),
            _tok("3056585131", 900, 935, half_h=h),
            _tok("[BNG]BANG", 400, 970, half_h=20),
        ]
        rows = _cluster_rows(items)
        self.assertGreaterEqual(len(rows), 5)
        metrics = parse_leaderboard_rows(items, "VersusPoints")
        by_name = {m.player_name: m.value for m in metrics}
        self.assertEqual(by_name.get("EnemyHelicopter"), 112_257_938.0)
        self.assertEqual(by_name.get("KBeezCONC"), 65_521_967.0)
        self.assertEqual(by_name.get("EscapeftomNY"), 56_705_138.0)
        self.assertEqual(by_name.get("TRACEoTBUFFALO"), 33_644_447.0)
        self.assertEqual(by_name.get("zena75"), 30_658_131.0)
        self.assertEqual(len(metrics), 5)

    def test_alliance_tag_y_gap_does_not_orphan_scores(self) -> None:
        """Name at y=330 and score at y=378 must still form one row (y_tol was 28)."""
        items = [
            _tok("KBeezCONC", 120, 330, half_h=28),
            _tok("2", 20, 376, half_h=30),
            _tok("65,521,967", 520, 378, half_h=35),
            _tok("[BNG]BANG", 120, 420, half_h=20),
        ]
        metrics = parse_leaderboard_rows(items, "Power")
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].player_name, "KBeezCONC")
        self.assertEqual(metrics[0].value, 65_521_967.0)

    def test_fully_merged_correct_score(self) -> None:
        items = [
            _tok("EnemyHelicopter112,257,938", 200, 40),
            _tok("[BNG]BANG", 120, 58),
        ]
        metrics = parse_leaderboard_rows(items, "VersusPoints")
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].player_name, "EnemyHelicopter")
        self.assertEqual(metrics[0].value, 112_257_938.0)

    def test_versus_scale_scores_still_work(self) -> None:
        items = [
            _tok("1Parzival1", 100, 40),
            _tok("167,040", 400, 40),
            _tok("93NAT93", 100, 100),
            _tok("161,180", 400, 100),
        ]
        metrics = parse_leaderboard_rows(items, "VersusPoints")
        by_name = {m.player_name: m.value for m in metrics}
        self.assertEqual(by_name["1Parzival1"], 167_040.0)
        self.assertEqual(by_name["93NAT93"], 161_180.0)

    def test_versus_false_five_scores(self) -> None:
        items = [
            _tok("1Parzival1", 100, 40),
            _tok("1675040", 400, 40),
            _tok("Babylon 4", 100, 100),
            _tok("1275560", 400, 100),
            _tok("Zeus el great", 100, 160),
            _tok("1175660", 400, 160),
            _tok("lastwarrior", 100, 220),
            _tok("1175600", 400, 220),
        ]
        metrics = parse_leaderboard_rows(items, "VersusPoints")
        by_name = {m.player_name: m.value for m in metrics}
        self.assertEqual(by_name["1Parzival1"], 167_040.0)
        self.assertEqual(by_name["Babylon 4"], 127_560.0)
        self.assertEqual(by_name["Zeus el great"], 117_660.0)
        self.assertEqual(by_name["lastwarrior"], 117_600.0)


@unittest.skipUnless(_have_easyocr(), "easyocr/cv2 or sample PNGs not available")
class SampleImageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from bot.ocr.pipeline import OCREngine

        cls.engine = OCREngine("easyocr")

    def test_power_leaderboard_png(self) -> None:
        from bot.ocr.pipeline import extract_metrics_from_image

        path = SAMPLES / "power_leaderboard.png"
        for kind in ("versus", "power"):
            with self.subTest(kind=kind):
                res = extract_metrics_from_image(path, kind=kind, engine=self.engine)
                self.assertGreaterEqual(len(res.metrics), 5, res.warnings)
                by_name = {m.player_name: m.value for m in res.metrics}
                self.assertIn("EnemyHelicopter", by_name)
                self.assertEqual(by_name["EnemyHelicopter"], 112_257_938.0)
                self.assertEqual(by_name.get("KBeezCONC"), 65_521_967.0)
                self.assertEqual(by_name.get("zena75"), 30_658_131.0)
                # TRACE / Escape names may have OCR letter swaps; match by value
                values = {m.value for m in res.metrics}
                self.assertIn(56_705_138.0, values)
                self.assertIn(33_644_447.0, values)

    def test_kills_leaderboard_webp(self) -> None:
        from bot.ocr.pipeline import extract_metrics_from_image

        path = SAMPLES / "kills_leaderboard.webp"
        res = extract_metrics_from_image(path, kind="kills", engine=self.engine)
        self.assertEqual(len(res.metrics), 8, res.raw_text)
        self.assertTrue(all(m.metric_type == "Kills" for m in res.metrics))
        by_name = {m.player_name: m.value for m in res.metrics}
        self.assertEqual(by_name["EnemyHelicopter"], 1_078_263.0)
        self.assertEqual(by_name["KBeezCONC"], 1_005_842.0)
        self.assertEqual(by_name["2Face"], 688_506.0)
        self.assertEqual(by_name["EscapefromNY"], 585_082.0)
        self.assertEqual(by_name["Vexx23"], 512_140.0)
        self.assertEqual(by_name["Louis153"], 443_341.0)
        # Avatar art text and 0/O swaps can distort names; match by value.
        values = {m.value for m in res.metrics}
        self.assertIn(815_445.0, values)
        self.assertIn(601_931.0, values)

    def test_versus_leaderboard_png(self) -> None:
        from bot.ocr.pipeline import extract_metrics_from_image

        path = SAMPLES / "versus_leaderboard.png"
        res = extract_metrics_from_image(path, kind="versus", engine=self.engine)
        self.assertEqual(len(res.metrics), 7, res.raw_text)
        by_name = {m.player_name: m.value for m in res.metrics}
        self.assertEqual(by_name["1Parzival1"], 167_040.0)
        self.assertEqual(by_name["93NAT93"], 161_180.0)
        self.assertEqual(by_name["EscapefromNY"], 144_960.0)
        self.assertEqual(by_name["Babylon 4"], 127_560.0)
        self.assertEqual(by_name["zippyfinny"], 121_220.0)
        self.assertEqual(by_name["Zeus el great"], 117_660.0)
        self.assertEqual(by_name["lastwarrior"], 117_600.0)

    def test_general_profile_png(self) -> None:
        from bot.ocr.pipeline import extract_metrics_from_image

        path = SAMPLES / "general_profile.png"
        res = extract_metrics_from_image(path, kind="general", engine=self.engine)
        by_type = {m.metric_type: m for m in res.metrics}
        self.assertIn("HQLevel", by_type)
        self.assertIn("Power", by_type)
        self.assertEqual(by_type["HQLevel"].value, 24.0)
        self.assertAlmostEqual(by_type["Power"].value, 65_400_000.0, delta=1.0)
        self.assertIn("PrincessPea", by_type["HQLevel"].player_name)


def re_search_digits_in_name(name: str) -> bool:
    """True if a comma-formatted score leftover is still stuck on the name."""
    import re

    return bool(re.search(r"\d{1,3}(?:,\d{3})+", name))


if __name__ == "__main__":
    unittest.main()
