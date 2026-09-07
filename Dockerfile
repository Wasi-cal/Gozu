# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV UV_PROJECT_ENVIRONMENT=/usr/local
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/

# scanner/screenshot.py's render_finding_snippet() needs Pygments'
# ImageFormatter to find an actual monospace font - confirmed live that
# python:3.12-slim has neither fontconfig (`fc-list`) nor any font at
# all, which makes ImageFormatter raise FileNotFoundError outright, not
# just render ugly. fonts-dejavu-core is the specific package that
# provides DejaVu Sans Mono, the font ImageFormatter looks for first;
# both packages together add a few MB, nowhere near what the Chromium
# install this replaced used to cost.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-group dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-group dev

CMD ["python", "-m", "temporal.worker"]
