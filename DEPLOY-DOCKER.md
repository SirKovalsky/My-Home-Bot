# Бот в Docker на NAS (Python 3.13)

Короткая инструкция: запуск бота контейнером на MySRV вместо venv с Python 3.8.
Смысл — DSM 6.2 даёт Python 3.8.6, из-за чего приходится пинить старые aiogram/aiohttp/urllib3.
В контейнере Python 3.13 и актуальные пакеты; код бота не меняется.

## Что уже лежит в репозитории

- `Dockerfile` — образ на `python:3.13-slim`, ставит `requirements.txt` (все колёса готовые, компилятор не нужен).
- `.dockerignore` — `.env`, cookies, логи и `.venv` в образ не попадают.
- `requirements.txt` — современный (не `requirements-dsm6.txt`).

## Требования

- Docker на NAS установлен и запущен:
  ```sh
  sudo /usr/local/bin/docker info
  ```
  Если пакет остановлен: `sudo /usr/syno/bin/synopkg start Docker`.
- `docker` — только через `sudo` (сокет `root:root 660`).
- FlareSolverr слушает `127.0.0.1:8191`, Transmission — `127.0.0.1:9091` (не меняем).

## 1. Остановить старый бот

Иначе два процесса одновременно делают getUpdates — Telegram отдаст `409 Conflict`,
а cookies и логи будут перетираться.

```sh
sudo /usr/local/etc/rc.d/S99torrent-bot.sh stop
sudo /usr/local/etc/rc.d/S99torrent-bot.sh status
```

Если контейнер заменяет старый автозапуск:

```sh
sudo rm /usr/local/etc/rc.d/S99torrent-bot.sh   # run-forever.sh остаётся для отката
```

## 2. Собрать образ

```sh
cd /volume1/My-Home-Bot
sudo docker build -t torrent-bot:py313 .
```

Первый раз качается `python:3.13-slim` (~50 МБ) и колёса — 2–4 минуты.

## 3. Каталог состояния

```sh
sudo mkdir -p /volume1/docker/torrent-bot
```

## 4. Запустить

```sh
sudo docker run -d --name torrent-bot --restart unless-stopped \
  --network host \
  -e TZ=Europe/Moscow \
  -e COOKIES_FILE=/data/cookies.pickle \
  -e LOG_FILE=/data/torrent-bot.log \
  -v /volume1/My-Home-Bot/.env:/app/.env:ro \
  -v /volume1/docker/torrent-bot:/data \
  torrent-bot:py313
```

Почему именно так:

- **`--network host`** обязателен: Transmission (`127.0.0.1:9091`) и FlareSolverr (`127.0.0.1:8191`)
  слушают localhost NAS, а в bridge-сети `127.0.0.1` — это сам контейнер.
  Архитектура при этом не меняется: трекеры — только через явный `proxies=...`,
  Transmission и FlareSolverr — без прокси.
- **`/app/.env:ro`** — конфиг монтируется, а не запекается в образ (секретов в образе нет).
- **`/data`** — cookies и лог переживают пересборку: иначе после каждого `docker build`
  заново проходить Cloudflare и `/login_rutracker`, `/login_kinozal`.
- `-e COOKIES_FILE/LOG_FILE` важнее значений из `.env`: `python-dotenv` не перезаписывает
  уже заданные переменные окружения.

## 5. Проверить

```sh
sudo docker ps --filter name=torrent-bot
sudo docker logs --tail 40 torrent-bot
tail -f /volume1/docker/torrent-bot/torrent-bot.log
```

В логе должно быть:

```
Transmission доступен (без прокси).
Бот запускается. Прокси трекеров: direct
```

Дальше — `/start` в Telegram.

## Обновление

```sh
cd /volume1/My-Home-Bot
sudo git pull
sudo docker build -t torrent-bot:py313 .
sudo docker rm -f torrent-bot
# и снова docker run из шага 4
```

## Откат на venv (Python 3.8)

```sh
sudo docker rm -f torrent-bot
sudo /usr/local/etc/rc.d/S99torrent-bot.sh start
```

Если rc.d-скрипт удалял — `cd /volume1/My-Home-Bot && sudo sh deploy/install.sh` его восстановит.

## Если удобнее compose

`docker-compose.yml` в корне репозитория:

```yaml
version: "2.4"
services:
  torrent-bot:
    build: .
    image: torrent-bot:py313
    container_name: torrent-bot
    restart: unless-stopped
    network_mode: host
    environment:
      TZ: Europe/Moscow
      COOKIES_FILE: /data/cookies.pickle
      LOG_FILE: /data/torrent-bot.log
    volumes:
      - ./.env:/app/.env:ro
      - /volume1/docker/torrent-bot:/data
```

```sh
sudo docker-compose up -d --build
```

## Грабли

- Два бота одновременно → в логах `Conflict: terminated by other getUpdates request`.
- `docker: command not found` — в PATH нет `/usr/local/bin`; используйте `sudo` или полный путь.
- `Cannot connect to the Docker daemon` — пакет остановлен: `sudo /usr/syno/bin/synopkg start Docker`.
- Медленно тянется `python:3.13-slim` — зеркало Docker Hub прописывается в
  `/usr/syno/etc/packages/Docker/dockerd.json` (`registry-mirrors`), затем перезапуск пакета.
- `docker logs` и файл лога дублируются — так и задумано: stdout в контейнер, файл в `/data`.
- Забыли смонтировать `.env` — бот упадёт на пустом `TELEGRAM_BOT_TOKEN`.
