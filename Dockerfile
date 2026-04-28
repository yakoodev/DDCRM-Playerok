FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY ddcrm_playerok_worker ./ddcrm_playerok_worker
COPY vendor/playerok-universal ./vendor/playerok-universal
COPY entrypoint.sh ./entrypoint.sh

RUN pip install --no-cache-dir .
RUN chmod +x ./entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
