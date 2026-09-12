# syntax=docker/dockerfile:1

# Step 36 uses the locally verified Node 26.8.1 toolchain. Vite 8 supports
# Node >=22.12; the application source and npm lockfile determine JS packages.
FROM node:26.8.1-bookworm-slim AS frontend
WORKDIR /web/app
COPY web/app/package.json web/app/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/app/ ./
RUN npm run build

# uv 0.12.6 is the repository's locally verified lockfile tool version.
FROM python:3.12.14-slim-bookworm AS python-build
WORKDIR /app
RUN pip install --no-cache-dir "uv==0.12.6"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

FROM python:3.12.14-slim-bookworm AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 wateragent \
    && useradd --uid 10001 --gid wateragent --create-home --shell /usr/sbin/nologin wateragent \
    && mkdir -p /var/lib/water-agent/uploads /var/lib/water-agent/exports \
    && chown -R wateragent:wateragent /var/lib/water-agent
COPY --from=python-build /app/.venv /app/.venv
COPY --from=python-build /app/src /app/src
COPY --from=frontend /web/app/dist /app/web/app/dist
USER wateragent
EXPOSE 8000
CMD ["uvicorn", "water_agent.webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
