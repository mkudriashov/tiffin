FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY config ./config
COPY data ./data
COPY claude-jobs ./claude-jobs
RUN uv sync --frozen --no-dev --no-editable
ENV TIFFIN_HOME=/data
VOLUME /data
CMD ["uv", "run", "--no-sync", "tiffin", "run"]
