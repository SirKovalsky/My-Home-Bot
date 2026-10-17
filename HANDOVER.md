# torrent-bot — сводка для передачи в другую сессию

Telegram-бот (aiogram v3), который принимает magnet / `.torrent` / ссылку на раздачу
rutracker.org и kinozal.me, сам ищет по трекерам, определяет категорию и складывает
торрент в Transmission на Synology в нужную папку.

Репозиторий: `git@github.com:SirKovalsky/My-Home-Bot.git`, ветка `main`
(локальная рабочая копия на Windows: `E:\MySoftware\My_Home_Bot` — это и есть корень репо).

---

## 1. Главное архитектурное правило (нарушать нельзя)

- Трафик трекеров идёт **только** через явный аргумент `proxies={...}` — `trackers/base.py`,
  помечено `>>> ЗДЕСЬ ЗАДАЁТСЯ ПРОКСИ <<<`; плюс `trust_env=False` (никакого системного прокси).
- Transmission RPC (`transmission_client.py`) и локальный решатель Cloudflare
  (`trackers/solver.py`) — **явно без прокси**: `proxies={}`, `trust_env=False`
  (`>>> ЯВНО БЕЗ ПРОКСИ <<<`).
- Запрещено: iptables/nftables на OpenWrt или Xpenology; заворачивать Transmission/P2P
  в прокси; reverse-proxy/туннели между NAS и шлюзом; системный прокси; глобальные
  `HTTP_PROXY`/`HTTPS_PROXY`. Единственная точка проксирования — сессии трекеров.
- `cf_clearance` от Cloudflare привязан к паре «внешний IP + User-Agent». Поэтому
  FlareSolverr и бот должны выходить с одного внешнего IP, а UA солвера закрепляется
  в сессии трекера поверх `impersonate` (иначе 403).

## 2. Где живёт (прод)

- **MySRV**: Xpenology DS918+ (Apollo Lake, 4 ядра, 12 ГБ), **DSM 6.2.3-25426 Update 3**,
  IPv4 **192.168.1.51**, проект **`/volume1/My-Home-Bot`** (файлы root-owned).
- venv `.venv` — Python **3.8.6** (пакет py3k), OpenSSL 1.0.2u ⇒ зависимость `urllib3<2`
  (см. `requirements-dsm6.txt`; для Python 3.10+ — `requirements.txt`).
- Transmission: `127.0.0.1:9091` (RPC, логин/пароль в `.env`).
- **FlareSolverr**: Docker-контейнер на `127.0.0.1:8191` (порт наружу не публикуется),
  решает Cloudflare-челлендж и отдаёт cookies + UA.
- `PROXY_URL=direct`, `TELEGRAM_PROXY_URL=` (пусто) — оба выхода работают напрямую.
- Загрузки: `/volume2/downloads2/{Movies,Series,Anime,Audiobooks,Music,Soft}`.
- Автозапуск: `/usr/local/etc/rc.d/S99torrent-bot.sh {start|stop|restart|status}`
  (под капотом `run-forever.sh` — супервизор с рестартом через 15 с).
- Логи: `/volume1/My-Home-Bot/torrent-bot.log` (сам бот, UTF-8) и `torrent-bot.out`
  (stdout, туда же падают ранние трейсбеки).

## 3. Структура репо

```
bot.py                     — точка входа, все хендлеры, меню, поиск, уведомления
config.py                  — чтение .env, единственное место, где строится dict прокси
transmission_client.py     — обёртка Transmission RPC (явно без прокси), format_torrent
trackers/base.py           — BaseTracker: сессия, прокси, cookies, капча, ретрай через солвер
trackers/rutracker.py      — логин, поиск, разбор раздачи (magnet/`.torrent`)
trackers/kinozal.py        — то же для kinozal.me
trackers/solver.py         — клиент FlareSolverr (локально, без прокси)
utils/torrent_parser.py    — bencode, определение категории, размеры, имена
deploy/install.sh          — установка/настройка .env/venv/автозапуска (ASCII, English)
deploy/check_proxy.py      — диагностика прокси/трекеров/Transmission (ASCII)
deploy/test_trackers.py    — проверка трекеров из консоли (ASCII)
deploy/openwrt/*           — варианты SOCKS/HTTP-прокси на OpenWrt (если PROXY_URL нужен)
requirements.txt           — Python 3.10+
requirements-dsm6.txt      — Python 3.8 (DSM 6.2), пины
README.md                  — подробная документация, раздел 8 — про Cloudflare/SNI
```

## 4. Команды бота

`/start` `/help` — приветствие и инлайн-меню (Статус, Статистика, Папки, Поиск,
Поиск rutracker, Поиск kinozal); под полем ввода — постоянная кнопка «☰ Меню».

- `/status` — **только активные** загрузки (качается + раздаётся); пауза/проверка/готовые скрыты.
- `/stats` — счётчики и текущие скорости.
- `/dirs` — папки по категориям.
- `/search` — поиск по обоим трекерам; кнопки «Поиск rutracker»/«Поиск kinozal» — по одному.
- Обычный текст (не команда, не magnet, не ссылка) — тоже поиск.
- В запрос можно дописать год и тип: «хрустальный 2021 сериал» — год берётся
  из названия раздачи (год выхода, не дата торрента), тип фильтрует выдачу
  (сериал/фильм/аниме/музыка/аудиокнига/книга). Если под фильтр пусто —
  показывается обычная выдача с пометкой.
- `/login_rutracker`, `/login_kinozal` — логин (нужны для rutracker; kinozal ищет и без логина).
- `/cookies` — ручной импорт cookies + UA (резервный путь, если нет FlareSolverr).
- `/cancel` — сброс состояния.

Входные форматы: magnet, файл `.torrent`, ссылка на страницу раздачи.
Выдача поиска: полные названия в тексте сообщения, кнопки короткие
(`1. [rutracker] 92↑ 2.21 GB`), после выбора — экран выбора папки с кнопкой
«🌐 Открыть раздачу» и подсказкой категории (⭐ = предлагаемая).

## 5. Переменные `.env`

`TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` (через запятую), `PROXY_URL` (для трекеров;
`direct`/пусто — без прокси), `PROXY_TIMEOUT`, `TELEGRAM_PROXY_URL` (пусто, если Telegram
доступен напрямую), `FLARESOLVERR_URL` (по умолчанию `http://127.0.0.1:8191/v1`,
`none`/пусто — выключить), `TRANSMISSION_HOST/PORT/USER/PASSWORD`,
`DOWNLOAD_DIR_*` + keyword-списки категорий, `RUTRACKER_LOGIN/PASSWORD`,
`KINOZAL_LOGIN/PASSWORD`, `COOKIES_FILE`, `COOKIES_TTL_DAYS`, `LOG_FILE`, `LOG_LEVEL`,
`POLL_INTERVAL`, `NOTIFY_ON_COMPLETE`, `USER_AGENT`.
Образец — `.env.example`; сам `.env` в git не попадает.

## 6. Деплой и эксплуатация

```bash
cd /volume1/My-Home-Bot && sudo git pull && sudo /usr/local/etc/rc.d/S99torrent-bot.sh restart
sudo /usr/local/etc/rc.d/S99torrent-bot.sh status
tail -f /volume1/My-Home-Bot/torrent-bot.log     # ранние трейсбеки — в torrent-bot.out
sudo .venv/bin/python deploy/check_proxy.py      # прокси, трекеры, Transmission
sudo .venv/bin/python deploy/test_trackers.py matrix   # проверка поиска
```

Первичная установка/переустановка: `sudo sh deploy/install.sh` (интерактивно спросит `.env`).

SSH на MySRV (ключ уже установлен):

```powershell
ssh -i "$env:USERPROFILE\.ssh\id_ed25519" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no Kovalsky@192.168.1.51
```

Пользователь только `Kovalsky` (uid 1026); `admin`/`root` по SSH недоступны,
**sudo — только с паролем** (значит команды от root выполняет пользователь).
Docker-сокет доступен только root ⇒ FlareSolverr тоже запускает пользователь.

## 7. Обход Cloudflare (ключевое)

Трекеры за Cloudflare. curl/curl_cffi не исполняют JavaScript, поэтому получают челлендж
(403 `Just a moment...`). Решение: локальный **FlareSolverr** (`trackers/solver.py`) —
при `CaptchaError` бот один раз спрашивает у него свежие cookies + UA, подставляет в сессию
трекера и повторяет запрос (`base.py: _request → _solve_challenge → retry`).
Важно: у солвера надо запрашивать **защищённую** страницу (`login.php`, `tracker.php?nm=…`;
`index.php` не защищён и вернёт пустой `cf_clearance`).

Запуск/обновление солвера (выполняет пользователь):

```bash
sudo docker run -d --name flaresolverr --restart unless-stopped \
  -p 127.0.0.1:8191:8191 -e LOG_LEVEL=info --shm-size=1g \
  ghcr.io/flaresolverr/flaresolverr:latest
```

Проверка: POST `{"cmd":"request.get","url":"https://rutracker.org/forum/tracker.php?nm=matrix","maxTimeout":120000}`
на `http://127.0.0.1:8191/v1` → `"status":"ok"` и непустой `cf_clearance`.

## 8. Что было выяснено и почему сделано именно так

- Провайдер на прежней линии фильтровал по **SNI** и на TCP, и на QUIC (расшифровывал
  QUIC Initial), ECH тоже резался ⇒ HTTP/3 и сокрытие имени хоста не работают.
- Прокси на OpenWrt (`socks5h://…`) остался в коде как опция (`PROXY_URL`), но на текущей
  площадке не нужен: трекеры доступны напрямую, а челлендж решает FlareSolverr.
- **Docker/Podman на OpenWrt-шлюзе ставить нельзя** — установка `dockerd` тянет
  br_netfilter/veth/nf-ipvs и ломает fw4/tproxy/DNS (проверено, восстанавливалось из бэкапа).
  Шлюз 192.168.1.1 не трогать.
- Headless Chromium на шлюзе и на NAS пробовали: на шлюзе мешает отсутствие WebGL/дисплея
  (ANGLE/Vulkan не инициализируется), на NAS — glibc 2.20 (нужен ≥2.28). Поэтому FlareSolverr
  в Docker на NAS — единственный рабочий на сегодня путь.

## 9. Правила работы с проектом

- Все файлы в `deploy/` (shell, init, service) — **только ASCII + English**; Python-код бота
  и README — по-русски.
- В репозиторий не коммитить реальные IP, порты и секреты — только плейсхолдеры
  (`192.168.1.2`, `<IP_OpenWrt>`). `.env` в `.gitignore`.
- Ничего не устанавливать без явного разрешения пользователя; не угадывать — спрашивать.
- Пароли/токены из `.env` не печатать в чат (токен бота однажды уже перевыпускали).

## 10. Состояние на момент передачи

- Работает и проверено живьём: логин rutracker, поиск по обоим трекерам с сидами и объёмом,
  получение `.torrent`/magnet, добавление в Transmission с раскладкой по папкам, уведомления
  о завершении, `/status` только по активным.
- Последние коммиты `main`: `61b7162` (полные названия в сообщении, короткие кнопки,
  кнопка «Открыть раздачу»), `4490ba4` (rutracker: обязательное поле `login=submit`),
  `a34ed37` (UA из cookies закрепляется поверх impersonate), `f05c16b` (ссылка
  `//dl.kinozal.me/...`, сохранение cookies, ложная капча), `6d51c8a` (FlareSolverr),
  `18effba` (кнопки поиска, текст = поиск, сиды+объём).
- Открытых задач нет; после `git pull` на NAS нужно один раз выполнить `/login_rutracker`
  (старые cookies были без `bb_session`).
