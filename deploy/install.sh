#!/bin/sh
# =============================================================================
#  torrent-bot installer for Synology DSM 6.x (Xpenology).
#
#  Run as root:
#      sudo sh deploy/install.sh
#
#  Optional environment variables:
#      INSTALL_DIR=/volume1/My-Home-Bot   target dir (default: parent of deploy/)
#      BOT_USER=root                      user the service runs as (systemd only)
#      PYTHON=/path/to/python3            explicit Python 3.8+ interpreter
#
#  Autostart method is chosen automatically:
#      1) systemd unit   - if /etc/systemd/system exists (DSM 7, most Linux)
#      2) rc.d script    - /usr/local/etc/rc.d/ (DSM 6.x usual way)
#      3) otherwise      - prints DSM Task Scheduler instructions
# =============================================================================
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
INSTALL_DIR="${INSTALL_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BOT_USER="${BOT_USER:-root}"
SERVICE_NAME=torrent-bot
SYSTEMD_DIR=/etc/systemd/system
RCD_DIR=/usr/local/etc/rc.d

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
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

# --- 2. Virtual environment (reused if already present) ------------------- #
if [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
	log "Existing venv found - reusing $INSTALL_DIR/.venv"
else
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

# Smoke test: fail here rather than at runtime. Catches, for example, the
# urllib3 2.x vs OpenSSL 1.0.2u problem on DSM 6.2.
log "Verifying dependency imports..."
if ! IMPORT_OUT=$("$VENV_PY" -c 'import requests, urllib3, socks, aiogram, transmission_rpc, dotenv' 2>&1); then
	err "Dependency smoke test failed:"
	printf '%s\n' "$IMPORT_OUT" >&2
	err "On DSM 6.2 the usual cause is urllib3 2.x with OpenSSL 1.0.2u:"
	err "    $VENV_PY -m pip install 'urllib3<2'"
	exit 1
fi
# PySocks is critical for socks5h:// - verify explicitly.
if ! "$VENV_PY" -c 'import socks' 2>/dev/null; then
	err "PySocks is missing - socks5h:// will not work!"
	exit 1
fi
log "Dependencies import cleanly; PySocks present (socks5h:// supported)."

# --- 3. Configuration ----------------------------------------------------- #
if [ ! -f "$INSTALL_DIR/.env" ]; then
	cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
	log ".env created from .env.example - EDIT IT:"
	log "    nano $INSTALL_DIR/.env"
else
	log ".env already exists - left unchanged."
fi

# --- 4. Autostart ---------------------------------------------------------- #
install_systemd() {
	[ -d "$SYSTEMD_DIR" ] || return 1
	command -v systemctl >/dev/null 2>&1 || return 1
	[ -f "$SCRIPT_DIR/torrent-bot.service" ] || return 1

	sed \
		-e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
		-e "s|__BOT_USER__|$BOT_USER|g" \
		"$SCRIPT_DIR/torrent-bot.service" > "$SYSTEMD_DIR/${SERVICE_NAME}.service" || return 1
	chmod 644 "$SYSTEMD_DIR/${SERVICE_NAME}.service"
	systemctl daemon-reload || true
	log "systemd unit installed: $SYSTEMD_DIR/${SERVICE_NAME}.service"
	return 0
}

install_rcd() {
	mkdir -p "$RCD_DIR" 2>/dev/null || return 1
	RCD_SCRIPT="$RCD_DIR/S99${SERVICE_NAME}.sh"

	cat > "$RCD_SCRIPT" <<EOF
#!/bin/sh
# Autostart script for torrent-bot (placed in $RCD_DIR).
# Usage: $RCD_SCRIPT {start|stop|restart|status}

BOT_DIR="$INSTALL_DIR"
PIDFILE="\$BOT_DIR/.torrent-bot.pid"
LOGFILE="\$BOT_DIR/torrent-bot.log"

is_running() {
	[ -f "\$PIDFILE" ] && kill -0 "\$(cat "\$PIDFILE")" 2>/dev/null
}

start_bot() {
	if is_running; then
		echo "torrent-bot already running (pid \$(cat "\$PIDFILE"))"
		return 0
	fi
	cd "\$BOT_DIR" || exit 1
	PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8 \\
		nohup "\$BOT_DIR/.venv/bin/python" "\$BOT_DIR/bot.py" >> "\$LOGFILE" 2>&1 &
	echo \$! > "\$PIDFILE"
	echo "torrent-bot started (pid \$(cat "\$PIDFILE"))"
}

stop_bot() {
	if ! is_running; then
		echo "torrent-bot is not running"
		rm -f "\$PIDFILE"
		return 0
	fi
	kill "\$(cat "\$PIDFILE")" 2>/dev/null
	rm -f "\$PIDFILE"
	echo "torrent-bot stopped"
}

case "\$1" in
	start) start_bot ;;
	stop) stop_bot ;;
	restart) stop_bot; sleep 1; start_bot ;;
	status) is_running && echo "running" || echo "stopped" ;;
	*) start_bot ;;
esac
EOF

	chmod +x "$RCD_SCRIPT"
	log "rc.d script installed: $RCD_SCRIPT"
	return 0
}

AUTOSTART=""
if install_systemd; then
	AUTOSTART="systemd"
elif install_rcd; then
	AUTOSTART="rcd"
fi

# --- 5. Start -------------------------------------------------------------- #
TOKEN_SET=0
if grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$INSTALL_DIR/.env" 2>/dev/null; then
	TOKEN_SET=1
fi

case "$AUTOSTART" in
	systemd)
		if [ "$TOKEN_SET" = "1" ]; then
			log "Enabling and starting the service..."
			systemctl enable --now "$SERVICE_NAME"
			sleep 2
			systemctl --no-pager status "$SERVICE_NAME" || true
			log "Logs: journalctl -u $SERVICE_NAME -f   (or $INSTALL_DIR/torrent-bot.log)"
		else
			log "TELEGRAM_BOT_TOKEN is not set yet - not starting."
			log "1) nano $INSTALL_DIR/.env"
			log "2) systemctl enable --now $SERVICE_NAME"
		fi
	;;
	rcd)
		if [ "$TOKEN_SET" = "1" ]; then
			log "Starting the bot via rc.d..."
			"$RCD_DIR/S99${SERVICE_NAME}.sh" start
			log "Logs: tail -f $INSTALL_DIR/torrent-bot.log"
		else
			log "TELEGRAM_BOT_TOKEN is not set yet - not starting."
			log "1) nano $INSTALL_DIR/.env"
			log "2) $RCD_DIR/S99${SERVICE_NAME}.sh start"
		fi
	;;
	*)
		warn "No supported autostart mechanism found (no systemd, no $RCD_DIR)."
		warn "Use DSM Task Scheduler instead:"
		warn "  Control Panel -> Task Scheduler -> Create -> Triggered Task -> Boot-up"
		warn "  User: root"
		warn "  Command:"
		warn "    cd $INSTALL_DIR && ./.venv/bin/python bot.py >> $INSTALL_DIR/torrent-bot.log 2>&1"
	;;
esac
