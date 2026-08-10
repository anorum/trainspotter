# Poller service: scrapes ODOT camera stills to object storage and publishes
# frame metadata.
#
# Built for linux/arm64 - the k3s nodes are Raspberry Pi 5s.
#
# Deliberately excludes the vision extras. The poller must be the most boring,
# most independent process in the system: ODOT overwrites each image within the
# minute, so capture downtime is data nobody can recover, and every dependency
# it does not have is one that cannot break it.
FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY libs/blockade-core/pyproject.toml libs/blockade-core/
COPY services/poller/pyproject.toml services/poller/
RUN uv sync --frozen --no-dev --package blockade-poller --no-install-workspace

COPY libs/blockade-core/ libs/blockade-core/
COPY services/poller/ services/poller/
RUN uv sync --frozen --no-dev --package blockade-poller


FROM python:3.11-slim

RUN useradd --create-home --uid 10001 blockade
WORKDIR /app

COPY --from=build --chown=blockade:blockade /app /app
COPY --chown=blockade:blockade config/ /app/config/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BLOCKADE_LOCAL_CACHE_DIR=/var/lib/blockade/frames \
    BLOCKADE_MANIFEST_DIR=/var/lib/blockade/manifests \
    BLOCKADE_CAMERA_CONFIG_PATH=/app/config/cameras.yaml

# The manifest is the outbox and the backfill corpus, so this volume must
# outlive the pod.
RUN mkdir -p /var/lib/blockade && chown -R blockade:blockade /var/lib/blockade
VOLUME ["/var/lib/blockade"]

USER blockade
EXPOSE 9102
ENTRYPOINT ["blockade-capture"]
CMD ["run"]
