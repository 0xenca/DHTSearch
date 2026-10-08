#!/usr/bin/env bash
# DHT Search — installer / updater (Debian, Ubuntu, Proxmox LXC; also Fedora/RHEL family).
#
#   sudo ./install.sh              interactive: asks every setting, Enter = default shown in [brackets]
#   sudo ./install.sh --yes        no questions: defaults (or the current settings when updating)
#   sudo ./install.sh --uninstall  removes the service and the code; the data directory is KEPT
#
# Any setting can also be given as an environment variable (used as the default, or as the value with --yes):
#   TS_DIR TS_DATA TS_USER TS_HOST TS_WEB_PORT TS_DHT_PORT TS_MAX_PROBES TS_ADMIN_PASSWORD TS_FIREWALL TS_BACKUP
#   TS_AI (y/n: local AI moderation model) TS_AI_PORT TS_AI_THREADS TS_AI_LLAMA (llama.cpp .tar.gz file or URL)
#
# What it does: installs the system packages, creates a service user and a Python virtualenv with the dependencies,
# copies the code, writes the systemd unit (listening on every network interface by default), stores the admin
# password outside the unit (/etc/torrent-search/env, mode 600), opens the firewall if one is active, runs the
# libtorrent self-test, starts the service and prints the addresses where it answers.
# Run it again to update: the current settings become the defaults and the data is kept (backup offered first).
set -euo pipefail

SERVICE=torrent-search
UNIT=/etc/systemd/system/$SERVICE.service
AI_SERVICE=torrent-search-ai
AI_UNIT=/etc/systemd/system/$AI_SERVICE.service
AI_DIR=/opt/torrent-search-ai
AI_MODEL_FILE=Qwen3Guard-Gen-0.6B.Q4_K_M.gguf
AI_MODEL_URL=https://huggingface.co/mradermacher/Qwen3Guard-Gen-0.6B-GGUF/resolve/main/$AI_MODEL_FILE
ENVDIR=/etc/torrent-search
ENVFILE=$ENVDIR/env
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=0
UNINSTALL=0

for a in "$@"; do
  case "$a" in
    -y|--yes) YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (see --help)"; exit 2 ;;
  esac
done

# ------------------------------------------------------------------ helpers
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B= G= Y= R= N=; fi
say()  { echo "${B}==>${N} $*"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
die()  { echo "${R}ERROR:${N} $*" >&2; exit 1; }

TTY=/dev/tty
if [ $YES -eq 0 ] && ! (exec < $TTY) 2>/dev/null; then YES=1; fi   # no terminal (pipe, CI): behave as --yes

ask() {        # ask VAR "question" default
  local var=$1 q=$2 def=$3 ans
  if [ $YES -eq 1 ]; then printf -v "$var" '%s' "$def"; echo "  $q: $def"; return; fi
  read -r -p "  $q [$def]: " ans < $TTY || true
  printf -v "$var" '%s' "${ans:-$def}"
}
ask_yn() {     # ask_yn "question" y|n  -> exit status 0 = yes
  local q=$1 def=$2 ans
  if [ $YES -eq 1 ]; then echo "  $q: $def"; [ "$def" = y ]; return; fi
  read -r -p "  $q [$( [ "$def" = y ] && echo Y/n || echo y/N )]: " ans < $TTY || true
  ans=${ans:-$def}
  [[ "$ans" =~ ^[YySs] ]]
}
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
port_busy() {  # port_busy PORT tcp|udp -> 0 if something else listens there
  command -v ss >/dev/null || return 1
  local flag=-ltnH; [ "$2" = udp ] && flag=-lunH
  ss $flag "sport = :$1" 2>/dev/null | grep -q .
}
unit_arg() {   # current value of --flag in the installed unit (for updates)
  [ -f $UNIT ] || return 0
  grep -E '^ExecStart=' $UNIT | grep -oE -- "--$1[ =][^ ]+" | head -1 | sed -E "s/--$1[ =]//" || true
}

[ "$(id -u)" -eq 0 ] || die "run it as root:  sudo $0 $*"
command -v systemctl >/dev/null && [ -d /run/systemd/system ] || die "systemd is required (this system does not run it)."

# ------------------------------------------------------------------ uninstall
if [ $UNINSTALL -eq 1 ]; then
  DIR=$(grep -E '^WorkingDirectory=' $UNIT 2>/dev/null | cut -d= -f2-); DIR=${DIR:-${TS_DIR:-/opt/torrentcrawler}}
  DATA=$(unit_arg data-dir); DATA=${DATA:-$DIR/data}
  say "Uninstalling $SERVICE (code in $DIR; the data in $DATA is KEPT)"
  ask_yn "Continue" n || exit 0
  systemctl disable --now $SERVICE 2>/dev/null || true
  rm -f $UNIT
  if [ -f $AI_UNIT ] && ask_yn "Also remove the local AI model server ($AI_DIR, ~0.5 GB)" y; then
    systemctl disable --now $AI_SERVICE 2>/dev/null || true
    rm -f $AI_UNIT; rm -rf "$AI_DIR"
  fi
  systemctl daemon-reload
  if [ -d "$DIR" ]; then
    find "$DIR" -mindepth 1 -maxdepth 1 ! -path "$DATA" ! -name 'data' ! -name 'data.bak*' -exec rm -rf {} +
  fi
  ok "service and code removed. Data left in $DATA; also remove $ENVFILE (admin password) if you no longer need it."
  exit 0
fi

echo
echo "${B}DHT Search installer${N}  (source: $SRC)"
[ -f "$SRC/app.py" ] && [ -f "$SRC/requirements.txt" ] || die "run install.sh from the project directory (app.py not found in $SRC)."
VERSION=$(sed -nE 's/^__version__ = "([^"]+)".*/\1/p' "$SRC/version.py")
UPDATE=0; [ -f $UNIT ] && UPDATE=1
[ $UPDATE -eq 1 ] && echo "An existing installation was found: its current settings are the defaults." || true
echo

# ------------------------------------------------------------------ settings
CUR_DIR=$(grep -E '^WorkingDirectory=' $UNIT 2>/dev/null | cut -d= -f2- || true)
CUR_USER=$(grep -E '^User=' $UNIT 2>/dev/null | cut -d= -f2- || true)
say "Settings (press Enter to keep the value in brackets)"
ask DIR        "Install directory" "${TS_DIR:-${CUR_DIR:-/opt/torrentcrawler}}"
DIR=$(realpath -m "$DIR")
DEF_DATA=${TS_DATA:-$(unit_arg data-dir)}
ask DATA       "Data directory (torrents, index, statistics)" "${DEF_DATA:-$DIR/data}"
DATA=$(realpath -m "$DATA")
ask SUSER      "System user that runs the service" "${TS_USER:-${CUR_USER:-torrentsearch}}"

echo
echo "  Web address. 0.0.0.0 = every IPv4 interface; :: = every IPv4 AND IPv6 interface;"
echo "  127.0.0.1 = this machine only (use it behind a reverse proxy such as nginx/Caddy)."
DEF_HOST=${TS_HOST:-$(unit_arg host || true)}; DEF_HOST=${DEF_HOST:-0.0.0.0}
while :; do
  ask HOST "Listen on" "$DEF_HOST"
  if [ "$HOST" = "::" ] && [ ! -f /proc/net/if_inet6 ]; then
    [ $YES -eq 1 ] && die "IPv6 is disabled on this system: use TS_HOST=0.0.0.0"
    warn "IPv6 is disabled on this system: use 0.0.0.0"; DEF_HOST=0.0.0.0; continue
  fi
  break
done
while :; do
  DEF=${TS_WEB_PORT:-$(unit_arg port || true)}
  ask WEB_PORT "Web port (TCP)" "${DEF:-8080}"
  valid_port "$WEB_PORT" || { [ $YES -eq 1 ] && die "invalid web port: $WEB_PORT"; warn "not a valid port (1-65535)"; continue; }
  if port_busy "$WEB_PORT" tcp && ! systemctl is-active -q $SERVICE; then warn "TCP $WEB_PORT is already in use by another program"; [ $YES -eq 1 ] && die "choose another TS_WEB_PORT"; continue; fi
  break
done
echo
echo "  DHT port: UDP port used to talk to the BitTorrent DHT (TCP on the same number is optional)."
echo "  Forward it on your router / Proxmox host to this machine for best results."
while :; do
  DEF=${TS_DHT_PORT:-$(unit_arg dht-port || true)}
  ask DHT_PORT "DHT port (UDP)" "${DEF:-6881}"
  valid_port "$DHT_PORT" || { [ $YES -eq 1 ] && die "invalid DHT port: $DHT_PORT"; warn "not a valid port (1-65535)"; continue; }
  [ "$DHT_PORT" != "$WEB_PORT" ] || { [ $YES -eq 1 ] && die "the DHT port must differ from the web port"; warn "must be different from the web port"; continue; }
  if port_busy "$DHT_PORT" udp && ! systemctl is-active -q $SERVICE; then warn "UDP $DHT_PORT is already in use by another program"; [ $YES -eq 1 ] && die "choose another TS_DHT_PORT"; continue; fi
  break
done
while :; do
  DEF=${TS_MAX_PROBES:-$(unit_arg max-probes || true)}
  ask MAX_PROBES "Simultaneous metadata downloads (more = more network load)" "${DEF:-150}"
  [[ "$MAX_PROBES" =~ ^[0-9]+$ ]] && [ "$MAX_PROBES" -ge 1 ] && break
  [ $YES -eq 1 ] && die "invalid TS_MAX_PROBES: $MAX_PROBES"
  warn "a positive number, please"
done

echo
OLD_PW=$(sed -nE 's/^TS_ADMIN_PASSWORD=//p' $ENVFILE 2>/dev/null | head -1 || true)
GENERATED_PW=0
if [ -n "${TS_ADMIN_PASSWORD:-}" ]; then
  ADMIN_PW=$TS_ADMIN_PASSWORD; ok "admin password taken from TS_ADMIN_PASSWORD"
elif [ $YES -eq 1 ]; then
  if [ -n "$OLD_PW" ]; then ADMIN_PW=$OLD_PW; ok "admin password: kept"
  else GENERATED_PW=1; ADMIN_PW=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20 || true); ok "admin password: generated (shown at the end)"; fi
else
  hint=$([ -n "$OLD_PW" ] && echo "Enter = keep the current one" || echo "Enter = generate a random one")
  read -r -s -p "  Admin panel password (Ctrl+Alt+A on the web) [$hint]: " ADMIN_PW < $TTY || true; echo
  if [ -z "$ADMIN_PW" ] && [ -n "$OLD_PW" ]; then ADMIN_PW=$OLD_PW
  elif [ -z "$ADMIN_PW" ]; then GENERATED_PW=1; ADMIN_PW=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20 || true)
  elif [ ${#ADMIN_PW} -lt 8 ]; then warn "short password: fine for testing, change it for a public server"; fi
fi

echo
echo "  Optional AI moderation: a small safety model (Qwen3Guard-Gen-0.6B via llama.cpp, ~0.5 GB download) classifies"
echo "  torrent names as NSFW/harmful; you switch it on and set the threshold in Admin. Costs ~0.7 GB RAM and up to"
echo "  1 CPU core while it analyses (low priority). Answer n if RAM is tight: it can run on another machine instead."
AI_DEF=${TS_AI:-$([ -f $AI_UNIT ] && echo y || echo n)}
INSTALL_AI=n
AI_PORT=8091; AI_THREADS=1
if ask_yn "Install the local AI moderation model" "$AI_DEF"; then
  INSTALL_AI=y
  CUR_AI_PORT=$(grep -oE -- '--port [0-9]+' $AI_UNIT 2>/dev/null | awk '{print $2}' || true)
  CUR_AI_T=$(grep -oE -- ' -t [0-9]+' $AI_UNIT 2>/dev/null | awk '{print $2}' || true)
  while :; do
    ask AI_PORT "AI model port (local only, 127.0.0.1)" "${TS_AI_PORT:-${CUR_AI_PORT:-8091}}"
    valid_port "$AI_PORT" && [ "$AI_PORT" != "$WEB_PORT" ] && [ "$AI_PORT" != "$DHT_PORT" ] && break
    [ $YES -eq 1 ] && die "invalid TS_AI_PORT: $AI_PORT"; warn "a free port different from the web and DHT ports"
  done
  while :; do
    ask AI_THREADS "CPU threads for the model" "${TS_AI_THREADS:-${CUR_AI_T:-1}}"
    [[ "$AI_THREADS" =~ ^[0-9]+$ ]] && [ "$AI_THREADS" -ge 1 ] && break
    [ $YES -eq 1 ] && die "invalid TS_AI_THREADS"; warn "a positive number"
  done
fi

FW=none
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then FW=ufw
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then FW=firewalld; fi
OPEN_FW=n
if [ $FW != none ]; then
  ask_yn "Open TCP $WEB_PORT and UDP+TCP $DHT_PORT in the firewall ($FW)" "${TS_FIREWALL:-y}" && OPEN_FW=y
fi

DO_BACKUP=n
if [ -d "$DATA" ] && [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
  used=$(du -sm "$DATA" | cut -f1); free=$(df -Pm "$(dirname "$DATA")" | awk 'NR==2{print $4}')
  if [ "$used" -lt "$free" ]; then
    ask_yn "Back up the existing data ($used MB) before updating" "${TS_BACKUP:-y}" && DO_BACKUP=y
  else
    warn "existing data ($used MB) does not fit twice on the disk ($free MB free): no backup will be made"
  fi
fi

echo
say "Summary"
echo "  version $VERSION -> $DIR  (data: $DATA, user: $SUSER)"
echo "  web http://$HOST:$WEB_PORT   DHT UDP/TCP $DHT_PORT   $MAX_PROBES simultaneous downloads   firewall: $([ $OPEN_FW = y ] && echo "open in $FW" || echo unchanged)"
if [ $INSTALL_AI = y ]; then echo "  AI moderation model: llama.cpp + Qwen3Guard-Gen-0.6B on 127.0.0.1:$AI_PORT ($AI_THREADS thread(s); off until enabled in Admin)"; fi
[ $YES -eq 1 ] || ask_yn "Install with these settings" y || { echo "Cancelled: nothing was changed."; exit 0; }

# ------------------------------------------------------------------ system packages
say "System packages"
PY_PKGS=""
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl rsync >/dev/null
  apt-get install -y -qq python3-libtorrent >/dev/null 2>&1 && PY_PKGS="python3-libtorrent" || warn "python3-libtorrent is not in the distribution: pip will be used"
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pip curl rsync >/dev/null
  dnf install -y -q python3-libtorrent-rasterbar >/dev/null 2>&1 || dnf install -y -q rb_libtorrent-python3 >/dev/null 2>&1 \
    && PY_PKGS="libtorrent (dnf)" || warn "libtorrent python bindings not packaged: pip will be used"
else
  die "unsupported package manager: install python3 (>= 3.9), python3-venv and libtorrent's Python bindings, then run this again."
fi
python3 - <<'EOF' || die "Python 3.9 or newer is required"
import sys; sys.exit(sys.version_info < (3, 9))
EOF
ok "python $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))') ${PY_PKGS:+, $PY_PKGS}"

# ------------------------------------------------------------------ user, directories, code
say "User and files"
if ! id "$SUSER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DIR" --no-create-home --shell /usr/sbin/nologin "$SUSER" 2>/dev/null \
    || useradd -r -d "$DIR" -M -s /sbin/nologin "$SUSER"
  ok "user $SUSER created"
fi
WAS_ACTIVE=0
if systemctl is-active -q $SERVICE; then
  WAS_ACTIVE=1; echo "  stopping the service (it writes its checkpoint; may take up to a minute)…"
  systemctl stop $SERVICE
fi
if [ $DO_BACKUP = y ]; then
  BK="${DATA%/}.bak-$(date +%Y%m%d-%H%M%S)"
  cp -a --reflink=auto "$DATA" "$BK"
  ok "data backed up to $BK"
fi
mkdir -p "$DIR" "$DATA"
if [ "$(realpath "$SRC")" != "$(realpath "$DIR")" ]; then
  KEEP=(); case "$DATA/" in "$DIR"/*) KEEP=(--exclude "/${DATA#"$DIR"/}/");; esac    # never touch a data dir inside DIR
  rsync -a --delete "${KEEP[@]}" \
    --exclude 'data/' --exclude 'data-demo/' --exclude 'data.bak*' --exclude '.venv/' --exclude 'venv/' \
    --exclude '__pycache__/' --exclude '*.pyc' --exclude '.git/' \
    "$SRC/" "$DIR/"
  ok "code copied to $DIR"
else
  ok "installing in place ($DIR)"
fi
# code: owned by root, read-only for the service user (the data dir is skipped even if it lives inside DIR)
find "$DIR" -path "$DATA" -prune -o \( ! -user root -o ! -group root \) -exec chown root:root {} + 2>/dev/null || true
find "$DIR" -path "$DATA" -prune -o -perm /022 -exec chmod go-w {} + 2>/dev/null || true
find "$DATA" \( ! -user "$SUSER" -o ! -group "$SUSER" \) -exec chown "$SUSER:$SUSER" {} + 2>/dev/null || true
chown "$SUSER:$SUSER" "$DATA"; chmod 750 "$DATA"

# ------------------------------------------------------------------ virtualenv + dependencies
say "Python environment"
VENV="$DIR/.venv"
[ -x "$DIR/venv/bin/python" ] && [ ! -x "$VENV/bin/python" ] && VENV="$DIR/venv"    # keep an older layout working
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv --system-site-packages "$VENV"      # system-site-packages: sees python3-libtorrent from apt
fi
"$VENV/bin/pip" install -q --upgrade pip >/dev/null
"$VENV/bin/pip" install -q -r <(grep -viE '^\s*libtorrent' "$DIR/requirements.txt")
LT_OK=$("$VENV/bin/python" - <<'EOF' 2>/dev/null || echo none
try:
    import libtorrent as lt
    v = tuple(int(x) for x in lt.__version__.split(".")[:3])
    print("ok" if v >= (2, 0, 9) else "old " + lt.__version__)
except Exception:
    print("none")
EOF
)
if [ "$LT_OK" != ok ]; then
  echo "  libtorrent from the distribution: ${LT_OK#old }; installing a current one with pip…"
  "$VENV/bin/pip" install -q "libtorrent>=2.0.9" || warn "pip could not install libtorrent (no wheel for this Python/CPU?)"
fi
"$VENV/bin/python" "$DIR/app.py" --selftest >/tmp/ts-selftest.log 2>&1 \
  && ok "libtorrent self-test passed ($(grep -oE 'libtorrent [0-9.]+' /tmp/ts-selftest.log | head -1))" \
  || { cat /tmp/ts-selftest.log; die "libtorrent self-test failed (see above). The web alone can be tried with: $VENV/bin/python $DIR/app.py --demo"; }
ok "dependencies installed in $VENV"

# ------------------------------------------------------------------ admin password + systemd unit
say "Service"
install -d -m 700 $ENVDIR
umask 077
printf 'TS_ADMIN_PASSWORD=%s\n' "$ADMIN_PW" > $ENVFILE
chmod 600 $ENVFILE
umask 022
CAPS=""
[ "$WEB_PORT" -lt 1024 ] || [ "$DHT_PORT" -lt 1024 ] && CAPS=$'AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE'
cat > $UNIT <<EOF
# Written by install.sh (DHT Search $VERSION). Run install.sh again to change the settings.
[Unit]
Description=DHT Search (BitTorrent DHT crawler + web search)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SUSER
Group=$SUSER
WorkingDirectory=$DIR
ExecStart=$VENV/bin/python $DIR/app.py --host $HOST --port $WEB_PORT --dht-port $DHT_PORT --max-probes $MAX_PROBES --data-dir $DATA
# Admin password (not visible in ps or in this file)
EnvironmentFile=-$ENVFILE
Environment=PYTHONUNBUFFERED=1
# Fewer glibc malloc arenas: without it RSS grows by hundreds of MB that the program does not use
Environment=MALLOC_ARENA_MAX=2
Restart=on-failure
RestartSec=5
# On stop the service writes its checkpoint and the index delta; if it is killed anyway nothing is lost
TimeoutStopSec=120
KillSignal=SIGTERM
LimitNOFILE=65536
$CAPS
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA
ProtectKernelTunables=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
ok "unit written: $UNIT"

if [ $OPEN_FW = y ]; then
  if [ $FW = ufw ]; then
    ufw allow "$WEB_PORT/tcp" >/dev/null; ufw allow "$DHT_PORT/udp" >/dev/null; ufw allow "$DHT_PORT/tcp" >/dev/null
  else
    firewall-cmd -q --permanent --add-port="$WEB_PORT/tcp" --add-port="$DHT_PORT/udp" --add-port="$DHT_PORT/tcp"; firewall-cmd -q --reload
  fi
  ok "firewall: TCP $WEB_PORT, UDP/TCP $DHT_PORT open"
fi

# ------------------------------------------------------------------ optional: local AI moderation model (llama.cpp)
AI_OK=n
if [ $INSTALL_AI = y ]; then
  say "AI moderation model"
  if command -v apt-get >/dev/null; then
    apt-get install -y -qq unzip libgomp1 >/dev/null || true
    apt-get install -y -qq libssl3 >/dev/null 2>&1 || apt-get install -y -qq libssl3t64 >/dev/null 2>&1 || true   # llama-server links OpenSSL
  elif command -v dnf >/dev/null; then dnf install -y -q unzip libgomp openssl-libs >/dev/null || true; fi
  case "$(uname -m)" in x86_64|amd64) LARCH=x64 ;; aarch64|arm64) LARCH=arm64 ;; *) LARCH= ;; esac
  mkdir -p "$AI_DIR/models"
  BIN=$(find "$AI_DIR/current" -name llama-server -type f 2>/dev/null | head -1 || true)
  PKG_SRC=${TS_AI_LLAMA:-}                               # optional: a llama.cpp .tar.gz/.zip file or URL given by hand
  if [ -z "$BIN" ] && { [ -n "$LARCH" ] || [ -n "$PKG_SRC" ]; }; then
    URL=""; LOCAL=""
    if [ -n "$PKG_SRC" ] && [ -f "$PKG_SRC" ]; then LOCAL=$PKG_SRC
    elif [ -n "$PKG_SRC" ]; then URL=$PKG_SRC
    else
      # llama.cpp release asset: llama-<tag>-bin-ubuntu-<arch>.tar.gz (older releases: .zip). Found through the GitHub
      # API, or (API refused / rate-limited) through the /releases/latest redirect that names the tag.
      API_OUT=$(mktemp)
      API_CODE=$(curl -sS -m 30 -o "$API_OUT" -w '%{http_code}' https://api.github.com/repos/ggml-org/llama.cpp/releases/latest 2>"$API_OUT.err" || true)
      if [ "$API_CODE" = 200 ]; then
        URL=$(LARCH=$LARCH python3 -c "
import json, os, re, sys
d = json.load(open(sys.argv[1]))
for a in d.get('assets', []):
    if re.fullmatch(r'llama-b\d+-bin-ubuntu-' + os.environ['LARCH'] + r'\.(tar\.gz|zip)', a['name']):
        print(a['browser_download_url']); break" "$API_OUT" 2>/dev/null || true)
        [ -n "$URL" ] || warn "the latest llama.cpp release has no ubuntu-$LARCH package (yet)"
      else
        warn "GitHub API: HTTP ${API_CODE:-000} $(head -c 200 "$API_OUT.err" 2>/dev/null)$(grep -o '"message": *"[^"]*"' "$API_OUT" 2>/dev/null | head -1)"
      fi
      rm -f "$API_OUT" "$API_OUT.err"
      if [ -z "$URL" ]; then
        HDRS=$(curl -sS -m 30 -o /dev/null -D - https://github.com/ggml-org/llama.cpp/releases/latest 2>&1 || true)
        TAG=$(printf '%s\n' "$HDRS" | tr -d '\r' | sed -nE 's#^[Ll]ocation: .*/releases/tag/(b[0-9]+).*#\1#p' | head -1)
        if [ -n "$TAG" ]; then URL="https://github.com/ggml-org/llama.cpp/releases/download/$TAG/llama-$TAG-bin-ubuntu-$LARCH.tar.gz"
        else warn "github.com: $(printf '%s\n' "$HDRS" | tr -d '\r' | head -1)"; fi
      fi
    fi
    TMPZ=$(mktemp -d)
    if [ -n "$LOCAL" ]; then cp "$LOCAL" "$TMPZ/pkg"; SRCNAME=$LOCAL
    elif [ -n "$URL" ]; then
      echo "  downloading llama.cpp ($(basename "$URL"))…"
      curl -fsSL -m 900 -o "$TMPZ/pkg" "$URL" || { warn "download failed: $URL"; rm -f "$TMPZ/pkg"; }
      SRCNAME=$URL
    fi
    if [ -s "$TMPZ/pkg" ]; then
      mkdir -p "$TMPZ/x"
      case "$SRCNAME" in
        *.zip) unzip -q "$TMPZ/pkg" -d "$TMPZ/x" || true ;;
        *)     tar -xzf "$TMPZ/pkg" -C "$TMPZ/x" || true ;;
      esac
      if [ -n "$(find "$TMPZ/x" -name llama-server -type f | head -1)" ]; then
        rm -rf "$AI_DIR/current"; mkdir -p "$AI_DIR/current"
        cp -a "$TMPZ/x/." "$AI_DIR/current/"
        BIN=$(find "$AI_DIR/current" -name llama-server -type f | head -1 || true)
      else
        warn "$(basename "$SRCNAME") does not contain llama-server"
      fi
    fi
    rm -rf "$TMPZ"
  fi
  if [ -z "$BIN" ]; then
    warn "llama.cpp could not be installed ($(uname -m)). Download llama-bNNNN-bin-ubuntu-x64.tar.gz from"
    warn "https://github.com/ggml-org/llama.cpp/releases on any machine, copy it here and run:"
    warn "  sudo env TS_AI=y TS_AI_LLAMA=/path/to/llama-bNNNN-bin-ubuntu-x64.tar.gz ./install.sh --yes"
  else
    chmod +x "$BIN"
    ok "llama-server: $BIN"
  fi
  MODEL="$AI_DIR/models/$AI_MODEL_FILE"
  if [ ! -s "$MODEL" ] || [ "$(stat -c %s "$MODEL")" -lt 300000000 ]; then
    echo "  downloading the model ($AI_MODEL_FILE, ~480 MB)…"
    if curl -fL --retry 3 -m 3600 -o "$MODEL.part" "$AI_MODEL_URL"; then mv "$MODEL.part" "$MODEL"
    else rm -f "$MODEL.part"; warn "model download failed: run the installer again later"; fi
  fi
  if [ -s "$MODEL" ]; then ok "model: $MODEL"; fi
  chown -R root:root "$AI_DIR"; chmod -R go-w "$AI_DIR"
  if [ -n "$BIN" ] && [ -s "$MODEL" ]; then
    {
      echo "# Written by install.sh: local safety model for DHT Search's AI moderation (Admin -> AI moderation)."
      echo "[Unit]"
      echo "Description=DHT Search AI moderation model (llama.cpp + Qwen3Guard-Gen-0.6B)"
      echo "After=network.target"
      echo
      echo "[Service]"
      echo "Type=simple"
      echo "User=$SUSER"
      echo "Group=$SUSER"
      echo "Environment=LD_LIBRARY_PATH=$(dirname "$BIN")"
      echo "ExecStart=$BIN -m $MODEL --host 127.0.0.1 --port $AI_PORT -c 1536 -t $AI_THREADS --parallel 1"
      echo "# Low priority: the crawler and the web come first"
      echo "Nice=15"
      echo "CPUWeight=20"
      echo "IOSchedulingClass=idle"
      echo "MemoryMax=1500M"
      echo "Restart=on-failure"
      echo "RestartSec=10"
      echo "NoNewPrivileges=true"
      echo "PrivateTmp=true"
      echo "ProtectSystem=strict"
      echo "ProtectHome=true"
      echo "ProtectKernelTunables=true"
      echo "ProtectControlGroups=true"
      echo
      echo "[Install]"
      echo "WantedBy=multi-user.target"
    } > $AI_UNIT
    systemctl daemon-reload
    systemctl enable -q $AI_SERVICE
    systemctl restart $AI_SERVICE
    for i in $(seq 1 60); do
      if curl -fsS -m 2 "http://127.0.0.1:$AI_PORT/health" >/dev/null 2>&1; then AI_OK=y; break; fi
      systemctl is-active -q $AI_SERVICE || break
      sleep 2
    done
    if [ $AI_OK = y ]; then ok "model server answering on 127.0.0.1:$AI_PORT"
    else journalctl -u $AI_SERVICE -n 20 --no-pager || true; warn "the model server did not start (log above); the search engine works without it"; fi
    # point the app at it (unless the admin configured an endpoint on another machine)
    AIJ="$DATA/ai.json"
    python3 -c '
import json, os, sys
p, port = sys.argv[1], sys.argv[2]
try:
    c = json.load(open(p))
except (OSError, ValueError):
    c = {}
if not c.get("endpoint") or c["endpoint"].startswith("http://127.0.0.1:"):
    c["endpoint"] = "http://127.0.0.1:" + port
    c.setdefault("profile", "qwen3guard")
    with open(p + ".tmp", "w") as f:
        json.dump(c, f)
    os.replace(p + ".tmp", p)
' "$AIJ" "$AI_PORT"
    chown "$SUSER:$SUSER" "$AIJ"; chmod 600 "$AIJ"
  fi
fi

# ------------------------------------------------------------------ start and check
say "Starting"
systemctl enable -q $SERVICE
systemctl restart $SERVICE
CHECK_HOST=127.0.0.1; [ "$HOST" = "::" ] && CHECK_HOST="[::1]"; [[ "$HOST" =~ ^[0-9.]+$ ]] && [ "$HOST" != 0.0.0.0 ] && CHECK_HOST=$HOST
up=0
for i in $(seq 1 90); do
  if curl -fsS -m 2 "http://$CHECK_HOST:$WEB_PORT/api/version" >/dev/null 2>&1; then up=1; break; fi
  systemctl is-active -q $SERVICE || break
  sleep 2
done
if [ $up -eq 1 ]; then
  ok "the web answers"
elif systemctl is-active -q $SERVICE; then
  warn "running, but the web does not answer yet: the first start after updating converts the data and can take a"
  warn "while with many torrents. Follow it with:  journalctl -u $SERVICE -f"
else
  journalctl -u $SERVICE -n 30 --no-pager || true
  die "the service did not start (log above)."
fi

echo
echo "${B}DHT Search $VERSION is installed.${N}"
case "$HOST" in
  0.0.0.0|::)
    for ip in $(hostname -I 2>/dev/null); do
      [[ "$ip" == *:* ]] && { [ "$HOST" = "::" ] && [[ "$ip" != fe80* ]] && echo "  http://[$ip]:$WEB_PORT"; continue; }
      echo "  http://$ip:$WEB_PORT"
    done ;;
  *) echo "  http://$HOST:$WEB_PORT" ;;
esac
echo "  Admin panel: press Ctrl+Alt+A on the web."
if [ $AI_OK = y ]; then echo "  AI moderation: installed and OFF. Switch it on in Admin -> AI moderation (test a few names first)."; fi
[ $GENERATED_PW -eq 1 ] && echo "  Admin password: ${B}$ADMIN_PW${N}   (saved in $ENVFILE, readable by root only)"
echo "  Logs:    journalctl -u $SERVICE -f"
echo "  Config:  run this installer again (current settings are kept as defaults); uninstall: $0 --uninstall"
if [ "$HOST" != 127.0.0.1 ] && [ "$HOST" != "::1" ]; then
  echo
  warn "The web is reachable from the network over plain HTTP: the admin password travels unencrypted."
  warn "For a public server put a reverse proxy with HTTPS in front (and then use 127.0.0.1 here)."
fi
command -v systemd-detect-virt >/dev/null && [ "$(systemd-detect-virt 2>/dev/null)" = lxc ] && \
  warn "LXC container: forward UDP $DHT_PORT (and TCP $WEB_PORT) from the Proxmox host / router to this container."
exit 0
