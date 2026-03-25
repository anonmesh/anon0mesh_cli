#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  anon0mesh — Setup Script
#  Mesh First, Chain When It Matters
#
#  Sets up everything needed to run beacon.py and client.py:
#    · Python venv + all dependencies
#    · Reticulum config with working public hubs
#    · Meshtastic interface file (optional)
#    · Launcher scripts  (run_beacon.sh / run_client.sh)
#    · systemd service   (optional, beacon only)
#
#  Usage:
#    chmod +x setup.sh
#    ./setup.sh              # interactive, asks questions
#    ./setup.sh --beacon     # beacon-only, non-interactive
#    ./setup.sh --client     # client-only, non-interactive
#    ./setup.sh --both       # both, non-interactive
#    ./setup.sh --systemd    # also install beacon as systemd service
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
R="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
CYAN="\033[36m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"

log_info()  { echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${CYAN}ℹ  $*${R}"; }
log_ok()    { echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${GREEN}✔  $*${R}"; }
log_warn()  { echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${YELLOW}⚠  $*${R}"; }
log_err()   { echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${RED}✘  $*${R}"; }
log_step()  { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${R}\n"; }
log_banner(){ echo -e "
${BOLD}${CYAN}
  █████╗ ███╗  ██╗ ██████╗ ███╗  ██╗ ██████╗     ███╗  ███╗███████╗███████╗██╗  ██╗
 ██╔══██╗████╗ ██║██╔═══██╗████╗ ██║██╔═══██╗    ████╗████║██╔════╝██╔════╝██║  ██║
 ███████║██╔██╗██║██║   ██║██╔██╗██║██║   ██║    ██╔████╔██║█████╗  ███████╗███████║
 ██╔══██║██║╚████║██║   ██║██║╚████║██║   ██║    ██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║
 ██║  ██║██║ ╚███║╚██████╔╝██║ ╚███║╚██████╔╝    ██║ ╚═╝ ██║███████╗███████║██║  ██║
 ╚═╝  ╚═╝╚═╝  ╚══╝ ╚═════╝ ╚═╝  ╚══╝ ╚═════╝    ╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝
${R}${DIM}              a n o n 0 m e s h  ·  Mesh First, Chain When It Matters${R}
${BOLD}                              Setup Script  ·  Powered by Reticulum${R}
"; }

# ── Defaults ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
RNS_CONFIG_DIR="$HOME/.reticulum"
RNS_CONFIG_FILE="$RNS_CONFIG_DIR/config"
INTERFACES_DIR="$RNS_CONFIG_DIR/interfaces"

INSTALL_BEACON=false
INSTALL_CLIENT=false
INSTALL_SYSTEMD=false
INSTALL_BLE=false
INSTALL_MESHTASTIC=false
SOLANA_NETWORK="devnet"
NONINTERACTIVE=false

# ── Argument parsing ──────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --beacon)    INSTALL_BEACON=true;  NONINTERACTIVE=true ;;
    --client)    INSTALL_CLIENT=true;  NONINTERACTIVE=true ;;
    --both)      INSTALL_BEACON=true;  INSTALL_CLIENT=true; NONINTERACTIVE=true ;;
    --systemd)   INSTALL_SYSTEMD=true ;;
    --ble)       INSTALL_BLE=true ;;
    --meshtastic) INSTALL_MESHTASTIC=true ;;
    --mainnet)   SOLANA_NETWORK="mainnet" ;;
    --devnet)    SOLANA_NETWORK="devnet" ;;
    --help|-h)
      echo "Usage: $0 [--beacon] [--client] [--both] [--systemd] [--ble] [--meshtastic] [--mainnet|--devnet]"
      exit 0 ;;
  esac
done

log_banner

# ── Interactive mode ──────────────────────────────────────────────────────────
if [[ "$NONINTERACTIVE" == false ]]; then
  echo -e "${BOLD}What do you want to set up?${R}"
  echo "  1) Beacon only   (RPC gateway — needs internet)"
  echo "  2) Client only   (off-grid wallet — no internet needed)"
  echo "  3) Both"
  read -rp "Choice [1/2/3]: " choice
  case $choice in
    1) INSTALL_BEACON=true ;;
    2) INSTALL_CLIENT=true ;;
    3) INSTALL_BEACON=true; INSTALL_CLIENT=true ;;
    *) log_err "Invalid choice"; exit 1 ;;
  esac

  echo ""
  echo -e "${BOLD}Solana network?${R}"
  echo "  1) devnet  (safe for testing, free)"
  echo "  2) mainnet"
  read -rp "Choice [1/2, default=1]: " net_choice
  [[ "$net_choice" == "2" ]] && SOLANA_NETWORK="mainnet"

  echo ""
  read -rp "Install BLE (Bluetooth) support? [y/N]: " ble_choice
  [[ "$ble_choice" =~ ^[Yy]$ ]] && INSTALL_BLE=true

  echo ""
  # Meshtastic disabled for now — uncomment to re-enable
  # read -rp "Install Meshtastic (LoRa) support? [y/N]: " mesh_choice
  # [[ "$mesh_choice" =~ ^[Yy]$ ]] && INSTALL_MESHTASTIC=true

  if [[ "$INSTALL_BEACON" == true ]]; then
    echo ""
    read -rp "Install beacon as systemd service (auto-start on boot)? [y/N]: " sd_choice
    [[ "$sd_choice" =~ ^[Yy]$ ]] && INSTALL_SYSTEMD=true
  fi
fi

log_ok "Configuration:"
log_info "  Beacon:     $INSTALL_BEACON"
log_info "  Client:     $INSTALL_CLIENT"
log_info "  Network:    $SOLANA_NETWORK"
log_info "  BLE:        $INSTALL_BLE"
log_info "  Meshtastic: $INSTALL_MESHTASTIC"
log_info "  systemd:    $INSTALL_SYSTEMD"

# ═════════════════════════════════════════════════════════════════════════════
# STEP 1 — System dependencies
# ═════════════════════════════════════════════════════════════════════════════
log_step "System dependencies"

MISSING_PKGS=()
for pkg in python3 python3-venv python3-pip curl wget; do
  if ! command -v "$pkg" &>/dev/null && ! dpkg -l "$pkg" &>/dev/null 2>&1; then
    MISSING_PKGS+=("$pkg")
  fi
done

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  log_info "Installing: ${MISSING_PKGS[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y -qq "${MISSING_PKGS[@]}"
fi

if [[ "$INSTALL_BLE" == true ]]; then
  log_info "Installing BLE system deps..."
  sudo apt-get install -y -qq bluetooth bluez libbluetooth-dev || true
fi

log_ok "System dependencies OK"

# ═════════════════════════════════════════════════════════════════════════════
# STEP 2 — Python virtual environment
# ═════════════════════════════════════════════════════════════════════════════
log_step "Python virtual environment"

if [[ ! -d "$VENV_DIR" ]]; then
  log_info "Creating venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
  log_ok "venv created"
else
  log_info "venv already exists at $VENV_DIR"
fi

# Activate
source "$VENV_DIR/bin/activate"
log_ok "venv activated  ($(python --version))"

# Upgrade pip silently
pip install --upgrade pip --quiet

# ═════════════════════════════════════════════════════════════════════════════
# STEP 3 — Python dependencies
# ═════════════════════════════════════════════════════════════════════════════
log_step "Python dependencies"

CORE_DEPS=("rns>=0.7.0" "lxmf" "requests>=2.31.0")
BEACON_DEPS=()
CLIENT_DEPS=()
OPTIONAL_DEPS=()

[[ "$INSTALL_BLE" == true ]]       && OPTIONAL_DEPS+=("bleak>=0.22.0")
[[ "$INSTALL_MESHTASTIC" == true ]] && OPTIONAL_DEPS+=("meshtastic")

# solders for offline signing (client)
if [[ "$INSTALL_CLIENT" == true ]]; then
  OPTIONAL_DEPS+=("solders")
fi

log_info "Installing core deps: ${CORE_DEPS[*]}"
pip install --quiet "${CORE_DEPS[@]}"

if [[ ${#OPTIONAL_DEPS[@]} -gt 0 ]]; then
  log_info "Installing optional deps: ${OPTIONAL_DEPS[*]}"
  pip install --quiet "${OPTIONAL_DEPS[@]}" || log_warn "Some optional deps failed — continuing"
fi

# Verify critical imports
python -c "import RNS; print('  RNS version:', RNS.__version__)" && log_ok "RNS import OK"
python -c "import requests" && log_ok "requests import OK"
python -c "import lxmf" && log_ok "lxmf import OK" || log_warn "lxmf import failed — discovery may not work"

if [[ "$INSTALL_BLE" == true ]]; then
  python -c "import bleak" && log_ok "bleak import OK" || log_warn "bleak import failed"
fi

if [[ "$INSTALL_CLIENT" == true ]]; then
  python -c "import solders" && log_ok "solders import OK (offline signing enabled)" \
    || log_warn "solders not available — offline signing disabled"
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 4 — Reticulum config
# ═════════════════════════════════════════════════════════════════════════════
log_step "Reticulum config"

mkdir -p "$RNS_CONFIG_DIR" "$INTERFACES_DIR"

BACKUP_DONE=false
if [[ -f "$RNS_CONFIG_FILE" ]]; then
  BACKUP="$RNS_CONFIG_FILE.bak.$(date +%Y%m%d_%H%M%S)"
  cp "$RNS_CONFIG_FILE" "$BACKUP"
  log_info "Backed up existing config → $BACKUP"
  BACKUP_DONE=true
fi

BLE_ENABLED="no"
[[ "$INSTALL_BLE" == true ]] && BLE_ENABLED="yes"

cat > "$RNS_CONFIG_FILE" << RNSCFG
[reticulum]
  enable_transport         = True
  share_instance           = Yes
  shared_instance_port     = 37428
  instance_control_port    = 37429
  panic_on_interface_error = No

# ── Local discovery (LAN + localhost) ─────────────────────────────────────────
[[Default Interface]]
  type    = AutoInterface
  enabled = yes

# ── Public TCP hubs ───────────────────────────────────────────────────────────

[[RNS Testnet Dublin]]
  type        = TCPClientInterface
  enabled     = yes
  target_host = dublin.connect.reticulum.network
  target_port = 4965

[[RNS Testnet BetweenTheBorders]]
  type        = TCPClientInterface
  enabled     = yes
  target_host = reticulum.betweentheborders.com
  target_port = 4242

[[Beleth RNS Hub]]
  type        = TCPClientInterface
  enabled     = yes
  target_host = rns.beleth.net
  target_port = 4242

[[g00n.cloud Hub]]
  type        = TCPClientInterface
  enabled     = yes
  target_host = dfw.us.g00n.cloud
  target_port = 6969

# ── Bluetooth BLE ─────────────────────────────────────────────────────────────
# enabled = ${BLE_ENABLED}  (set by setup.sh based on your choice)

[[BLE Interface]]
  type    = BLEInterface
  enabled = ${BLE_ENABLED}

# ── Meshtastic / LoRa ─────────────────────────────────────────────────────────
# Commented out — hardware not connected yet.
# To enable: uncomment, set device path, and re-run setup.sh --meshtastic
#
# [[Meshtastic]]
#   type    = Meshtastic_Interface
#   enabled = no
#   device  = /dev/ttyUSB0
RNSCFG

log_ok "Reticulum config written → $RNS_CONFIG_FILE"

# ── Validate config ────────────────────────────────────────────────────────────
python -c "
import configparser, sys
# RNS uses its own parser but basic INI check catches most errors
cfg = open('$RNS_CONFIG_FILE').read()
# Check for common indentation error: [[section]] with leading spaces
import re
bad = [i+1 for i,l in enumerate(cfg.splitlines()) if re.match(r' +\[\[', l)]
if bad:
    print('ERROR: [[section]] headers have leading spaces on lines:', bad)
    sys.exit(1)
print('Config syntax looks OK')
" && log_ok "Config validated" || { log_err "Config validation failed — check $RNS_CONFIG_FILE"; exit 1; }

# ═════════════════════════════════════════════════════════════════════════════
# STEP 5 — Meshtastic interface file
# ═════════════════════════════════════════════════════════════════════════════
if [[ "$INSTALL_MESHTASTIC" == true ]]; then
  log_step "Meshtastic interface"

  MESH_IFACE="$INTERFACES_DIR/Meshtastic_Interface.py"
  if [[ -f "$MESH_IFACE" ]]; then
    log_info "Meshtastic_Interface.py already present"
  else
    log_info "Downloading Meshtastic_Interface.py from anon0mesh repo..."
    if wget -q -O "$MESH_IFACE" \
      "https://raw.githubusercontent.com/Magicred-1/anon0mesh/main/interfaces/Meshtastic_Interface.py"; then
      log_ok "Meshtastic_Interface.py downloaded → $MESH_IFACE"
    else
      log_warn "Download failed — you can place it manually at $MESH_IFACE"
    fi
  fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 6 — Launcher scripts
# ═════════════════════════════════════════════════════════════════════════════
log_step "Launcher scripts"

if [[ "$INSTALL_BEACON" == true ]]; then
  cat > "$SCRIPT_DIR/run_beacon.sh" << LAUNCHER
#!/usr/bin/env bash
# anon0mesh Beacon launcher
# Edit NETWORK and RPC_URL below to match your setup.
# ─────────────────────────────────────────────────────

SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
source "\$SCRIPT_DIR/venv/bin/activate"

# ── Configuration ──────────────────────────────────────────────────────────
NETWORK="${SOLANA_NETWORK}"          # devnet | mainnet | custom
RPC_URL=""               # leave empty to use default public endpoint
                         # or set e.g. https://my-node.helius-rpc.com/?api-key=XXX
ANNOUNCE_INTERVAL=300    # seconds between re-announces after burst phase

# ── Build args ─────────────────────────────────────────────────────────────
ARGS="--network \$NETWORK --announce-interval \$ANNOUNCE_INTERVAL"
[[ -n "\$RPC_URL" ]] && ARGS="\$ARGS --rpc \$RPC_URL"

exec python "\$SCRIPT_DIR/beacon.py" \$ARGS "\$@"
LAUNCHER
  chmod +x "$SCRIPT_DIR/run_beacon.sh"
  log_ok "run_beacon.sh created"
fi

if [[ "$INSTALL_CLIENT" == true ]]; then
  cat > "$SCRIPT_DIR/run_client.sh" << LAUNCHER
#!/usr/bin/env bash
# anon0mesh Client launcher
# Pass beacon hashes as arguments, or use --discover for auto-discovery.
#
# Examples:
#   ./run_client.sh                           # discover mode
#   ./run_client.sh <HASH1> <HASH2>           # explicit beacons
#   ./run_client.sh <HASH1> --balance <ADDR>  # one-shot balance
# ─────────────────────────────────────────────────────

SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
source "\$SCRIPT_DIR/venv/bin/activate"

# ── Default: auto-discover ─────────────────────────────────────────────────
if [[ \$# -eq 0 ]]; then
  exec python "\$SCRIPT_DIR/client.py" --discover --strategy race
fi

# ── One or more hashes passed → use them + discover ───────────────────────
HASHES=()
EXTRA_ARGS=()
for arg in "\$@"; do
  # If it looks like a hex hash (16+ hex chars), treat as beacon hash
  if [[ "\$arg" =~ ^[0-9a-fA-F]{16,}$ ]]; then
    HASHES+=("\$arg")
  else
    EXTRA_ARGS+=("\$arg")
  fi
done

CMD="python \$SCRIPT_DIR/client.py"
[[ \${#HASHES[@]} -gt 0 ]] && CMD="\$CMD --beacon \${HASHES[*]}"
CMD="\$CMD --discover"   # always keep discovery on for new beacons
CMD="\$CMD --strategy race"

exec \$CMD "\${EXTRA_ARGS[@]}"
LAUNCHER
  chmod +x "$SCRIPT_DIR/run_client.sh"
  log_ok "run_client.sh created"
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 7 — systemd service (beacon only, optional)
# ═════════════════════════════════════════════════════════════════════════════
if [[ "$INSTALL_BEACON" == true && "$INSTALL_SYSTEMD" == true ]]; then
  log_step "systemd service"

  SERVICE_FILE="/etc/systemd/system/anon0mesh-beacon.service"
  CURRENT_USER="$(whoami)"

  sudo tee "$SERVICE_FILE" > /dev/null << SERVICE
[Unit]
Description=anon0mesh Beacon — Reticulum RPC gateway for Solana
Documentation=https://anonme.sh/docs
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/run_beacon.sh
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=anon0mesh-beacon

# Give Reticulum time to connect to mesh peers before killing on stop
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
SERVICE

  sudo systemctl daemon-reload
  sudo systemctl enable anon0mesh-beacon.service
  log_ok "systemd service installed and enabled"
  log_info "Start now:    sudo systemctl start anon0mesh-beacon"
  log_info "View logs:    journalctl -u anon0mesh-beacon -f"
  log_info "Status:       sudo systemctl status anon0mesh-beacon"
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 8 — Smoke test
# ═════════════════════════════════════════════════════════════════════════════
log_step "Smoke test"

python -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
errors = []

try:
    import RNS
except ImportError:
    errors.append('RNS not importable')

try:
    from shared import APP_NAME, APP_ASPECT, build_rpc, decode_json
    r = build_rpc('getSlot')
    import json; json.loads(r)
except Exception as e:
    errors.append(f'shared.py error: {e}')

if errors:
    for e in errors:
        print('FAIL:', e)
    sys.exit(1)
else:
    print('All imports OK')
" && log_ok "Smoke test passed" || { log_err "Smoke test failed"; exit 1; }

# ═════════════════════════════════════════════════════════════════════════════
# Done — print summary
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"
echo -e "${BOLD}${GREEN}  Setup complete!${R}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"
echo ""

if [[ "$INSTALL_BEACON" == true ]]; then
  echo -e "${BOLD}  Start beacon:${R}"
  echo -e "    ${CYAN}./run_beacon.sh${R}"
  echo ""
fi

if [[ "$INSTALL_CLIENT" == true ]]; then
  echo -e "${BOLD}  Start client (auto-discover):${R}"
  echo -e "    ${CYAN}./run_client.sh${R}"
  echo ""
  echo -e "${BOLD}  Start client (explicit beacon hash):${R}"
  echo -e "    ${CYAN}./run_client.sh <BEACON_HASH>${R}"
  echo ""
  echo -e "${BOLD}  One-shot balance check:${R}"
  echo -e "    ${CYAN}./run_client.sh <HASH> --balance <WALLET_ADDRESS>${R}"
  echo ""
fi

if [[ "$INSTALL_SYSTEMD" == true ]]; then
  echo -e "${BOLD}  Beacon systemd service:${R}"
  echo -e "    ${CYAN}sudo systemctl start anon0mesh-beacon${R}"
  echo -e "    ${CYAN}journalctl -u anon0mesh-beacon -f${R}"
  echo ""
fi

echo -e "${DIM}  Reticulum config:  $RNS_CONFIG_FILE${R}"
echo -e "${DIM}  venv:              $VENV_DIR${R}"
echo -e "${DIM}  Network:           $SOLANA_NETWORK${R}"
[[ "$BACKUP_DONE" == true ]] && echo -e "${DIM}  Old config backed up (see $RNS_CONFIG_DIR/*.bak.*)${R}"
echo ""
