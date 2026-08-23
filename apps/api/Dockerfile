FROM python:3.12-slim

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN python -m pip install --index-url "$PIP_INDEX_URL" .

EXPOSE 8000

CMD ["uvicorn", "hzcu_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
