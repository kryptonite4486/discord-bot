"""Tests for zip extraction and image-source merging (no Discord/OCR required)."""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

# Allow `python tests/test_archive.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from bot.cogs.ingest import MAX_BATCH_IMAGES, MAX_INGEST_IMAGES, Ingest  # noqa: E402
from bot.utils.archive import (  # noqa: E402
    ArchiveError,
    ImageSource,
    extract_images_from_zip,
    is_zip_upload,
)


def _png(color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _extract(data: bytes, **kwargs):
    return extract_images_from_zip(
        data, archive_name="shots.zip", key_prefix="zip:1", **kwargs
    )


class ExtractTests(unittest.TestCase):
    def test_images_sorted_and_tagged(self) -> None:
        data = _zip({"b.png": _png(), "A.png": _png(), "c.jpg": _png()})
        images, warnings = _extract(data)
        self.assertEqual(
            [i.filename for i in images],
            ["shots.zip/A.png", "shots.zip/b.png", "shots.zip/c.jpg"],
        )
        self.assertTrue(all(i.from_archive for i in images))
        self.assertEqual(images[0].key, "zip:1:A.png")
        self.assertEqual(warnings, [])

    def test_skips_junk_entries(self) -> None:
        data = _zip(
            {
                "week1/one.png": _png(),
                "__MACOSX/week1/._one.png": b"junk",
                ".DS_Store": b"junk",
                "notes.txt": b"hello",
                "week1/.hidden.png": _png(),
            }
        )
        images, warnings = _extract(data)
        self.assertEqual([i.filename for i in images], ["shots.zip/week1/one.png"])
        # Only notes.txt is worth mentioning; macOS metadata and hidden files are silent.
        self.assertEqual(warnings, ["Skipped 1 non-image file(s) in `shots.zip`."])

    def test_finder_zip_has_no_false_warning(self) -> None:
        # Finder's "Compress" adds a __MACOSX/._name metadata file per image.
        members = {}
        for n in range(3):
            members[f"vs/vs_{n}.png"] = _png()
            members[f"__MACOSX/vs/._vs_{n}.png"] = b"\0\5\26\7"
        images, warnings = _extract(_zip(members))
        self.assertEqual(len(images), 3)
        self.assertEqual(warnings, [])

    def test_path_traversal_entries_skipped(self) -> None:
        data = _zip({"../../evil.png": _png(), "ok.png": _png()})
        images, _ = _extract(data)
        self.assertEqual([i.filename for i in images], ["shots.zip/ok.png"])

    def test_corrupt_image_skipped(self) -> None:
        data = _zip({"good.png": _png(), "bad.png": b"not an image"})
        images, warnings = _extract(data)
        self.assertEqual([i.filename for i in images], ["shots.zip/good.png"])
        self.assertTrue(any("bad.png" in w for w in warnings))

    def test_image_cap(self) -> None:
        data = _zip({f"{n:02}.png": _png() for n in range(5)})
        images, warnings = _extract(data, max_images=3)
        self.assertEqual(len(images), 3)
        self.assertTrue(any("first 3" in w for w in warnings))

    def test_member_size_cap(self) -> None:
        big = _png() + b"\0" * 5000
        data = _zip({"big.png": big, "ok.png": _png()})
        images, warnings = _extract(data, max_member_bytes=1000)
        self.assertEqual([i.filename for i in images], ["shots.zip/ok.png"])
        self.assertTrue(any("big.png" in w for w in warnings))

    def test_total_size_cap_raises(self) -> None:
        data = _zip({f"{n}.png": _png() for n in range(5)})
        with self.assertRaises(ArchiveError):
            _extract(data, max_total_bytes=len(_png()) * 2)

    def test_not_a_zip(self) -> None:
        with self.assertRaises(ArchiveError):
            _extract(b"definitely not a zip")

    def test_empty_zip(self) -> None:
        images, warnings = _extract(_zip({}))
        self.assertEqual(images, [])
        self.assertEqual(warnings, [])

    def test_is_zip_upload(self) -> None:
        self.assertTrue(is_zip_upload("Shots.ZIP", None))
        self.assertTrue(is_zip_upload("upload", "application/zip"))
        self.assertTrue(is_zip_upload("upload", "application/x-zip-compressed"))
        self.assertFalse(is_zip_upload("shot.png", "image/png"))


def _src(key: str, *, archive: bool) -> ImageSource:
    return ImageSource(key=key, filename=key, data=b"", from_archive=archive)


class MergeTests(unittest.TestCase):
    def test_dedupes_by_key(self) -> None:
        a = _src("att:1", archive=False)
        merged = Ingest._merge_sources([a], [a, _src("att:2", archive=False)], limit=10)
        self.assertEqual([s.key for s in merged], ["att:1", "att:2"])

    def test_loose_images_keep_old_cap(self) -> None:
        loose = [_src(f"att:{n}", archive=False) for n in range(30)]
        merged = Ingest._merge_sources([], loose, limit=MAX_BATCH_IMAGES)
        self.assertEqual(len(merged), MAX_INGEST_IMAGES)

    def test_zip_images_go_to_batch_cap(self) -> None:
        zipped = [_src(f"zip:1:{n}", archive=True) for n in range(80)]
        merged = Ingest._cap_sources(zipped)
        self.assertEqual(len(merged), MAX_BATCH_IMAGES)


if __name__ == "__main__":
    unittest.main()
