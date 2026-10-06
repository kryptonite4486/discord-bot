"""Time the vision OCR on the repo's sample screenshots (plan §2 item 14).

Sends each sample image to the configured OCR server (OCR_VISION_* from
.env, the same model and prompts as the bot) ``--runs`` times and prints
seconds per image: mean, p50 and p95, overall and per sample. Each timing
covers one ``extract_metrics_from_image`` call, the same entry point the
bot's ingest uses, so retries count as they would in production.

It never opens the bot's database; feed the mean to
``/ops capacity seconds_per_image:<mean>`` to see what it means for quotas.

    .venv/bin/python scripts/benchmark_ocr.py --runs 5
    .venv/bin/python scripts/benchmark_ocr.py --runs 5 --concurrency 2

With ``--concurrency`` above 1, requests overlap like OCR_MAX_CONCURRENCY
slots would. Seconds per image is then slot time, and the throughput line
shows whether the extra slot really adds capacity on this machine.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_vision_env  # noqa: E402  (also loads .env)
from bot.ocr.pipeline import VisionOCR, extract_metrics_from_image  # noqa: E402

# Sample screenshot -> dataset kind (the caller always names the kind).
SAMPLES: tuple[tuple[str, str], ...] = (
    ("samples/power_leaderboard.png", "power"),
    ("samples/versus_leaderboard.png", "versus"),
    ("samples/kills_leaderboard.webp", "kills"),
    ("samples/general_members.png", "general"),
    ("samples/general_profile.png", "general"),
    ("samples/arena_members.png", "arena"),
    ("tests/e2e/general_profile_small.jpg", "general"),
)


def local_base_url(configured: str) -> str:
    """host.docker.internal only resolves inside Docker; use loopback outside it."""
    parts = urlsplit(configured)
    if parts.hostname == "host.docker.internal" and not Path("/.dockerenv").exists():
        return urlunsplit(
            parts._replace(netloc=parts.netloc.replace("host.docker.internal", "127.0.0.1"))
        )
    return configured


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..100) of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil
    return ordered[int(rank) - 1]


def summary(values: list[float]) -> str:
    return (
        f"mean {statistics.fmean(values):6.2f}s  "
        f"p50 {percentile(values, 50):6.2f}s  "
        f"p95 {percentile(values, 95):6.2f}s  (n={len(values)})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=3, help="times to send each image (default 3)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="requests in flight at once, like OCR_MAX_CONCURRENCY (default 1)",
    )
    parser.add_argument(
        "--warmup", type=int, default=1, help="untimed requests first, to load the model (default 1)"
    )
    parser.add_argument("--base-url", help="override OCR_VISION_BASE_URL")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.concurrency < 1 or args.warmup < 0:
        parser.error("--runs and --concurrency must be at least 1, --warmup at least 0")

    base, model, api_key, timeout = load_vision_env()
    ocr = VisionOCR(
        base_url=args.base_url or os.getenv("OCR_VISION_TEST_BASE_URL") or local_base_url(base),
        model=model,
        api_key=api_key,
        timeout=timeout,
    )
    samples = [(ROOT / rel, kind) for rel, kind in SAMPLES if (ROOT / rel).is_file()]
    if not samples:
        print("No sample images found.", file=sys.stderr)
        return 1
    print(f"OCR server {ocr.base_url}, model {ocr.model}")
    print(
        f"{len(samples)} image(s) x {args.runs} run(s), concurrency {args.concurrency}, "
        f"{args.warmup} warm-up request(s)\n"
    )

    def timed(job: tuple[Path, str]) -> tuple[Path, float, str | None]:
        path, kind = job
        start = time.perf_counter()
        try:
            result = extract_metrics_from_image(path, kind=kind, ocr=ocr)  # type: ignore[arg-type]
            error = None if result.metrics else "no players found"
        except Exception as exc:  # noqa: BLE001  (report every failure, keep going)
            error = f"{type(exc).__name__}: {exc}"
        return path, time.perf_counter() - start, error

    for i in range(args.warmup):
        _, secs, error = timed(samples[i % len(samples)])
        print(f"warm-up {i + 1}: {secs:.2f}s" + (f" ({error})" if error else ""))

    jobs = [s for _ in range(args.runs) for s in samples]
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(timed, jobs))
    wall = time.perf_counter() - wall_start

    by_image: dict[Path, list[float]] = {}
    failures: list[tuple[Path, str]] = []
    for path, secs, error in results:
        by_image.setdefault(path, []).append(secs)
        if error:
            failures.append((path, error))

    print()
    width = max(len(p.name) for p in by_image)
    for path, times in by_image.items():
        print(f"{path.name:<{width}}  {summary(times)}")
    all_times = [secs for _, secs, _ in results]
    print(f"\n{'All images':<{width}}  {summary(all_times)}")
    print(
        f"Throughput: {len(results) / wall * 3600:,.0f} images/hour "
        f"({wall / len(results):.2f}s wall per image at concurrency {args.concurrency})"
    )
    print(f"Failed: {len(failures)} of {len(results)}")
    for path, error in failures:
        print(f"  {path.name}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
