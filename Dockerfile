# syntax=docker/dockerfile:1.7
FROM docker.io/library/python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tesseract is the local OCR fallback if OCR_API_KEY isn't set; libjpeg/zlib
# are needed by Pillow for runtime image decoding.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libjpeg62-turbo \
        zlib1g \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Run as non-root.
RUN useradd --create-home --uid 10001 bot && chown -R bot:bot /app
USER bot

CMD ["python", "-u", "bot.py"]
