# syntax=docker/dockerfile:1
# Every base image and the Claude CLI are pinned to an exact version; the digests were read from
# the registries on the day they were chosen. Update the tag and the digest together.

FROM node:22.23.4-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS claude
# The package ships a native binary; npm only fetches the right one for this platform.
RUN npm install -g --no-audit --no-fund @anthropic-ai/claude-code@2.1.316 \
    && cp "$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe" /claude \
    && /claude --version

FROM python:3.14.10-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527 AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY config ./config
COPY data/cofid ./data/cofid
COPY claude-jobs ./claude-jobs
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14.10-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527 AS run
# Runs as a non-root user. Change the number here, or set `user:` in compose; everything the
# process writes is under /data, and the code is world-readable.
ARG TIFFIN_UID=10010
# Stamped by the build so /tiffin/api/version can say what is running (release id, source commit).
ARG RELEASE_ID=unknown
ARG RELEASE_COMMIT=unknown
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIFFIN_HOME=/data \
    TIFFIN_CONTAINER=docker \
    TIFFIN_RELEASE=${RELEASE_ID} \
    TIFFIN_COMMIT=${RELEASE_COMMIT} \
    HOME=/data/home \
    TMPDIR=/data/tmp \
    XDG_CACHE_HOME=/data/home/.cache
RUN groupadd --gid "${TIFFIN_UID}" tiffin \
    && useradd --uid "${TIFFIN_UID}" --gid "${TIFFIN_UID}" --home-dir /data/home \
        --no-create-home --shell /usr/sbin/nologin tiffin \
    && mkdir -p /data/home /data/tmp \
    && chown -R "${TIFFIN_UID}:${TIFFIN_UID}" /data
COPY --from=claude /claude /usr/local/bin/claude
COPY --from=build /opt/venv /opt/venv
RUN chmod -R a+rX /opt/venv && claude --version && tiffin version
USER ${TIFFIN_UID}:${TIFFIN_UID}
VOLUME /data
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "tiffin.healthcheck"]
CMD ["tiffin", "run"]
