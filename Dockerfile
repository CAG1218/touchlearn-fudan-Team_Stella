FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOUCHLEARN_OUTPUTS=/app/outputs \
    TOUCHLEARN_CONTAINER=1

WORKDIR /app

# 大陆服务器构建慢时可传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" ".[docs-reader]" \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/outputs \
    && chown -R app:app /app

USER app

EXPOSE 8765

# Render/Railway/Fly 会注入 PORT；本地默认 8765。
CMD ["sh", "-c", "python -m touchlearn.agent.server --host 0.0.0.0 --port ${PORT:-8765} --no-browser"]
