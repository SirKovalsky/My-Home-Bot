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
├── .gitattributes
├── requirements.txt          # зависимости для Python 3.10+
├── requirements-dsm6.txt     # зависимости для Python 3.8 (DSM 6.2)
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
└── deploy/                   # развёртывание
    ├── install.sh            # venv + зависимости + systemd-юнит (DSM 6.2)
    ├── torrent-bot.service   # шаблон systemd-юнита (DSM 6.2)
    ├── check_proxy.py        # диагностика прокси/Transmission без nc и curl
    └── openwrt/
        ├── socks-lan.init    # проброс SOCKS5 в LAN через socat (procd)
        └── confdir/
            └── socks-lan.json # SOCKS-вход для --v2ray-confdir (без пакетов)
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

> ⚠️ **Про версию Python на DSM 6.2.** Штатный пакет Synology — это **Python 3.8**,
> а актуальные `aiogram` (≥3.14), `requests` (≥2.33) и `python-dotenv` (≥1.1)
> требуют **Python ≥3.10/3.9**. Поэтому для 3.8 в репозитории лежит отдельный
> `requirements-dsm6.txt` с последними совместимыми версиями, а `install.sh`
> выбирает его автоматически. Вручную тогда:
> `.venv/bin/pip install -r requirements-dsm6.txt`.
> Хотите свежие библиотеки — поставьте новый Python из SynoCommunity
> (например, `python311`) и укажите его при установке:
> `PYTHON=/var/packages/python311/target/usr/local/bin/python3 sudo -E sh deploy/install.sh`.
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
# Python 3.8 (штатный на DSM 6.2):
.venv/bin/pip install -r requirements-dsm6.txt
# ...либо, если у вас Python 3.9+:
# .venv/bin/pip install -r requirements.txt
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

### 2.4. Настройка Transmission RPC на DSM (перед первым запуском бота)

Бот общается с Transmission по `127.0.0.1:9091`, поэтому RPC должен быть включён.

**SynoCommunity `transmission`** — включите RPC в настройках пакета
(*Package Center → transmission → RPC*), либо прямо в `settings.json`:

```bash
# путь у SynoCommunity-пакета:
sudo vi /volume1/@appstore/transmission/var/settings.json
```

Ключевые поля:

```jsonc
{
  "rpc-enabled": true,
  "rpc-bind-address": "127.0.0.1",   // ТОЛЬКО localhost, наружу не светим
  "rpc-port": 9091,
  "rpc-whitelist-enabled": true,
  "rpc-whitelist": "127.0.0.1",
  "rpc-username": "",                 // если зададите — продублируйте в .env
  "rpc-password": ""                  // Transmission хранит его в виде хэша
}
```

Затем перезапустите пакет (Package Center → transmission → Restart) и проверьте.
На DSM нет `curl`, поэтому проверяем питоном из venv проекта:

```bash
cd /volume1/torrent-bot
.venv/bin/python - <<'PY'
import requests
s = requests.Session()
s.trust_env = False          # игнорировать HTTP_PROXY/HTTPS_PROXY из окружения
s.proxies = {}               # прокси нет
r = s.get("http://127.0.0.1:9091/transmission/rpc", timeout=10)
print("HTTP", r.status_code, "- 409 или 200 означают, что RPC жив")
PY
```

Если `rpc-username`/`rpc-password` заполнены — укажите их в `.env`
(`TRANSMISSION_USER` / `TRANSMISSION_PASSWORD`).

> **Про пользователя бота.** `deploy/install.sh` по умолчанию ставит
> `User=root` — так проще всего и работает всегда (pid-файлы не нужны).
> Если хотите строже, используйте `BOT_USER=sc-transmission`, но тогда этот
> пользователь должен иметь право писать в `/volume1/torrent-bot` (`.env`,
> `cookies.*.pickle`, `torrent-bot.log`):
> ```bash
> sudo chown -R sc-transmission:users /volume1/torrent-bot
> sudo BOT_USER=sc-transmission sh deploy/install.sh
> ```

### 2.5. Запуск из исходников на другой ОС (Linux/macOS)

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
| `PROXY_URL` | SOCKS5-вход на OpenWrt. **Используйте `socks5h://`** — тогда DNS резолвит сторона прокси. Пример: `socks5h://192.168.1.2:20170` |
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
| `DOWNLOAD_DIR_MOVIES` | фильмы, в т.ч. полнометражные аниме и мультфильмы |
| `DOWNLOAD_DIR_SERIES` | сериалы, в т.ч. мультипликационные и 3D |
| `DOWNLOAD_DIR_ANIME` | только многосерийное аниме |
| `DOWNLOAD_DIR_AUDIOBOOKS` | аудиокниги |
| `DOWNLOAD_DIR_MUSIC` | музыка |
| `DOWNLOAD_DIR_SOFT` | софт |
| `DOWNLOAD_DIR_DEFAULT` | запасной путь, если категория не определена |
| `CONFIRM_TTL` | сколько секунд торрент ждёт подтверждения папки |

Ключевые слова категорий (меняются без правки кода):

| Переменная | Категория |
|---|---|
| `MOVIES_KEYWORDS` | фильмы / мультфильмы |
| `SERIES_KEYWORDS` | сериалы |
| `ANIME_KEYWORDS` | аниме |
| `AUDIOBOOKS_KEYWORDS` | аудиокниги |
| `MUSIC_KEYWORDS` | музыка |
| `SOFT_KEYWORDS` | софт |
| `EPISODE_MARKERS` | маркеры многосерийности (для отличия сериала/аниме от полнометражки) |

**Бот не выбирает папку молча.** Он определяет категорию (`utils/torrent_parser.py`),
показывает её как предложенную (⭐) и ждёт нажатия кнопки. Приоритет автоопределения:
аудиокниги → музыка → софт → аниме → сериалы → фильмы → прочее. Для аниме логика
такая: есть признаки сериальности (сезон/S01E02/диапазон серий) → `Anime`,
иначе (полнометражное аниме) → `Movies`.

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

## 4. Прокси на OpenWrt: v2rayA / xray, SOCKS-inbound + WireGuard

### 4.1. Почему «прозрачный» редирект сам по себе не подходит

На OpenWrt работает **v2rayA** (веб-панель над ядром xray/v2ray), который по
своим правилам заворачивает
трафик **своих клиентов** (тех, кто ходит через OpenWrt как шлюз) на удалённый
сервер. Xpenology же стоит **за Keenetic, на одном уровне с OpenWrt**, и его шлюз
по умолчанию — Keenetic, а не OpenWrt. Поэтому:

- «прозрачные» правила v2rayA/xray (TPROXY/редирект) **трафик Xpenology не
  покрывают**;
- чтобы трафик к трекерам всё-таки ушёл через туннель, бот **сам** подключается
  к SOCKS5-входу на OpenWrt — это и есть явный `proxies={...}`, как и требует
  архитектура.

Значит, на OpenWrt нужно лишь **открыть SOCKS5-вход** (в v2rayA это делается в
панели, без правки файлов). Никакого прозрачного проксирования, nftables-правил
и реверс-туннелей не требуется.

### 4.2. Где у v2rayA «конфиг» и почему его не редактируют руками

У v2rayA **нет конфига, который вы бы правили вручную**. Настройки панель хранит
в своей базе, а конфиг ядра xray/v2ray **генерирует сама** при каждом запуске
(каталог по умолчанию — `/etc/v2raya`). Если отредактировать сгенерированный
JSON, v2rayA его перезапишет. Всё делается через веб-панель.

- Панель: `http://<IP_OpenWrt>:2017` (по умолчанию слушает `0.0.0.0:2017`).
- Посмотреть, что реально слушает ядро, можно так:
  ```bash
  ssh root@<IP_OpenWrt>
  netstat -lnpt | grep -E 'xray|v2ray'
  ```

### 4.3. Главный подводный камень: входы v2rayA слушают только 127.0.0.1

По умолчанию встроенные SOCKS5/HTTP-входы v2rayA (и пользовательские инбаунды)
биндутся на **`127.0.0.1`**, а на `0.0.0.0` переключаются только при включённой
опции **Port sharing**. Логика в коде v2rayA буквально такая:

```go
listenAddr := "127.0.0.1"
if t.Setting.PortSharing { listenAddr = "0.0.0.0" }
```

Поэтому по умолчанию SOCKS5-вход виден **только с самого OpenWrt**, и Xpenology
до него не достучится. Точные порты у вас видны в панели
(*Setting → Address and Ports*):

| Поле | Значение |
|---|---|
| Port of SOCKS5 | `20170` |
| Port of HTTP | `20171` |
| Port of SOCKS5 (with Rule) | `0` (закрыт) |
| Port of HTTP (with Rule) | `20172` |

> Если OpenWrt стоит **за Keenetic** (его WAN — это адрес из сети Keenetic), то
> порт наружу всё равно не смотрит: интернета за ним нет. Поэтому биндить можно
> спокойно на `0.0.0.0`, не заботясь о firewall.

### Почему НЕ включаем «Port Sharing»

Кнопка **Port Sharing** действительно переключает входы на `0.0.0.0` (в коде
v2rayA: `if PortSharing { listenAddr = "0.0.0.0" }`). Но в интерфейсе она стоит
рядом с `IP Forward` под «Transparent Proxy/System Proxy» — то есть это
**альтернативный режим реализации прозрачного прокси**, а не безобидный флаг
«открыть порт». Его включение меняет схему перехвата для **всех** клиентов,
ходящих через OpenWrt, — отсюда и поломки, с которыми вы воевали.

Нам оно не нужно — есть два способа обойтись без него.

### Способ A (без установки пакетов): дополнительный каталог конфигов v2rayA

v2rayA умеет запускать ядро с параметром `--confdir=<каталог>`, и файлы из этого
каталога **сливаются** с её сгенерированным конфигом. Это ровно наш случай: мы
добавляем свой SOCKS-вход отдельным файлом, а прозрачный прокси и Port Sharing
не трогаем.

Факты из исходников v2rayA:

```
core запускается как:  v2raya_core run --config=/etc/v2raya/config.json \
                                         --confdir=<V2rayConfigDirectory>
```

`<V2rayConfigDirectory>` задаётся флагом `--v2ray-confdir` **или** переменной
окружения `V2RAYA_V2RAY_CONFDIR`. Если параметр пуст — доп. каталог не читается.

Шаги:

```bash
ssh root@192.168.1.2

# 1) каталог для доп. конфигов
mkdir -p /etc/v2raya/confdir

# 2) файл из репозитория (deploy/openwrt/confdir/socks-lan.json)
cat > /etc/v2raya/confdir/socks-lan.json <<'JSON'
{
  "inbounds": [
    {
      "tag": "socks-lan",
      "listen": "0.0.0.0",
      "port": 1080,
      "protocol": "socks",
      "settings": { "auth": "noauth", "udp": false },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls"] }
    }
  ]
}
JSON

# 3) проверить, поддерживает ли ваш v2rayA этот параметр
v2raya --help 2>&1 | grep -i confdir
```

Если `confdir` в `--help` есть — прописываем его в запуск v2rayA. Как именно —
зависит от того, чем он стартует; посмотрите:

```bash
cat /etc/init.d/v2raya 2>/dev/null
cat /etc/config/v2raya 2>/dev/null
ps w | grep v2raya
```

- Если v2rayA стартует из init-скрипта — добавьте к команде запуска
  `--v2ray-confdir=/etc/v2raya/confdir`.
- Если через uci/env — задайте `V2RAYA_V2RAY_CONFDIR=/etc/v2raya/confdir`.
- Проще всего, если init-скрипт читает переменные: добавьте строку
  `export V2RAYA_V2RAY_CONFDIR=/etc/v2raya/confdir` перед запуском бинаря.

Затем перезапустить и проверить:

```bash
/etc/init.d/v2raya restart      # или: reboot
netstat -lnpt | grep 1080       # ждём 0.0.0.0:1080
```

В `.env`:

```env
PROXY_URL=socks5h://192.168.1.2:1080
```

Если `confdir` в `--help` отсутствует (слишком старая сборка) — переходите к B.

### Способ B: проброс порта (`socat`/`ncat`)

Подходит, если на роутере есть пакетный менеджер или нужная утилита уже стоит.
Проверить:

```bash
which apk opkg socat ncat nc
```

- есть `apk` → `apk add socat`
- есть `opkg` → `opkg update && opkg install socat`
- уже есть `socat` → просто запустить:

```bash
# 0.0.0.0 допустим, т.к. OpenWrt за Keenetic; для строгости можно bind=<LAN_IP>
socat TCP-LISTEN:20170,fork,reuseaddr TCP:127.0.0.1:20170
```

Чтобы поднималось при загрузке, в репозитории есть готовый procd-скрипт
`deploy/openwrt/socks-lan.init` (в нём при желании поменяйте `LAN_IP`/`SOCKS_PORT`):

```bash
scp deploy/openwrt/socks-lan.init root@192.168.1.2:/etc/init.d/socks-lan
ssh root@192.168.1.2
chmod +x /etc/init.d/socks-lan
/etc/init.d/socks-lan enable
/etc/init.d/socks-lan start
logread -e socks-lan
netstat -lnpt | grep 20170
```

В `.env` тогда:

```env
PROXY_URL=socks5h://192.168.1.2:20170
```

### Проверить маршрутизацию (для обоих способов)

И способ A, и способ B отдают обычный SOCKS-вход, трафик которого подчиняется
общим правилам v2rayA (у вас `Traffic Splitting Mode of Rule Port: RoutingA`).
Если `deploy/check_proxy.py` покажет, что внешний IP через прокси **совпадает**
с провайдерским, — трафик уходит в `direct`, и в правила v2rayA надо добавить
`rutracker.org` / `kinozal.me` в проксируемые. Если IP **разный** — всё готово.

### 4.3.1. Вариант надёжнее — Custom Inbound (только в новых версиях)

> ⚠️ В **старых LuCI-сборках** v2rayA этого пункта в панели нет — тогда
> используйте встроенный SOCKS5-порт из п. 4.3 (при необходимости + проброс
> через `socat`). Ниже — для тех, у кого v2rayA 2.3+.

В свежих версиях v2rayA есть **Custom Inbound**: свой SOCKS/HTTP-вход на
заданном порту с **явной привязкой к исходящему узлу**. Это гарантирует, что
трафик к трекерам уйдёт через ваш прокси, независимо от общих правил
маршрутизации (иначе socks-вход может попасть в `direct`).

В панели добавьте инбаунд примерно так:

| Поле | Значение |
|---|---|
| Protocol | `socks` |
| Port | `1080` (или 20170) |
| Outbound | ваша группа/узел удалённого сервера |
| Outbound type | `direct` — весь трафик этого входа идёт в выбранный outbound |
| Username / Password | опционально, если нужно ограничить доступ |

Помните: Custom Inbound **тоже** слушает `127.0.0.1`, пока не включён Port
sharing (см. 4.3).

Если в вашей версии v2rayA Custom Inbound нет — используйте встроенный
SOCKS5-порт из п. 4.3 и просто убедитесь, что режим правил (Proxy Mode) отправляет
`rutracker.org` / `kinozal.me` через прокси, а не напрямую.

### 4.4. Firewall

В дефолтной OpenWrt зона **lan** имеет `input ACCEPT`, поэтому отдельное правило
обычно **не нужно** — Xpenology просто достучится до `<IP_OpenWrt>:<SOCKS_PORT>`
(например `:20170`). Правило добавьте только если вы ужесточали firewall:

```bash
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-SOCKS-from-LAN'
uci set firewall.@rule[-1].src='lan'
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].dest_port='20170'
uci set firewall.@rule[-1].target='ACCEPT'
uci commit firewall
/etc/init.d/firewall restart
```

Учтите: `Port sharing` открывает вход на **всех** интерфейсах OpenWrt, включая
WAN, если он не изолирован. Если хотите ограничиться LAN — не полагайтесь на
`0.0.0.0`, а закройте WAN-вход правилом из этого раздела (src `wan`, target
`REJECT`) или задайте `Username`/`Password` в Custom Inbound.

### 4.5. WireGuard-сервер — не для бота

Поднятый на OpenWrt **WireGuard-сервер** нужен для доступа в домашнюю сеть
извне, и к маршрутизации трекер-трафика отношения не имеет. Не направляйте
`PROXY_URL` на WireGuard: боту нужен именно SOCKS5 к выходному узлу. P2P-трафик
Transmission и так идёт напрямую и в туннель не попадает.

### 4.6. Проверка с Xpenology (без nc и curl)

На DSM нет ни `nc`, ни `curl`, поэтому проверяем питоном из venv проекта:

```bash
cd /volume1/torrent-bot
sudo .venv/bin/python deploy/check_proxy.py
```

Скрипт `deploy/check_proxy.py` делает всё сразу: TCP до SOCKS-порта, запрос через
прокси и напрямую (сравнивает IP), доступность трекеров через прокси и ответ
Transmission. Пример успешного вывода:

```
[OK  ] 1) TCP до прокси 192.168.1.2:20170
[OK  ] 2) Выход через прокси
        HTTP 200, внешний IP через прокси: 203.0.113.7
[OK  ] 3) Выход напрямую (для сравнения)
        HTTP 200, прямой внешний IP: 198.51.100.23
[OK  ] 4) Трекеры через прокси
        https://rutracker.org/forum/index.php -> HTTP 200
[OK  ] 5) Transmission напрямую
```

Если IP в пунктах 2 и 3 **разные** — трекеры идут через туннель, всё верно.
В `.env` пишите ровно:

```env
PROXY_URL=socks5h://<IP_OpenWrt>:20170
```

> **Почему `socks5h`, а не `socks5`?** `h` = DNS через прокси. Тогда имена
> `rutracker.org`/`kinozal.me` резолвит сторона прокси. Это важно, потому что
> с Xpenology эти домены могут не резолвиться/блокироваться, а на стороне
> туннеля резолв корректный. У v2rayA домен к тому же уходит на прокси как имя
> (он умеет sniffing), так что связка `socks5h` + v2rayA работает корректно.

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
443, 80). **Не должно быть** устойчивых соединений с `<IP_OpenWrt>:20170`
(адресом SOCKS-входа v2rayA) — их и не будет, потому что Transmission работает
напрямую.

### 5.2. Проверить, что RPC доступен без прокси

На DSM нет `curl`, поэтому проверяем питоном из venv проекта:

```bash
cd /volume1/torrent-bot
.venv/bin/python - <<'PY'
import requests

# Строго БЕЗ прокси и БЕЗ учёта HTTP_PROXY/HTTPS_PROXY из окружения —
# ровно как клиент Transmission в боте.
s = requests.Session()
s.trust_env = False
s.proxies = {}
r = s.get("http://127.0.0.1:9091/transmission/rpc", timeout=10)
print("HTTP", r.status_code, "- 409 или 200 = RPC отвечает напрямую")
PY
```

### 5.3. Убедиться, что в окружении нет глобального прокси

```bash
env | grep -i proxy        # должно быть пусто
```

Бот его и не использует: `trust_env=False` у обеих сессий. Даже если кто-то
пропишет `HTTP_PROXY`, клиент Transmission и RPC останутся прямыми.

### 5.4. Проверить, что трекеры идут **через** прокси

Проще всего — встроенной проверкой (не нужны ни `curl`, ни `nc`):

```bash
cd /volume1/torrent-bot
sudo .venv/bin/python deploy/check_proxy.py
```

Скрипт проверит TCP до прокси, выход через прокси и напрямую (сравните IP!),
доступность `rutracker.org`/`kinozal.me` через прокси и ответ Transmission RPC.
Если IP через прокси отличается от прямого и совпадает с удалённым сервером —
маршрутизация трекеров корректна.

То же самое вручную:

```bash
cd /volume1/torrent-bot
.venv/bin/python - <<'PY'
import requests
s = requests.Session()
# как в боте: явный прокси + игнор окружения
s.proxies.update({"http": "socks5h://<IP_OpenWrt>:20170",
                  "https": "socks5h://<IP_OpenWrt>:20170"})
s.trust_env = False
print("через прокси:", s.get("https://api.ipify.org", timeout=20).text)
PY
```

---

## 6. Использование бота

| Команда | Что делает |
|---|---|
| `/start`, `/help` | краткая справка |
| `/login_rutracker` | логин на rutracker (логин/пароль из `.env`), cookies сохраняются |
| `/login_kinozal` | то же для kinozal |
| `/status` | активные загрузки Transmission |
| `/stats` | суммарные скорости и количество |
| `/dirs` | список папок загрузки по категориям |

Приём сообщений:

- **magnet-ссылка** текстом → бот предлагает папку;
- **`.torrent` документом** → бот предлагает папку;
- **ссылка на страницу** `rutracker.org/forum/viewtopic.php?t=...` или
  `kinozal.me/details.php?id=...` → бот парсит страницу **через прокси**,
  находит `.torrent`/magnet, скачивает `.torrent` **через прокси**, затем
  предлагает папку.

**Подтверждение папки.** Бот определяет категорию сам, помечает её звёздочкой ⭐
и показывает inline-клавиатуру из шести папок. В Transmission торрент уходит
только после нажатия кнопки; значение передаётся в RPC как `download-dir`.
Есть кнопка «❌ Отмена». Если категорию определить не удалось — звёздочки нет,
папку выбираете вручную. Неподтверждённые запросы живут `CONFIRM_TTL` секунд.

Уведомления: сообщение при добавлении и отдельное — при завершении загрузки.

---

## 7. Разбор ошибок

| Симптом в логе/чате | Причина и что делать |
|---|---|
| `Missing dependencies for SOCKS support` | не установлен PySocks → `pip install "requests[socks]"` |
| `Could not find a version that satisfies the requirement aiogram` / `requires a different Python` | Python 3.8 на DSM 6.2 и свежие версии библиотек → ставьте `requirements-dsm6.txt` (или новый Python из SynoCommunity) |
| `python3: command not found` | используйте полный путь `/var/packages/py3k/target/usr/local/bin/python3` |
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
