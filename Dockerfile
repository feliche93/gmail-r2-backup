FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install dependencies into /app/.venv using the locked resolver.
COPY pyproject.toml uv.lock README.md /app/
COPY gmail_r2_backup /app/gmail_r2_backup

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

ENTRYPOINT ["gmail-r2-backup"]
CMD ["--help"]

