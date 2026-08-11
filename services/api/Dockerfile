# Serving layer: Astro static build + FastAPI, one container.
#
# The web stage runs on the BUILD platform, not the target: static output is
# arch-independent, and a node build under QEMU would add minutes for nothing.
FROM --platform=$BUILDPLATFORM node:22-slim AS web
WORKDIR /web
COPY services/api/web/package.json services/api/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY services/api/web/ ./
RUN npm run build


FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY libs/blockade-core/pyproject.toml libs/blockade-core/
COPY services/api/pyproject.toml services/api/
RUN uv sync --frozen --no-dev --package blockade-api --no-install-workspace

COPY libs/blockade-core/ libs/blockade-core/
COPY services/api/ services/api/
RUN uv sync --frozen --no-dev --package blockade-api


FROM python:3.11-slim

RUN useradd --create-home --uid 10001 blockade

WORKDIR /app
COPY --from=build --chown=blockade:blockade /app /app
COPY --from=web --chown=blockade:blockade /web/dist /app/static
COPY --chown=blockade:blockade config/ /app/config/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BLOCKADE_CAMERA_CONFIG_PATH=/app/config/cameras.yaml

USER blockade
EXPOSE 8000
ENTRYPOINT ["blockade-api"]
CMD ["run"]
