FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow \
    DB_PATH=/app/data/radar.sqlite \
    REGISTRY_SEED_PATH=/app/seed/registry_seed.sqlite

WORKDIR /app

COPY pyproject.toml README.md ./
COPY radar ./radar
# Заготовка реестра лежит вне тома с данными, чтобы обновлённый образ приносил новый реестр.
COPY data/registry_seed.sqlite ./seed/registry_seed.sqlite

RUN pip install --upgrade pip && pip install . \
    && useradd -r -u 10001 -d /app radar \
    && mkdir -p /app/data && chown -R radar:radar /app/data /app/seed

USER radar
VOLUME ["/app/data"]

CMD ["radar", "run"]
