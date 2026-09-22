#!/bin/sh
# =============================================================================
#  Установка torrent-bot на Synology DSM 6.2 (Xpenology) через systemd.
#
#  Запускать от root:
#      sudo sh deploy/install.sh
#
#  Переменные окружения (необязательно):
#      INSTALL_DIR=/volume1/torrent-bot   куда ставить (по умолчанию — родитель deploy/)
#      BOT_USER=root                      от чьего имени работать
#      PYTHON=/path/to/python3            явный интерпретатор Python 3.8+
# =============================================================================
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
INSTALL_DIR="${INSTALL_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BOT_USER="${BOT_USER:-root}"
SERVICE_NAME=torrent-bot
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

if [ "$(id -u)" != "0" ]; then
	err "Запустите от root: sudo sh deploy/install.sh"
	exit 1
fi

# --- 1. Находим Python 3.8+ ------------------------------------------------ #
find_python() {
	for candidate in \
		"${PYTHON:-}" \
		/var/packages/py3k/target/usr/local/bin/python3 \
		/var/packages/python3/target/usr/local/bin/python3 \
		/usr/local/bin/python3 \
		/usr/bin/python3
	do
		[ -n "$candidate" ] || continue
		[ -x "$candidate" ] || continue
		if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
			echo "$candidate"
			return 0
		fi
	done
	return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
	err "Python 3.8+ не найден."
	err "Установите пакет 'Python3' из Package Center (Synology) или SynoCommunity."
	err "Либо укажите интерпретатор: PYTHON=/path/to/python3 sudo -E sh deploy/install.sh"
	exit 1
fi
log "Python: $PY ($("$PY" --version 2>&1))"

# --- 2. Виртуальное окружение --------------------------------------------- #
log "Создаю venv: $INSTALL_DIR/.venv"
if ! "$PY" -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
	log "venv напрямую не сработал (нет ensurepip) — пробую virtualenv..."
	"$PY" -m ensurepip --upgrade 2>/dev/null || true
	"$PY" -m pip install --quiet --upgrade pip virtualenv || {
		err "Не удалось подготовить pip/virtualenv."
		err "Попробуйте вручную: $PY -m ensurepip --upgrade"
		exit 1
	}
	"$PY" -m virtualenv "$INSTALL_DIR/.venv"
fi

VENV_PY="$INSTALL_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || { err "venv создан неверно: нет $VENV_PY"; exit 1; }

log "Устанавливаю зависимости..."
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
log "Зависимости установлены."

# PySocks — критично для socks5h://. Проверяем явно.
if ! "$VENV_PY" -c 'import socks' 2>/dev/null; then
	err "Модуль socks (PySocks) не установился — socks5h:// работать не будет!"
	exit 1
fi
log "PySocks на месте (socks5h:// поддержан)."

# --- 3. Конфиг ------------------------------------------------------------ #
if [ ! -f "$INSTALL_DIR/.env" ]; then
	cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
	log ".env создан из .env.example — ОБЯЗАТЕЛЬНО отредактируйте его:"
	log "    nano $INSTALL_DIR/.env"
else
	log ".env уже существует — не трогаю."
fi

# --- 4. systemd unit ------------------------------------------------------ #
if [ ! -f "$SCRIPT_DIR/torrent-bot.service" ]; then
	err "Не найден $SCRIPT_DIR/torrent-bot.service"
	exit 1
fi

log "Устанавливаю systemd-юнит: $UNIT_PATH"
sed \
	-e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
	-e "s|__BOT_USER__|$BOT_USER|g" \
	"$SCRIPT_DIR/torrent-bot.service" > "$UNIT_PATH"

chmod 644 "$UNIT_PATH"
systemctl daemon-reload

# --- 5. Запуск ------------------------------------------------------------ #
if grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$INSTALL_DIR/.env" 2>/dev/null; then
	log "Включаю и запускаю сервис..."
	systemctl enable --now "$SERVICE_NAME"
	sleep 2
	systemctl --no-pager status "$SERVICE_NAME" || true
	log "Готово. Логи: journalctl -u $SERVICE_NAME -f   (или $INSTALL_DIR/torrent-bot.log)"
else
	log "TELEGRAM_BOT_TOKEN ещё не заполнен — сервис НЕ запускаю."
	log "1) nano $INSTALL_DIR/.env"
	log "2) systemctl enable --now $SERVICE_NAME"
fi
