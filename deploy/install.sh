#!/bin/sh
# =============================================================================
#  torrent-bot installer for Synology DSM 6.2 (Xpenology), using systemd.
#
#  Run as root:
#      sudo sh deploy/install.sh
#
#  Optional environment variables:
#      INSTALL_DIR=/volume1/torrent-bot   target dir (default: parent of deploy/)
#      BOT_USER=root                      user the service runs as
#      PYTHON=/path/to/python3            explicit Python 3.8+ interpreter
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
	err "Run as root: sudo sh deploy/install.sh"
	exit 1
fi

# --- 1. Locate Python 3.8+ ------------------------------------------------ #
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
	err "Python 3.8+ not found."
	err "Install the 'Python3' package from Package Center (Synology) or SynoCommunity."
	err "Or pass one explicitly: PYTHON=/path/to/python3 sudo -E sh deploy/install.sh"
	exit 1
fi
log "Python: $PY ($("$PY" --version 2>&1))"

# --- 2. Virtual environment ----------------------------------------------- #
log "Creating venv: $INSTALL_DIR/.venv"
if ! "$PY" -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
	log "Plain venv failed (no ensurepip) - trying virtualenv..."
	"$PY" -m ensurepip --upgrade 2>/dev/null || true
	"$PY" -m pip install --quiet --upgrade pip virtualenv || {
		err "Failed to prepare pip/virtualenv."
		err "Try manually: $PY -m ensurepip --upgrade"
		exit 1
	}
	"$PY" -m virtualenv "$INSTALL_DIR/.venv"
fi

VENV_PY="$INSTALL_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || { err "venv looks broken: missing $VENV_PY"; exit 1; }

log "Installing dependencies..."
"$VENV_PY" -m pip install --quiet --upgrade pip

# DSM 6.2 ships Python 3.8, while recent aiogram/requests require 3.10+.
# For 3.8 use the pinned, compatible set.
PY_VERSION=$("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -gt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 9 ]; }; then
	REQ_FILE="$INSTALL_DIR/requirements.txt"
else
	REQ_FILE="$INSTALL_DIR/requirements-dsm6.txt"
	log "Python $PY_VERSION (DSM 6.2) - using compatible pins from requirements-dsm6.txt"
fi

if [ ! -f "$REQ_FILE" ]; then
	err "Requirements file not found: $REQ_FILE"
	exit 1
fi

"$VENV_PY" -m pip install --quiet -r "$REQ_FILE"
log "Dependencies installed ($(basename "$REQ_FILE"))."

# PySocks is critical for socks5h:// - verify explicitly.
if ! "$VENV_PY" -c 'import socks' 2>/dev/null; then
	err "PySocks is missing - socks5h:// will not work!"
	exit 1
fi
log "PySocks present (socks5h:// supported)."

# --- 3. Configuration ----------------------------------------------------- #
if [ ! -f "$INSTALL_DIR/.env" ]; then
	cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
	log ".env created from .env.example - EDIT IT:"
	log "    nano $INSTALL_DIR/.env"
else
	log ".env already exists - left unchanged."
fi

# --- 4. systemd unit ------------------------------------------------------ #
if [ ! -f "$SCRIPT_DIR/torrent-bot.service" ]; then
	err "Missing $SCRIPT_DIR/torrent-bot.service"
	exit 1
fi

log "Installing systemd unit: $UNIT_PATH"
sed \
	-e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
	-e "s|__BOT_USER__|$BOT_USER|g" \
	"$SCRIPT_DIR/torrent-bot.service" > "$UNIT_PATH"

chmod 644 "$UNIT_PATH"
systemctl daemon-reload

# --- 5. Start ------------------------------------------------------------- #
if grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$INSTALL_DIR/.env" 2>/dev/null; then
	log "Enabling and starting the service..."
	systemctl enable --now "$SERVICE_NAME"
	sleep 2
	systemctl --no-pager status "$SERVICE_NAME" || true
	log "Done. Logs: journalctl -u $SERVICE_NAME -f   (or $INSTALL_DIR/torrent-bot.log)"
else
	log "TELEGRAM_BOT_TOKEN is not set yet - not starting the service."
	log "1) nano $INSTALL_DIR/.env"
	log "2) systemctl enable --now $SERVICE_NAME"
fi
