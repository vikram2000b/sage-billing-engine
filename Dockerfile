# Use Python 3.12 slim image
FROM python:3.12-slim AS python-base

ENV POETRY_VERSION=2.1.1 \
    POETRY_VENV="/opt/poetry-venv" \
    POETRY_CACHE_DIR="/opt/.cache"

FROM python-base AS builder-base

ENV PATH="$POETRY_VENV/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential

RUN python3 -m venv $POETRY_VENV && \
    $POETRY_VENV/bin/pip install "poetry==$POETRY_VERSION"

WORKDIR /app
COPY poetry.lock pyproject.toml ./

RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --no-root --only main

FROM python-base AS runtime

COPY --from=builder-base /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

WORKDIR /app
COPY . .

RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 50061

CMD ["python", "-m", "app.main"]
