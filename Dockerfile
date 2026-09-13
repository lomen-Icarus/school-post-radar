FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

COPY pyproject.toml README.md ./
COPY radar ./radar
COPY data/registry_seed.sqlite ./data/registry_seed.sqlite

RUN pip install --upgrade pip && pip install .

# Рабочая база хранится в томе /app/data (см. docker-compose.yml)
VOLUME ["/app/data"]

CMD ["radar", "run"]
