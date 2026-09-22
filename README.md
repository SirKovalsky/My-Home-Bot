# torrent-bot — Telegram-бот автозакачки торрентов в Transmission

Бот живёт на **Xpenology** и делает ровно две вещи:

1. ходит к трекерам (`rutracker.org`, `kinozal.me`) **только через прокси на OpenWrt**;
2. отдаёт полученный `.torrent`/magnet в **локальный Transmission RPC без прокси**.

```
        Telegram
           │  (интернет)
           ▼
   ┌─────────────────┐        SOCKS5/HTTP        ┌──────────────┐
   │  бот на Xpeno   │ ───────────────────────▶  │   OpenWrt    │ ──▶ rutracker.org
   │  (Python)       │   proxies={...}           │  (прокси)    │ ──▶ kinozal.me
   │                 │
   │                 │   БЕЗ прокси              ┌──────────────┐
   │                 │ ───────────────────────▶  │ Transmission │ ──▶ P2P напрямую
   └─────────────────┘   localhost:9091          └──────────────┘
```

> Никаких iptables/nftables, никакого прозрачного проксирования, никакого
> реверс-туннеля. Только явный параметр `proxies` в HTTP-сессии.

---

## 1. Структура проекта

```
torrent-bot/
├── .env.example              # шаблон конфигурации
├── .gitignore
├── requirements.txt
├── README.md
├── bot.py                    # точка входа, aiogram
├── config.py                 # чтение .env (единственное место с прокси-словарём)
├── transmission_client.py    # RPC-обёртка БЕЗ прокси
├── trackers/
│   ├── __init__.py
│   ├── base.py               # общий интерфейс + сессия с явным прокси
│   ├── rutracker.py          # логин + парсинг + .torrent через прокси
│   └── kinozal.py            # то же для kinozal
├── utils/
│   ├── __init__.py
│   └── torrent_parser.py     # bencode-разбор, категория, download-dir
└── deploy/                   # развёртывание на DSM 6.2
    ├── install.sh            # venv + зависимости + systemd-юнит
    └── torrent-bot.service   # шаблон systemd-юнита
```

---

## 2. Установка

### 2.1. На Xpenology / Synology DSM 6.2 (Xpenology DSM 6.2.3-25426)

DSM 6.2 основан на **systemd**, поэтому самый удобный способ автозапуска —
собственный systemd-юнит. Готовые файлы лежат в `deploy/`.

**Что нужно один раз включить в DSM:**

1. **SSH** — *Control Panel → Terminal & SNMP → Enable SSH service*.
2. **Python 3.8+** — Package Center → *Python3* (пакет Synology; на DSM 6.2 это
   Python 3.8, ставится в `/var/packages/py3k/target/usr/local/bin/python3`).
   Альтернатива — пакет `python3` из **SynoCommunity** (репозиторий
   `https://packages.synocommunity.com`).
3. *(опционально)* **Git** — тоже из Package Center / SynoCommunity, если хотите
   деплоить через `git pull`.

> Проверить, что Python на месте:
> ```bash
> /var/packages/py3k/target/usr/local/bin/python3 --version   # ждём 3.8.x
> ```
> На DSM встроенный `python3` из `$PATH` может отсутствовать — путь выше рабочий.

**Установка (рекомендуемый путь):**

```bash
# 1) клонируем/кладём проект в папку на томе (не в /tmp — он чистится)
cd /volume1
git clone git@github.com:SirKovalsky/My-Home-Bot.git torrent-bot
#    либо (если git недоступен) просто скопируйте папку по SFTP/File Station

# 2) установка: venv + зависимости + systemd-юнит
cd /volume1/torrent-bot
sudo sh deploy/install.sh

# 3) заполняем .env
sudo nano .env

# 4) запускаем (скрипт сам запустит, если токен уже был заполнен)
sudo systemctl enable --now torrent-bot
sudo systemctl status torrent-bot
```

Скрипт `deploy/install.sh` делает всё сам: находит Python 3.8+ (`py3k`),
создаёт `.venv`, ставит зависимости, **проверяет наличие PySocks** (без него
`socks5h://` не работает), создаёт `.env` из `.env.example` и ставит
systemd-юнит `torrent-bot.service`.

Полезные команды:

```bash
journalctl -u torrent-bot -f          # живой лог сервиса
tail -f /volume1/torrent-bot/torrent-bot.log
sudo systemctl restart torrent-bot
sudo systemctl stop torrent-bot
```

**Обновление версии:**

```bash
cd /volume1/torrent-bot
sudo git pull
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart torrent-bot
```

### 2.2. Установка вручную (если не хотите install.sh)

```bash
cd /volume1/torrent-bot
PY=/var/packages/py3k/target/usr/local/bin/python3

$PY -m venv .venv          # если падает без ensurepip — см. примечание ниже
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
.venv/bin/python bot.py
```

Если `python3 -m venv` падает (в пакете Synology иногда нет `ensurepip`):

```bash
$PY -m ensurepip --upgrade
$PY -m pip install virtualenv
$PY -m virtualenv .venv
```

`PySocks` ставится автоматически вместе с `requests[socks]` — **без него схемы
`socks5h://` не работают** (ошибка `Missing dependencies for SOCKS support`).

### 2.3. Автозапуск через DSM Task Scheduler (альтернатива systemd)

Если не хочется трогать systemd: *Control Panel → Task Scheduler → Create →
Triggered Task → **Boot-up***, пользователь **root**, и такой скрипт:

```bash
#!/bin/sh
cd /volume1/torrent-bot
exec ./.venv/bin/python bot.py >> /volume1/torrent-bot/torrent-bot.log 2>&1
```

Минус: нет автоперезапуска при падении — это даёт только systemd-юнит.

### 2.4. Запуск из исходников на другой ОС (Linux/macOS)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env && nano .env
python bot.py
```

---

## 3. Переменные `.env`

### Telegram
| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USER_IDS` | ваш Telegram user id (можно несколько через запятую). Пусто = доступ всем — **не рекомендуется** |

### Прокси (только для трекеров!)
| Переменная | Описание |
|---|---|
| `PROXY_URL` | адрес прокси на OpenWrt. **Используйте `socks5h://`** — тогда DNS резолвит сам прокси. Пример: `socks5h://192.168.1.2:1080` |
| `PROXY_TIMEOUT` | таймаут запросов к трекерам, сек (по умолчанию 30) |

### Transmission (без прокси)
| Переменная | Описание |
|---|---|
| `TRANSMISSION_HOST` / `TRANSMISSION_PORT` | RPC, по умолчанию `127.0.0.1:9091` |
| `TRANSMISSION_USER` / `TRANSMISSION_PASSWORD` | если включена аутентификация RPC |
| `TRANSMISSION_RPC_PATH` | по умолчанию `/transmission/rpc` |
| `TRANSMISSION_PROTOCOL` | `http` обычно |
| `TRANSMISSION_TIMEOUT` | таймаут RPC, сек |

### Роутинг по папкам
| Переменная | Описание |
|---|---|
| `DOWNLOAD_DIR_DEFAULT` | папка «по умолчанию» |
| `DOWNLOAD_DIR_SERIES` | папка для сериалов (напр. `/volume1/Downloads/Series`) |
| `DOWNLOAD_DIR_FILMS` | папка для фильмов (напр. `/volume1/Downloads/Films`) |
| `SERIES_KEYWORDS` | ключевые слова категории «сериалы» |
| `FILMS_KEYWORDS` | ключевые слова категории «фильмы» |

Категория определяется по имени торрента (из `.torrent`/magnet) в
`utils/torrent_parser.py`. Ключевые слова можно менять без правки кода.

### Учётные данные трекеров
| Переменная | Описание |
|---|---|
| `RUTRACKER_LOGIN` / `RUTRACKER_PASSWORD` | логин rutracker.org |
| `KINOZAL_LOGIN` / `KINOZAL_PASSWORD` | логин kinozal.me |

### Прочее
| Переменная | Описание |
|---|---|
| `COOKIES_FILE` | базовый путь для cookies (создаются `*.rutracker.pickle`, `*.kinozal.pickle`) |
| `COOKIES_TTL_DAYS` | сколько дней доверять сохранённым cookies (0 = всегда доверять) |
| `LOG_FILE` / `LOG_LEVEL` | файл и уровень логов (`INFO`, `DEBUG`) |
| `POLL_INTERVAL` | период опроса Transmission для уведомлений, сек |
| `NOTIFY_ON_COMPLETE` | `true`/`false` — слать ли уведомление о завершении |
| `USER_AGENT` | реальный браузерный UA (трекеры банят дефолтный) |

---

## 4. Как узнать IP/порт SOCKS5 на OpenWrt

Прокси на OpenWrt — это обычно **Shadowsocks-libev (`ss-local`)**, **v2ray/xray**,
**trojan** или **redsocks/3proxy/privoxy** для HTTP. Задача: найти локальный
SOCKS5-порт и **выставить прослушивание на LAN-адрес**, чтобы Xpenology мог
подключиться по сети (а не только `127.0.0.1`).

### 4.1. Найти порт

```bash
ssh root@192.168.1.2            # ваш OpenWrt

# кто вообще слушает TCP
netstat -lnpt                    # или: ss -lnpt
# ждём строки вида 0.0.0.0:1080 / 127.0.0.1:1080 / 192.168.1.2:1080

# конфиг Shadowsocks-libev
uci show shadowsocks-libev
cat /etc/config/shadowsocks-libev

# если используется v2ray/xray — посмотрите inbound'ы
cat /etc/xray/config.json 2>/dev/null | grep -A5 socks
```

Типичные порты: **1080** (Shadowsocks/`ss-local`), **1081**, **8118** (privoxy, HTTP),
**3128**, **10808** (xray socks).

### 4.2. Разрешить прослушивание на LAN

Если прокси слушает только `127.0.0.1`, Xpenology его не увидит. Варианты:

**Shadowsocks-libev** — задайте `local_address` (адрес прослушивания):

```bash
uci set shadowsocks-libev.@shadowsocks-libev[0].local_address='0.0.0.0'
uci set shadowsocks-libev.@shadowsocks-libev[0].local_port='1080'
uci commit shadowsocks-libev
/etc/init.d/shadowsocks-libev restart
```

Либо запустить отдельный `ss-local`, слушающий LAN:

```bash
ss-local -s <server> -p <port> -k <password> -m aes-256-gcm -b 0.0.0.0 -l 1080
```

**xray/v2ray** — в inbound'е `"listen": "0.0.0.0"`, `"protocol": "socks"`.

**3proxy / dante** — в конфиге `socks -p1080 -i0.0.0.0` (для dante — `external:`).

### 4.3. Открыть порт в firewall OpenWrt

```bash
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-SOCKS-from-LAN'
uci set firewall.@rule[-1].src='lan'
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].dest_port='1080'
uci set firewall.@rule[-1].target='ACCEPT'
uci commit firewall
/etc/init.d/firewall restart
```

### 4.4. Проверить с Xpenology (через прокси)

```bash
# TCP-доступность
nc -vz 192.168.1.2 1080

# запрос через SOCKS5 — должен вернуть внешний IP прокси
curl -x socks5h://192.168.1.2:1080 https://api.ipify.org; echo
```

Если `curl -x socks5h://...` выдаёт ваш прокси-IP, а без `-x` — IP провайдера,
всё настроено правильно. В `.env` пишите ровно:

```env
PROXY_URL=socks5h://192.168.1.2:1080
```

> **Почему `socks5h`, а не `socks5`?** `h` = DNS через прокси. Тогда имена
> `rutracker.org`/`kinozal.me` резолвит OpenWrt. Это важно, потому что с
> Xpenology эти домены могут не резолвиться/блокироваться, а на прокси-стороне
> резолв корректный.

---

## 5. Как убедиться, что Transmission **НЕ** идёт через прокси

Transmission в этом проекте вообще не знает про прокси: бот обращается к нему по
`127.0.0.1:9091`, а клиент создаётся с `trust_env=False` и `proxies={}`
(см. `transmission_client.py`).

### 5.1. Смотреть активные соединения процесса

На Xpenology:

```bash
# 1) находим PID Transmission
ps w | grep transmission

# 2) смотрим, куда он ходит
netstat -antp 2>/dev/null | grep transmission
# или
ss -tnp | grep transmission
```

Ожидаемая картина: соединения с **внешними IP пиров/трекеров** (порты 51413,
443, 80). **Не должно быть** устойчивых соединений с `192.168.1.2:1080/8118`
(адресом прокси OpenWrt) — их и не будет, потому что Transmission работает
напрямую.

### 5.2. Проверить, что RPC доступен без прокси

```bash
# RPC отвечает напрямую, БЕЗ -x/--socks
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9091/transmission/rpc
# 409 или 200 = ок (409 = нужен X-Transmission-Session-Id, это норма)

# а с прокси НЕ должно требоваться:
curl --noproxy '*' -sI http://127.0.0.1:9091/transmission/rpc
```

### 5.3. Убедиться, что в окружении нет глобального прокси

```bash
env | grep -i proxy        # должно быть пусто
```

Бот его и не использует: `trust_env=False` у обеих сессий. Даже если кто-то
пропишет `HTTP_PROXY`, клиент Transmission и RPC останутся прямыми.

### 5.4. Проверить, что трекеры идут **через** прокси

```bash
python - <<'PY'
import requests
s = requests.Session()
# как в боте: явный прокси + игнор окружения
s.proxies.update({"http": "socks5h://192.168.1.2:1080",
                  "https": "socks5h://192.168.1.2:1080"})
s.trust_env = False
print(s.get("https://api.ipify.org", timeout=20).text)   # IP прокси
PY
```

Если IP совпадает с прокси OpenWrt — маршрутизация трекеров корректна.

---

## 6. Использование бота

| Команда | Что делает |
|---|---|
| `/start`, `/help` | краткая справка |
| `/login_rutracker` | логин на rutracker (логин/пароль из `.env`), cookies сохраняются |
| `/login_kinozal` | то же для kinozal |
| `/status` | активные загрузки Transmission |
| `/stats` | суммарные скорости и количество |

Приём сообщений:

- **magnet-ссылка** текстом → сразу в Transmission;
- **`.torrent` документом** → сразу в Transmission;
- **ссылка на страницу** `rutracker.org/forum/viewtopic.php?t=...` или
  `kinozal.me/details.php?id=...` → бот парсит страницу **через прокси**,
  находит `.torrent`/magnet, скачивает `.torrent` **через прокси** и отдаёт в
  Transmission **напрямую**.

Роутинг папок: по ключевым словам в имени торрента выбирается
`DOWNLOAD_DIR_SERIES` / `DOWNLOAD_DIR_FILMS` / `DOWNLOAD_DIR_DEFAULT`, значение
уходит в RPC как `download-dir`.

Уведомления: сообщение при добавлении и отдельное — при завершении загрузки.

---

## 7. Разбор ошибок

| Симптом в логе/чате | Причина и что делать |
|---|---|
| `Missing dependencies for SOCKS support` | не установлен PySocks → `pip install "requests[socks]"` |
| `Прокси ... недоступен` | OpenWrt не слушает LAN, порт закрыт firewall или неверный `PROXY_URL` (см. раздел 4) |
| `Обнаружена капча/антибот` | трекер требует капчу; подождите, смените UA/прокси, проверьте логин |
| `rutracker не выдал cookie bb_session` | неверный логин/пароль, либо нужен вход через браузер (проверьте данные в `.env`) |
| `требует авторизацию` | истекли cookies → `/login_rutracker` или `/login_kinozal` |
| `Transmission недоступен` | RPC выключен, неверный порт/логин, или `localhost` в контейнере ≠ хост |
| Трекер отдал `403/429` | бан UA/лимит; смените `USER_AGENT`, уменьшите частоту запросов |

Логи пишутся одновременно в stdout и в файл `LOG_FILE` (по умолчанию
`torrent-bot.log`).

---

## 8. Что сознательно НЕ делается

- ❌ iptables/nftables на OpenWrt и Xpenology;
- ❌ заворачивание Transmission (и P2P) в прокси;
- ❌ реверс-прокси и туннели между Xpenology и OpenWrt;
- ❌ системный прокси ОС и глобальные `HTTP_PROXY`/`HTTPS_PROXY`.

Единственная точка проксирования — `proxies=...` в сессиях трекеров
(`trackers/base.py`, комментарии помечены `>>> ЗДЕСЬ ЗАДАЁТСЯ ПРОКСИ <<<`).
Единственная точка «явно без прокси» — `transmission_client.py`
(`>>> ЯВНО БЕЗ ПРОКСИ <<<`).
