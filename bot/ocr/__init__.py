"""OCR package."""

from bot.ocr.pipeline import OCREngine, OCRResult, ExtractedMetric, extract_metrics_from_image

__all__ = [
    "OCREngine",
    "OCRResult",
    "ExtractedMetric",
    "extract_metrics_from_image",
    "extract_metrics_via_vision",
]


def __getattr__(name: str):
    if name == "extract_metrics_via_vision":
        from bot.ocr.vision import extract_metrics_via_vision

        return extract_metrics_via_vision
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
