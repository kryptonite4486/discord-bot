FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/data/weekly.db

WORKDIR /app

# Fonts for matplotlib charts (OCR runs on the external vision model)
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY bot/ ./bot/
COPY data/.gitkeep ./data/

RUN mkdir -p /app/data /app/tmp \
    && useradd --create-home --uid 1000 botuser \
    && chown -R botuser:botuser /app

USER botuser

VOLUME ["/app/data"]

CMD ["python", "-m", "bot.main"]