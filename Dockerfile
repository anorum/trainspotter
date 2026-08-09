# Capture service image. Python 3.11 to match the PyFlink constraint in Phase 2,
# so the Phase 0 corpus and the streaming jobs share one interpreter.
FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependency layer first: source edits don't invalidate the dependency install.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN uv sync --frozen --no-dev


FROM python:3.11-slim

RUN useradd --create-home --uid 10001 blockade
WORKDIR /app

COPY --from=build --chown=blockade:blockade /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BLOCKADE_LOCAL_CACHE_DIR=/var/lib/blockade/frames \
    BLOCKADE_MANIFEST_DIR=/var/lib/blockade/manifests

# Frames and manifests live on a PVC mounted here. The manifest is the backfill
# corpus for the entire pipeline, so this volume must outlive the pod.
RUN mkdir -p /var/lib/blockade && chown -R blockade:blockade /var/lib/blockade
VOLUME ["/var/lib/blockade"]

USER blockade
EXPOSE 9102
ENTRYPOINT ["blockade-capture"]
CMD ["run"]
