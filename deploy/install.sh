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
#      NONINTERACTIVE=1                   skip the interactive .env setup
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
if ! "$VENV_PY" -c 'import socks' 2>/dev/null; then
	err "PySocks is missing - socks5h:// will not work!"
	exit 1
fi
log "Dependencies import cleanly; PySocks present (socks5h:// supported)."

# --- 3. .env file --------------------------------------------------------- #
ENV_FILE="$INSTALL_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
	cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
	log ".env created from .env.example"
else
	log ".env already exists - it will be updated in place"
fi

# --- 4. Interactive configuration ----------------------------------------- #
current_value() {
	grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-
}

# Value of KEY, or a fallback when it is empty or missing in .env.
# Defaults must not depend on the current file contents.
default_or() {
	_v="$(current_value "$1")"
	[ -n "$_v" ] || _v="$2"
	printf '%s' "$_v"
}

# Parent directory of a path (to guess the download base dir).
parent_of() {
	case "$1" in
		*/?*) printf '%s' "${1%/*}" ;;
		*) printf '%s' "$1" ;;
	esac
}

# Configured = a real bot token plus a PROXY_URL that is not localhost
# (localhost would mean "on this NAS", which is always wrong for the proxy).
is_configured() {
	_tok="$(current_value TELEGRAM_BOT_TOKEN)"
	case "$_tok" in ""|123456:*) return 1 ;; esac
	_url="$(current_value PROXY_URL)"
	case "$_url" in ""|*127.0.0.1*|*localhost*) return 1 ;; esac
	return 0
}

set_env() {
	# Robust in-place update. Values may contain / : + = and even non-UTF-8
	# bytes: on DSM the locale is usually "C", so Python decodes argv with
	# surrogateescape and a stray high byte becomes a lone surrogate.
	# PYTHONUTF8=1 + surrogateescape round-trips whatever came in.
	PYTHONUTF8=1 "$VENV_PY" - "$ENV_FILE" "$1" "$2" <<'PY'
import pathlib, sys

path = pathlib.Path(sys.argv[1])
key, value = sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8", errors="surrogateescape")
out, found = [], False
for line in text.splitlines():
    if line.startswith(key + "="):
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8", errors="surrogateescape")
PY
}

mask() {
	[ -n "$1" ] || { echo "(empty)"; return; }
	printf '%s****' "$(printf '%s' "$1" | cut -c1-4)"
}

ANSWER=""
ask() {
	_prompt=$1
	_default=$2
	if [ -n "$_default" ]; then
		printf '%s [%s]: ' "$_prompt" "$_default"
	else
		printf '%s: ' "$_prompt"
	fi
	read -r ANSWER || ANSWER=""
	[ -n "$ANSWER" ] || ANSWER="$_default"
}

# Like ask(), but keeps asking until the answer matches an ERE pattern.
# Guards against non-ASCII bytes sneaking into IPs/ports/tokens when the
# terminal locale is not UTF-8.
ask_valid() {
	_prompt=$1
	_default=$2
	_pattern=$3
	_errmsg=$4
	while :; do
		ask "$_prompt" "$_default"
		# NOTE: the trailing newline matters - without it an empty answer
		# gives grep zero lines, so a pattern that allows empty ("*") would
		# never match.
		if printf '%s\n' "$ANSWER" | grep -Eq "$_pattern"; then
			return 0
		fi
		printf '  ! %s\n' "$_errmsg"
	done
}

# Like ask(), but requires a valid TCP port (1..65535).
ask_port() {
	_prompt=$1
	_default=$2
	while :; do
		ask "$_prompt" "$_default"
		if printf '%s\n' "$ANSWER" | grep -Eq '^[0-9]{1,5}$' \
			&& [ "$ANSWER" -ge 1 ] && [ "$ANSWER" -le 65535 ]; then
			return 0
		fi
		printf '  ! enter a port between 1 and 65535\n'
	done
}

is_placeholder() {
	case "$1" in
		""|123456:*|you@example.com) return 0 ;;
		*) return 1 ;;
	esac
}

INTERACTIVE=1
if [ "${NONINTERACTIVE:-0}" = "1" ] || [ ! -t 0 ]; then
	INTERACTIVE=0
fi

configure_env() {
	cur_token="$(current_value TELEGRAM_BOT_TOKEN)"
	if is_placeholder "$cur_token"; then cur_token=""; fi

	if is_configured; then
		printf 'Existing .env looks configured. Re-run the setup to review values? [y/N]: '
		read -r _again || _again=""
		case "$_again" in
			[Yy]*) ;;
			*) log "Keeping the existing .env unchanged."; return 0 ;;
		esac
	else
		log "Some required values are still missing - let's fill them in."
	fi

	echo
	log "Interactive setup. Press Enter to keep the value in brackets."
	echo

	# --- Telegram ---
	echo "-- Telegram --"
	ask_valid "  Bot token from @BotFather (123456789:AA... ; Enter to skip)" \
		"$cur_token" '^([0-9]{6,}:[A-Za-z0-9_-]{20,})?$' \
		"a token looks like 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
	if [ -n "$ANSWER" ]; then
		set_env TELEGRAM_BOT_TOKEN "$ANSWER"
	fi

	ask_valid "  Your Telegram user id(s), comma separated (empty = anyone)" \
		"$(current_value ALLOWED_USER_IDS)" '^[0-9, ]*$' \
		"digits and commas only, e.g. 111111111,222222222"
	set_env ALLOWED_USER_IDS "$ANSWER"

	# --- Proxy to trackers ---
	echo
	echo "-- Proxy on OpenWrt (ONLY tracker traffic goes here) --"
	echo "   Transmission is NOT affected: it always stays direct."
	cur_url="$(current_value PROXY_URL)"
	cur_scheme="socks5h"; cur_host=""; cur_port=""
	case "$cur_url" in
		*"://"*)
			cur_scheme="${cur_url%%://*}"
			_rest="${cur_url#*://}"
			case "$_rest" in
				*:*) cur_host="${_rest%%:*}"; cur_port="${_rest##*:}" ;;
				*) cur_host="$_rest" ;;
			esac
			;;
	esac

	ask_valid "  Scheme (socks5h recommended)" "${cur_scheme:-socks5h}" \
		'^(socks5h|socks5|http|https)$' "use socks5h, socks5, http or https"
	PROXY_SCHEME="$ANSWER"
	ask_valid "  Address of the OpenWrt box (IP or hostname)" "$cur_host" \
		'^[A-Za-z0-9._-]+$' "ASCII only, e.g. 192.168.1.2 or 172.50.16.24"
	PROXY_HOST="$ANSWER"
	ask_port "  Port (our xray-socks-lan = 1080, v2rayA SOCKS5 usually 20170)" \
		"${cur_port:-1080}"
	PROXY_PORT="$ANSWER"
	if [ -n "$PROXY_HOST" ]; then
		set_env PROXY_URL "${PROXY_SCHEME}://${PROXY_HOST}:${PROXY_PORT}"
	fi

	# --- Transmission RPC (no proxy) ---
	echo
	echo "-- Transmission RPC (always WITHOUT proxy) --"
	echo "   Transmission runs on this NAS, so Host is normally 127.0.0.1."
	ask_valid "  Host (127.0.0.1 = this NAS)" \
		"$(default_or TRANSMISSION_HOST 127.0.0.1)" \
		'^[A-Za-z0-9._-]+$' "ASCII only, e.g. 127.0.0.1 or the NAS IP"
	set_env TRANSMISSION_HOST "$ANSWER"
	ask_port "  Port (Transmission RPC port, default 9091)" \
		"$(default_or TRANSMISSION_PORT 9091)"
	set_env TRANSMISSION_PORT "$ANSWER"
	ask "  RPC username (plain text; Enter if RPC auth is OFF)" \
		"$(current_value TRANSMISSION_USER)"
	set_env TRANSMISSION_USER "$ANSWER"
	ask "  RPC password (plain text, NOT the {hash} in settings.json)" \
		"$(current_value TRANSMISSION_PASSWORD)"
	set_env TRANSMISSION_PASSWORD "$ANSWER"

	# --- Download dirs ---
	echo
	echo "-- Download folders (Plex) --"
	echo "   Six folders (Movies/Series/Anime/Audiobooks/Music/Soft) go under it."
	ask_valid "  Base directory" \
		"$(parent_of "$(default_or DOWNLOAD_DIR_MOVIES /volume2/downloads2/Movies)")" \
		'^/[A-Za-z0-9._/-]*$' "absolute path, ASCII only, e.g. /volume2/downloads2"
	BASE_DIR_DL="$ANSWER"
	set_env DOWNLOAD_DIR_MOVIES "$BASE_DIR_DL/Movies"
	set_env DOWNLOAD_DIR_SERIES "$BASE_DIR_DL/Series"
	set_env DOWNLOAD_DIR_ANIME "$BASE_DIR_DL/Anime"
	set_env DOWNLOAD_DIR_AUDIOBOOKS "$BASE_DIR_DL/Audiobooks"
	set_env DOWNLOAD_DIR_MUSIC "$BASE_DIR_DL/Music"
	set_env DOWNLOAD_DIR_SOFT "$BASE_DIR_DL/Soft"
	set_env DOWNLOAD_DIR_DEFAULT "$BASE_DIR_DL/Movies"

	# --- Trackers ---
	echo
	echo "-- Tracker accounts (for /login_rutracker and /login_kinozal) --"
	echo "   Optional: leave empty to fill later; input is visible on screen."
	ask "  rutracker login (Enter to skip)" "$(current_value RUTRACKER_LOGIN)"
	set_env RUTRACKER_LOGIN "$ANSWER"
	ask "  rutracker password" "$(current_value RUTRACKER_PASSWORD)"
	set_env RUTRACKER_PASSWORD "$ANSWER"
	ask "  kinozal login (empty to skip)" "$(current_value KINOZAL_LOGIN)"
	set_env KINOZAL_LOGIN "$ANSWER"
	ask "  kinozal password" "$(current_value KINOZAL_PASSWORD)"
	set_env KINOZAL_PASSWORD "$ANSWER"

	# --- Summary ---
	echo
	log "Configuration written to $ENV_FILE"
	echo "    TELEGRAM_BOT_TOKEN : $(mask "$(current_value TELEGRAM_BOT_TOKEN)")"
	echo "    ALLOWED_USER_IDS   : $(current_value ALLOWED_USER_IDS)"
	echo "    PROXY_URL          : $(current_value PROXY_URL)"
	echo "    TRANSMISSION       : $(current_value TRANSMISSION_HOST):$(current_value TRANSMISSION_PORT)"
	echo "    DOWNLOAD_DIR_MOVIES: $(current_value DOWNLOAD_DIR_MOVIES)"
	echo "    RUTRACKER_LOGIN    : $(current_value RUTRACKER_LOGIN)"
	echo "    KINOZAL_LOGIN      : $(current_value KINOZAL_LOGIN)"
	echo
}

if [ "$INTERACTIVE" = "1" ]; then
	configure_env
else
	warn "Non-interactive mode: skipping the .env setup."
	warn "Edit it manually: nano $ENV_FILE"
fi

# --- 5. Autostart ---------------------------------------------------------- #
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

# --- 6. Start -------------------------------------------------------------- #
TOKEN_SET=0
if grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$ENV_FILE" 2>/dev/null; then
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
			log "1) nano $ENV_FILE"
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
			log "1) nano $ENV_FILE"
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
