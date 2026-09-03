FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV UV_PROJECT_ENVIRONMENT=/usr/local
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# scanner/screenshot.py needs an actual Chromium binary - --with-deps also
# installs the OS-level libs (fonts, etc) python:3.12-slim doesn't have,
# without which even a present binary fails to launch headless.
RUN playwright install --with-deps chromium

CMD ["python", "-m", "temporal.worker"]
