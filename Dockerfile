# Бот в контейнере на Python 3.13 (вместо Python 3.8 из пакета py3k на DSM 6.2).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# ca-certificates нужны requests/curl_cffi, tzdata — чтобы работал TZ.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY bot.py config.py transmission_client.py ./
COPY trackers/ ./trackers/
COPY utils/ ./utils/

# Cookies, логи и прочее состояние — в /data (volume).
RUN mkdir -p /data

CMD ["python", "-u", "bot.py"]
