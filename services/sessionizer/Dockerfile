# Sessionizer service: turns the observation stream into blockage sessions
# and alerts.
#
# Built for linux/arm64 - the k3s nodes are Raspberry Pi 5s.
#
# Kafka client and pydantic only: no vision, no S3, no volumes. State is a
# pure function of the observations topic and rebuilds by replay on boot, so
# this is the most stateless container in the system.
FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY libs/blockade-core/pyproject.toml libs/blockade-core/
COPY services/sessionizer/pyproject.toml services/sessionizer/
RUN uv sync --frozen --no-dev --package blockade-sessionizer --no-install-workspace

COPY libs/blockade-core/ libs/blockade-core/
COPY services/sessionizer/ services/sessionizer/
RUN uv sync --frozen --no-dev --package blockade-sessionizer


FROM python:3.11-slim

RUN useradd --create-home --uid 10001 blockade
WORKDIR /app

COPY --from=build --chown=blockade:blockade /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER blockade
EXPOSE 9102
ENTRYPOINT ["blockade-sessionizer"]
CMD ["run"]
