# syntax=docker/dockerfile:1

# Step 36 uses the locally verified Node 26.8.1 toolchain. Vite 8 supports
# Node >=22.12; the application source and npm lockfile determine JS packages.
FROM node:26.8.1-bookworm-slim@sha256:367679cf9792759492a486e4aa4b421764d71a9546a6dae8aab81a99eb797b3e AS frontend
WORKDIR /web/app
COPY web/app/package.json web/app/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/app/ ./
RUN npm run build

# uv 0.12.6 is the repository's locally verified lockfile tool version.
FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS python-build
WORKDIR /app
RUN pip install --no-cache-dir "uv==0.12.6"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# P5-S51: LibreDWG (GPLv3) for DWG -> DXF conversion on upload.  Debian bookworm
# and trixie ship no libredwg package (checked), so it is built from the pinned
# GNU release, verified by sha256.  Only dwg2dxf, the shared library and the
# licence travel to the runtime stage; it is invoked as a separate process, never
# linked into Python (docs/design/p5-step-51-dwg-read.md D8).  The build takes
# ~7 min cold; the layer is cached as long as the version and checksum are unchanged.
FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS libredwg
ARG LIBREDWG_VERSION=0.14
ARG LIBREDWG_SHA256=cb6ee0b078c6d9e0f09d66f1feac33ba6342df88ae544e9f9335fab475218351
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL -o libredwg.tar.gz "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.gz" \
    && echo "${LIBREDWG_SHA256}  libredwg.tar.gz" | sha256sum -c - \
    && tar -xzf libredwg.tar.gz --strip-components=1 \
    && ./configure --prefix=/opt/libredwg --disable-bindings --disable-werror --disable-docs \
    && make -j"$(nproc)" \
    && make install-strip \
    && mkdir -p /opt/libredwg-runtime/bin /opt/libredwg-runtime/lib /opt/libredwg-runtime/share \
    && cp /opt/libredwg/bin/dwg2dxf /opt/libredwg-runtime/bin/ \
    && cp -P /opt/libredwg/lib/libredwg.so* /opt/libredwg-runtime/lib/ \
    && cp COPYING /opt/libredwg-runtime/share/COPYING \
    && echo "LibreDWG ${LIBREDWG_VERSION} https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.gz sha256=${LIBREDWG_SHA256}" > /opt/libredwg-runtime/share/SOURCE

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# The credential and trace directories must exist in the image with the right
# owner before Compose mounts a volume over them: Docker copies ownership and
# mode from the image only when it first populates an empty volume, so a path
# that is absent here becomes a root-owned mount that the wateragent user
# cannot write.
# Both are siblings of /var/lib/water-agent, not children — scripts/backup.sh
# archives that directory wholesale into user_files.tar and must never pick up
# credentials, nor the verbatim user questions and answers a trace contains.
# Traces live on a volume at all because /app/data is baked read-only below,
# which would make JsonTraceRecorder.finish() fail on every run.
RUN groupadd --gid 10001 wateragent \
    && useradd --uid 10001 --gid wateragent --create-home --shell /usr/sbin/nologin wateragent \
    && mkdir -p /var/lib/water-agent/uploads /var/lib/water-agent/exports \
    && mkdir -p /var/lib/water-agent-credentials /var/lib/water-agent-traces \
    && chown -R wateragent:wateragent /var/lib/water-agent /var/lib/water-agent-credentials /var/lib/water-agent-traces \
    && chmod 700 /var/lib/water-agent-credentials /var/lib/water-agent-traces
COPY --from=python-build /app/.venv /app/.venv
COPY --from=python-build /app/alembic.ini /app/alembic.ini
COPY --from=python-build /app/alembic /app/alembic
COPY --from=python-build /app/src /app/src
COPY --from=frontend /web/app/dist /app/web/app/dist
COPY --from=libredwg /opt/libredwg-runtime /opt/libredwg
# P6-S1: dxf2dwg for the experimental, opt-in DWG output of parametric drawing
# generation.  Every DWG it writes is read back with dwg2dxf and discarded on any
# mismatch.  Copied straight from the libredwg stage's install prefix so that stage
# (the ~7 min source build) stays cached.
COPY --from=libredwg /opt/libredwg/bin/dxf2dwg /opt/libredwg/bin/dxf2dwg
ENV WATER_AGENT_DWG2DXF=/opt/libredwg/bin/dwg2dxf \
    LD_LIBRARY_PATH=/opt/libredwg/lib
RUN /opt/libredwg/bin/dwg2dxf --version && /opt/libredwg/bin/dxf2dwg --version
# Retrieval data: fixtures + curated corpus + version registry + the prebuilt
# TriviumDB index.  The index is a build artifact produced beforehand by
# scripts/build_index.py -- it is deliberately NOT built here, because that needs
# DASHSCOPE_API_KEY and a build secret would make "which corpus is in this image"
# unauditable.  Copied read-only (0444 files / 0555 dirs, root-owned): the
# retriever opens it with access_mode="read_only", so the wateragent user never
# needs to write here, and the mode makes that guarantee explicit rather than
# incidental.  Scope is set by .dockerignore, which re-includes only these paths.
COPY --chown=root:root data /app/data
RUN chmod -R a-w /app/data
USER wateragent
EXPOSE 8000
CMD ["uvicorn", "water_agent.webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
