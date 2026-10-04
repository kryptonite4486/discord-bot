"""Image sources for OCR ingest, including safe extraction from .zip archives."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from PIL import Image, UnidentifiedImageError

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
ZIP_CONTENT_TYPES = frozenset(
    {"application/zip", "application/x-zip-compressed", "application/x-zip"}
)

# Defaults for archive extraction; callers may tighten these.
MAX_ZIP_IMAGES = 50
MAX_ZIP_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024


class ArchiveError(ValueError):
    """The archive is unreadable or exceeds a safety limit."""


@dataclass(frozen=True)
class ImageSource:
    """One image to OCR, independent of where it came from."""

    key: str  # stable identity used for de-duplication
    filename: str  # display name (zip members are "archive.zip/member.png")
    data: bytes
    from_archive: bool = False

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.filename).suffix.lower() or ".png"


def is_image_name(filename: str) -> bool:
    return PurePosixPath(filename).suffix.lower() in IMAGE_SUFFIXES


def is_zip_upload(filename: str, content_type: str | None) -> bool:
    if content_type and content_type.split(";")[0].strip() in ZIP_CONTENT_TYPES:
        return True
    return PurePosixPath(filename).suffix.lower() == ".zip"


def _skip_member(info: zipfile.ZipInfo) -> bool:
    if info.is_dir():
        return True
    parts = PurePosixPath(info.filename.replace("\\", "/")).parts
    if any(p == "__MACOSX" or p.startswith(".") for p in parts):
        return True
    return not is_image_name(info.filename)


def _is_valid_image(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return False
    return True


def extract_images_from_zip(
    data: bytes,
    *,
    archive_name: str,
    key_prefix: str,
    max_images: int = MAX_ZIP_IMAGES,
    max_member_bytes: int = MAX_ZIP_MEMBER_BYTES,
    max_total_bytes: int = MAX_ZIP_TOTAL_BYTES,
) -> tuple[list[ImageSource], list[str]]:
    """
    Read image members from a zip held in memory.

    Members are never written to disk by name, so path traversal entries are
    harmless. Non-image files, folders, dotfiles and ``__MACOSX`` metadata are
    skipped. Images are returned sorted by member path. Returns
    ``(images, warnings)``; raises ``ArchiveError`` for unreadable archives or
    when size limits are exceeded.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"`{archive_name}` is not a valid zip file") from exc

    warnings: list[str] = []
    with zf:
        candidates = sorted(
            (i for i in zf.infolist() if not _skip_member(i)),
            key=lambda i: i.filename.lower(),
        )
        skipped = sum(1 for i in zf.infolist() if not i.is_dir()) - len(candidates)
        if skipped:
            warnings.append(f"Skipped {skipped} non-image file(s) in `{archive_name}`.")
        if not candidates:
            return [], warnings
        if len(candidates) > max_images:
            warnings.append(
                f"`{archive_name}` has {len(candidates)} images; "
                f"only the first {max_images} will be processed."
            )
            candidates = candidates[:max_images]

        images: list[ImageSource] = []
        total = 0
        for info in candidates:
            if info.file_size > max_member_bytes:
                warnings.append(f"Skipped `{info.filename}` (too large).")
                continue
            if total + info.file_size > max_total_bytes:
                raise ArchiveError(
                    f"`{archive_name}` expands past the "
                    f"{max_total_bytes // (1024 * 1024)} MB limit"
                )
            try:
                with zf.open(info) as member:
                    # Read one byte past the cap so a lying header can't
                    # make us decompress unbounded data.
                    blob = member.read(max_member_bytes + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                # RuntimeError: encrypted member; NotImplementedError: codec.
                warnings.append(f"Skipped `{info.filename}` ({exc}).")
                continue
            if len(blob) > max_member_bytes:
                warnings.append(f"Skipped `{info.filename}` (too large).")
                continue
            total += len(blob)
            if not _is_valid_image(blob):
                warnings.append(f"Skipped `{info.filename}` (not a readable image).")
                continue
            images.append(
                ImageSource(
                    key=f"{key_prefix}:{info.filename}",
                    filename=f"{archive_name}/{info.filename}",
                    data=blob,
                    from_archive=True,
                )
            )
    return images, warnings
