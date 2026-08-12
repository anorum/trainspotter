# Detector service: consumes frame metadata, scores frames, publishes raw
# detections.
#
# Carries the vision extras (numpy, Pillow) but deliberately not torch. YOLO is
# available as a detector implementation, but an 8GB ARM node is the wrong place
# for a 2GB image, and reference differencing does the same job here with numpy.
FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY libs/blockade-core/pyproject.toml libs/blockade-core/
COPY services/detector/pyproject.toml services/detector/
RUN uv sync --frozen --no-dev --package blockade-detector --no-install-workspace

COPY libs/blockade-core/ libs/blockade-core/
COPY services/detector/ services/detector/
RUN uv sync --frozen --no-dev --package blockade-detector


FROM python:3.11-slim

# numpy's OpenMP runtime on arm64. Missing it fails at import time rather than
# at build time, which is a much worse place to discover it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 blockade

WORKDIR /app
COPY --from=build --chown=blockade:blockade /app /app
COPY --chown=blockade:blockade config/ /app/config/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BLOCKADE_REFERENCES_DIR=/var/lib/blockade/references \
    BLOCKADE_CAMERA_CONFIG_PATH=/app/config/cameras.yaml \
    BLOCKADE_DETECTOR=reference

RUN mkdir -p /var/lib/blockade && chown -R blockade:blockade /var/lib/blockade

USER blockade
EXPOSE 9102
ENTRYPOINT ["blockade-detect"]
CMD ["run"]
