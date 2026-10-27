FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOUCHLEARN_OUTPUTS=/app/outputs \
    TOUCHLEARN_CONTAINER=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir ".[docs-reader]" \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/outputs \
    && chown -R app:app /app

USER app

EXPOSE 8765

# Render/Railway/Fly 会注入 PORT；本地默认 8765。
CMD ["sh", "-c", "python -m touchlearn.agent.server --host 0.0.0.0 --port ${PORT:-8765} --no-browser"]
