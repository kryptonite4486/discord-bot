"""OCR capacity estimate from the usage ledger (plan §2 item 14).

Pure arithmetic, no database or Discord: ``/ops capacity`` reads UsageLedger
and passes the rows in.

How the estimate works:

- ``ocr_seconds`` is the time a batch held an OCR slot, so OCR seconds per
  image is slot time per image (retries and failed images included).
- One slot is busy at most 86,400 seconds a day. The fair queue runs
  ``OCR_MAX_CONCURRENCY`` slots, so the bot's daily capacity is
  ``slots * 86,400 / seconds_per_image`` images. Only part of a day is
  usable in practice (uploads bunch up after the reset, and the Mac does
  other work), so the estimate is scaled by a target utilization.
- Demand is not spread evenly over the week. The peak-day share is the
  busiest weekday's share of an average week. A server that uses its whole
  weekly quota is assumed to follow the same pattern, so on the peak day it
  sends ``quota * peak_share`` images. The number of such servers that fit
  is the usable peak-day capacity divided by that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

SECONDS_PER_DAY = 86_400
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Below these, the measured seconds per image and weekly pattern are too
# noisy to plan quotas on: one slow image or one busy day dominates.
MIN_IMAGES_FOR_ESTIMATE = 100
MIN_ACTIVE_DAYS_FOR_ESTIMATE = 7


@dataclass(frozen=True)
class UsageStats:
    """What the ledger says about a window of UTC days."""

    days: int
    images: float
    batches: float
    failed: float
    ocr_seconds: float
    wait_seconds: float
    # Average images per weekday (Mon..Sun) over the window's calendar days.
    weekday_avg: tuple[float, ...]
    # (day, images, average wait per batch that day), busiest first.
    busiest_days: list[tuple[str, float, float | None]]
    # Worst daily average wait: (day, seconds), or None with no batches.
    peak_wait: tuple[str, float] | None
    # Days in the window with at least one image.
    active_days: int = 0

    @property
    def seconds_per_image(self) -> float | None:
        return self.ocr_seconds / self.images if self.images else None

    @property
    def failure_rate(self) -> float | None:
        return self.failed / self.images if self.images else None

    @property
    def avg_wait(self) -> float | None:
        return self.wait_seconds / self.batches if self.batches else None

    @property
    def peak_day_share(self) -> float | None:
        """Busiest weekday's share of an average week, or None with no images."""
        week = sum(self.weekday_avg)
        return max(self.weekday_avg) / week if week else None

    @property
    def peak_weekday(self) -> str | None:
        if not sum(self.weekday_avg):
            return None
        return WEEKDAYS[self.weekday_avg.index(max(self.weekday_avg))]


def summarize_usage(
    daily: dict[str, dict[str, float]],
    first_day: date,
    last_day: date,
    *,
    top_days: int = 5,
) -> UsageStats:
    """Totals, weekday pattern, busiest days and waits for ``first_day..last_day``.

    ``daily`` maps an ISO day to its totals per usage kind, summed across
    servers. Days with no rows count as zero, so a quiet Monday still pulls
    the Monday average down.
    """
    if last_day < first_day:
        raise ValueError("last_day is before first_day")
    n_days = (last_day - first_day).days + 1
    weekday_sum = [0.0] * 7
    weekday_count = [0] * 7
    for i in range(n_days):
        d = first_day + timedelta(days=i)
        weekday_count[d.weekday()] += 1
        weekday_sum[d.weekday()] += daily.get(d.isoformat(), {}).get("ocr_images", 0.0)
    weekday_avg = tuple(
        s / c if c else 0.0 for s, c in zip(weekday_sum, weekday_count)
    )

    def total(kind: str) -> float:
        return sum(u.get(kind, 0.0) for u in daily.values())

    def day_wait(u: dict[str, float]) -> float | None:
        batches = u.get("ocr_batches", 0.0)
        return u.get("ocr_wait_seconds", 0.0) / batches if batches else None

    busiest = sorted(
        ((d, u.get("ocr_images", 0.0), day_wait(u)) for d, u in daily.items()),
        key=lambda row: (-row[1], row[0]),
    )
    busiest = [row for row in busiest if row[1] > 0][:top_days]
    waits = [(d, w) for d, u in daily.items() if (w := day_wait(u)) is not None]
    peak_wait = max(waits, key=lambda dw: (dw[1], dw[0])) if waits else None

    return UsageStats(
        days=n_days,
        images=total("ocr_images"),
        batches=total("ocr_batches"),
        failed=total("ocr_failed"),
        ocr_seconds=total("ocr_seconds"),
        wait_seconds=total("ocr_wait_seconds"),
        weekday_avg=weekday_avg,
        busiest_days=busiest,
        peak_wait=peak_wait,
        active_days=sum(1 for u in daily.values() if u.get("ocr_images", 0.0) > 0),
    )


def sample_warning(stats: UsageStats, *, seconds_measured: bool, share_measured: bool) -> str | None:
    """Why a measured estimate can't be trusted yet, or None if the sample is big enough.

    Only measured inputs count: a given seconds_per_image or an assumed peak
    share doesn't depend on how much usage has been recorded.
    """
    if not (seconds_measured or share_measured):
        return None
    if stats.images >= MIN_IMAGES_FOR_ESTIMATE and stats.active_days >= MIN_ACTIVE_DAYS_FOR_ESTIMATE:
        return None
    return (
        f"Too little data to plan on: {int(stats.images)} image(s) on "
        f"{stats.active_days} day(s). The measured figures need at least "
        f"{MIN_IMAGES_FOR_ESTIMATE} images over {MIN_ACTIVE_DAYS_FOR_ESTIMATE}+ days of real use; "
        "until then treat the numbers below as illustrative."
    )


def format_weekday_avg(n: float) -> str:
    """An average image count, with enough decimals that small values aren't shown as 0."""
    if n == 0:
        return "0"
    if n >= 10:
        return f"{n:.0f}"
    if n >= 1:
        return f"{n:.1f}"
    return f"{n:.2f}"


@dataclass(frozen=True)
class CapacityEstimate:
    seconds_per_image: float
    slots: int
    peak_day_share: float
    utilization: float
    # Images per week one slot can process if it never stops.
    images_per_slot_week: float
    # Images the whole bot can process on one day at the target utilization.
    usable_images_per_day: float
    # Weekly volume that fits when the peak day is the limit.
    usable_images_per_week: float
    # Tier name -> (weekly quota, servers at that quota that fit).
    servers_by_tier: dict[str, tuple[int, int]]


def estimate_capacity(
    *,
    seconds_per_image: float,
    slots: int,
    peak_day_share: float,
    quotas: dict[str, int],
    utilization: float = 0.7,
) -> CapacityEstimate:
    """How many images, and how many servers at each quota, the OCR slots can take.

    ``peak_day_share`` is the busiest weekday's fraction of a week's images
    (1/7 for perfectly even demand, 1.0 if everything lands on one day).
    ``utilization`` is the fraction of a day the slots can usefully be busy.
    """
    if seconds_per_image <= 0:
        raise ValueError("seconds_per_image must be positive")
    if slots < 1:
        raise ValueError("slots must be at least 1")
    if not 0 < peak_day_share <= 1:
        raise ValueError("peak_day_share must be in (0, 1]")
    if not 0 < utilization <= 1:
        raise ValueError("utilization must be in (0, 1]")

    per_slot_day = SECONDS_PER_DAY / seconds_per_image
    usable_day = per_slot_day * slots * utilization
    servers = {
        name: (quota, int(usable_day // (quota * peak_day_share)) if quota > 0 else 0)
        for name, quota in quotas.items()
    }
    return CapacityEstimate(
        seconds_per_image=seconds_per_image,
        slots=slots,
        peak_day_share=peak_day_share,
        utilization=utilization,
        images_per_slot_week=per_slot_day * 7,
        usable_images_per_day=usable_day,
        usable_images_per_week=usable_day / peak_day_share,
        servers_by_tier=servers,
    )
