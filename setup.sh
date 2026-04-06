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
  █████╗ ███╗  ██╗ ██████╗ ███╗  ██╗    ███╗  ███╗███████╗███████╗██╗  ██╗
 ██╔══██╗████╗ ██║██╔═══██╗████╗ ██║    ████╗████║██╔════╝██╔════╝██║  ██║
 ███████║██╔██╗██║██║   ██║██╔██╗██║    ██╔████╔██║█████╗  ███████╗███████║
 ██╔══██║██║╚████║██║   ██║██║╚████║    ██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║
 ██║  ██║██║ ╚███║╚██████╔╝██║ ╚███║    ██║ ╚═╝ ██║███████╗███████║██║  ██║
 ╚═╝  ╚═╝╚═╝  ╚══╝ ╚═════╝ ╚═╝  ╚══╝    ╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝
${R}${DIM}              a n o n m e s h  ·  Mesh First, Chain When It Matters${R}
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
INSTALL_RNODE=false
SETUP_WALLET=false
SOLANA_NETWORK="devnet"
NONINTERACTIVE=false

# ── OS detection ────────────────────────────────────────────────────────────
OS_TYPE="linux"
PKG_MANAGER="apt-get"
if [[ "$(uname -s)" == "Darwin" ]]; then
  OS_TYPE="macos"
  PKG_MANAGER="brew"
fi

# Wallet/nonce outputs — populated by Step 9, referenced in the summary
WALLET_KEYPAIR_PATH=""
WALLET_PUBKEY=""
NONCE_ACCOUNT_PUBKEY=""
NONCE_KEYPAIR_PATH=""

# ── Argument parsing ──────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --beacon)    INSTALL_BEACON=true;  NONINTERACTIVE=true ;;
    --client)    INSTALL_CLIENT=true;  NONINTERACTIVE=true ;;
    --both)      INSTALL_BEACON=true;  INSTALL_CLIENT=true; NONINTERACTIVE=true ;;
    --systemd)   INSTALL_SYSTEMD=true ;;
    --ble)       INSTALL_BLE=true ;;
    --meshtastic) INSTALL_MESHTASTIC=true ;;
    --rnode)      INSTALL_RNODE=true ;;
    --mainnet)     SOLANA_NETWORK="mainnet" ;;
    --devnet)      SOLANA_NETWORK="devnet" ;;
    --wallet-setup) SETUP_WALLET=true ;;
    --help|-h)
      echo "Usage: $0 [--beacon] [--client] [--both] [--systemd] [--ble] [--rnode] [--meshtastic] [--mainnet|--devnet] [--wallet-setup]"
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
  read -rp "Configure RNode LoRa interface (Heltec V3)? [y/N]: " rnode_choice
  [[ "$rnode_choice" =~ ^[Yy]$ ]] && INSTALL_RNODE=true

  if [[ "$INSTALL_BEACON" == true ]]; then
    echo ""
    read -rp "Install beacon as systemd service (auto-start on boot)? [y/N]: " sd_choice
    [[ "$sd_choice" =~ ^[Yy]$ ]] && INSTALL_SYSTEMD=true
  fi

  if [[ "$INSTALL_CLIENT" == true ]]; then
    echo ""
    echo -e "${BOLD}Set up a Solana signing wallet + durable nonce account?${R}"
    echo "  · A keypair is needed to sign transactions offline."
    echo "  · A nonce account lets signed transactions stay valid indefinitely"
    echo "    (no blockhash expiry) — ideal for off-grid / mesh-delayed relay."
    read -rp "Set up now? [y/N]: " wallet_choice
    [[ "$wallet_choice" =~ ^[Yy]$ ]] && SETUP_WALLET=true
  fi
fi

log_ok "Configuration:"
log_info "  Beacon:     $INSTALL_BEACON"
log_info "  Client:     $INSTALL_CLIENT"
log_info "  Network:    $SOLANA_NETWORK"
log_info "  BLE:        $INSTALL_BLE"
log_info "  RNode LoRa: $INSTALL_RNODE"
log_info "  Meshtastic: $INSTALL_MESHTASTIC"
log_info "  systemd:    $INSTALL_SYSTEMD"
log_info "  OS:         $OS_TYPE ($PKG_MANAGER)"

# ═════════════════════════════════════════════════════════════════════════════
# STEP 1 — System dependencies
# ═════════════════════════════════════════════════════════════════════════════
log_step "System dependencies"

if [[ "$OS_TYPE" == "macos" ]]; then
  # macOS — use Homebrew
  if ! command -v brew &>/dev/null; then
    log_err "Homebrew not found. Install it: https://brew.sh"
    exit 1
  fi

  MISSING_CMDS=()
  for cmd in python3 curl wget; do
    command -v "$cmd" &>/dev/null || MISSING_CMDS+=("$cmd")
  done

  if [[ ${#MISSING_CMDS[@]} -gt 0 ]]; then
    log_info "Installing via brew: ${MISSING_CMDS[*]}"
    brew install "${MISSING_CMDS[@]}"
  fi

  if [[ "$INSTALL_BLE" == true ]]; then
    log_info "BLE: macOS has native CoreBluetooth — no extra system deps needed."
  fi
else
  # Linux — use apt-get
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

# solders for offline signing + qrcode for wallet QR display (client)
if [[ "$INSTALL_CLIENT" == true ]]; then
  OPTIONAL_DEPS+=("solders" "qrcode>=7.4.2")
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
python -c "import LXMF" && log_ok "LXMF import OK" || log_warn "LXMF import failed — discovery may not work"

if [[ "$INSTALL_BLE" == true ]]; then
  python -c "import bleak" && log_ok "bleak import OK" || log_warn "bleak import failed"
fi

if [[ "$INSTALL_CLIENT" == true ]]; then
  python -c "import solders" && log_ok "solders import OK (offline signing enabled)" \
    || log_warn "solders not available — offline signing disabled"
  python -c "import qrcode" && log_ok "qrcode import OK (wallet QR display enabled)" \
    || log_warn "qrcode not available — wallet QR display disabled"
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

[interfaces]

# ── Local discovery (LAN + localhost) ─────────────────────────────────────────
  [[Default Interface]]
    type    = AutoInterface
    enabled = yes

# ── Public TCP hubs ───────────────────────────────────────────────────────────

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

  [[RNS Testnet BetweenTheBorders]]
    type        = TCPClientInterface
    enabled     = yes
    target_host = reticulum.betweentheborders.com
    target_port = 4242
RNSCFG

log_ok "Reticulum config written → $RNS_CONFIG_FILE"

# ── Validate config ────────────────────────────────────────────────────────────
python -c "
import sys, re
cfg = open('$RNS_CONFIG_FILE').read()
lines = cfg.splitlines()
has_reticulum = any(l.strip() == '[reticulum]' for l in lines)
has_interfaces = any(l.strip() == '[interfaces]' for l in lines)
errors = []
if not has_reticulum:
    errors.append('Missing [reticulum] section')
if not has_interfaces:
    errors.append('Missing [interfaces] section')
if errors:
    for e in errors: print('ERROR:', e)
    sys.exit(1)
print('Config syntax looks OK')
" && log_ok "Config validated" || { log_err "Config validation failed — check $RNS_CONFIG_FILE"; exit 1; }

# ═════════════════════════════════════════════════════════════════════════════
# STEP 4b — RNode LoRa interface (Heltec V3)
# ═════════════════════════════════════════════════════════════════════════════
if [[ "$INSTALL_RNODE" == true ]]; then
  log_step "RNode LoRa interface (Heltec V3)"

  # ── Detect serial device ──────────────────────────────────────────────────
  RNODE_PORT=""
  SERIAL_DEVICES=()

  if [[ "$OS_TYPE" == "macos" ]]; then
    while IFS= read -r dev; do
      [[ -n "$dev" ]] && SERIAL_DEVICES+=("$dev")
    done < <(ls /dev/cu.usbserial-* /dev/cu.usbmodem-* /dev/cu.SLAB_USBtoUART* 2>/dev/null || true)
  else
    while IFS= read -r dev; do
      [[ -n "$dev" ]] && SERIAL_DEVICES+=("$dev")
    done < <(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true)
  fi

  if [[ ${#SERIAL_DEVICES[@]} -eq 0 ]]; then
    log_warn "No serial devices found."
    log_info "  Plug in the Heltec V3 and check:"
    if [[ "$OS_TYPE" == "macos" ]]; then
      log_info "    ls /dev/cu.usbserial-*"
    else
      log_info "    ls /dev/ttyUSB*"
    fi
    echo ""
    read -rp "  Enter serial port manually (or press Enter to skip): " manual_port
    if [[ -n "$manual_port" ]]; then
      RNODE_PORT="$manual_port"
    fi
  elif [[ ${#SERIAL_DEVICES[@]} -eq 1 ]]; then
    RNODE_PORT="${SERIAL_DEVICES[0]}"
    log_ok "Found serial device: $RNODE_PORT"
  else
    log_info "Multiple serial devices found:"
    for i in "${!SERIAL_DEVICES[@]}"; do
      echo "  $((i+1))) ${SERIAL_DEVICES[$i]}"
    done
    read -rp "  Select device [1-${#SERIAL_DEVICES[@]}]: " dev_choice
    if [[ "$dev_choice" =~ ^[0-9]+$ ]] && (( dev_choice >= 1 && dev_choice <= ${#SERIAL_DEVICES[@]} )); then
      RNODE_PORT="${SERIAL_DEVICES[$((dev_choice-1))]}"
    else
      log_warn "Invalid choice — skipping RNode setup"
    fi
  fi

  if [[ -n "$RNODE_PORT" ]]; then
    # ── Frequency region ──────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}LoRa frequency region:${R}"
    echo "  1) EU 868 MHz  (Europe, Africa, Middle East)"
    echo "  2) US 915 MHz  (Americas, Australia)"
    read -rp "Choice [1/2, default=1]: " freq_choice

    if [[ "$freq_choice" == "2" ]]; then
      RNODE_FREQ=915000000
      RNODE_TXPOWER=22
      RNODE_REGION="US 915 MHz"
    else
      RNODE_FREQ=867200000
      RNODE_TXPOWER=14
      RNODE_REGION="EU 868 MHz"
    fi

    log_info "Configuring RNode: $RNODE_PORT @ $RNODE_REGION"

    # ── Append RNode interface to existing Reticulum config ───────────────
    cat >> "$RNS_CONFIG_FILE" << RNODE

# ── RNODE LORA (Heltec V3 / SX1262) ─────────────────────────────────────────
# Region: ${RNODE_REGION}
# Serial: ${RNODE_PORT}

  [[RNode LoRa]]
    type              = RNodeInterface
    interface_enabled = True
    port              = ${RNODE_PORT}
    speed             = 115200
    databits          = 8
    parity            = none
    stopbits          = 1
    flow_control      = False
    frequency         = ${RNODE_FREQ}
    bandwidth         = 125000
    spreadingfactor   = 7
    codingrate        = 5
    txpower           = ${RNODE_TXPOWER}
RNODE

    log_ok "RNode interface added to $RNS_CONFIG_FILE"
    log_info "  Port:      $RNODE_PORT"
    log_info "  Region:    $RNODE_REGION"
    log_info "  SF7/BW125k → ~5.5 kbps raw, ~3.5 kbps usable"
    echo ""
    log_info "Make sure the Heltec V3 is flashed with RNode firmware:"
    log_info "  pip install rns && rnodeconf --autoinstall"
  else
    log_warn "Skipping RNode setup — no serial port configured."
    log_info "  Run setup.sh --rnode later, or add manually from:"
    log_info "  $SCRIPT_DIR/config/reticulum_rnode.conf"
  fi
fi

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

CMD=(python "\$SCRIPT_DIR/client.py")
[[ \${#HASHES[@]} -gt 0 ]] && CMD+=(--beacon "\${HASHES[@]}")
CMD+=(--discover --strategy race)   # always keep discovery on for new beacons

exec "\${CMD[@]}" "\${EXTRA_ARGS[@]}"
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
    import json; json.loads(build_rpc('getSlot'))
except Exception as e:
    errors.append(f'shared: {e}')

try:
    import state
    assert hasattr(state, 'pool')
    assert hasattr(state, 'active_wallet')
except Exception as e:
    errors.append(f'state: {e}')

try:
    from mesh import BeaconPool, BeaconAnnounceHandler, BeaconLink
except Exception as e:
    errors.append(f'mesh: {e}')

try:
    import rpc
    assert callable(rpc.rpc_call)
    assert callable(rpc.get_balance)
    assert callable(rpc.send_transaction)
    assert callable(rpc.get_nonce_account)
except Exception as e:
    errors.append(f'rpc: {e}')

try:
    import wallet
    assert callable(wallet.generate_wallet)
    assert callable(wallet.import_wallet)
    assert callable(wallet.create_nonce_account)
except Exception as e:
    errors.append(f'wallet: {e}')

try:
    import menu
    assert callable(menu.repl)
except Exception as e:
    errors.append(f'menu: {e}')

if errors:
    for e in errors:
        print('FAIL:', e)
    sys.exit(1)
else:
    print('All modules OK')
" && log_ok "Smoke test passed" || { log_err "Smoke test failed"; exit 1; }

# ═════════════════════════════════════════════════════════════════════════════
# STEP 9 — Solana wallet + durable nonce account (client only, opt-in)
# ═════════════════════════════════════════════════════════════════════════════
if [[ "$INSTALL_CLIENT" == true && "$SETUP_WALLET" == true ]]; then
  log_step "Solana wallet + durable nonce account"

  if ! python -c "import solders" 2>/dev/null; then
    log_warn "solders not installed — skipping wallet setup (pip install solders)"
  else

    # ── 9a: Keypair ──────────────────────────────────────────────────────────
    log_info "Step 9a: Solana signing keypair"

    if [[ -f "$SCRIPT_DIR/wallet.json" ]]; then
      log_info "wallet.json already exists in project directory."
      read -rp "  Reuse it? [Y/n]: " reuse_choice
      if [[ ! "$reuse_choice" =~ ^[Nn]$ ]]; then
        WALLET_KEYPAIR_PATH="$SCRIPT_DIR/wallet.json"
        log_ok "Reusing existing wallet.json"
      fi
    fi

    if [[ -z "$WALLET_KEYPAIR_PATH" ]]; then
      echo "  1) Generate a new keypair  (saved to wallet.json)"
      echo "  2) Use an existing keypair file"
      read -rp "Choice [1/2, default=1]: " kp_choice

      if [[ "$kp_choice" == "2" ]]; then
        read -rp "  Path to keypair JSON: " kp_import
        if [[ ! -f "$kp_import" ]]; then
          log_err "File not found: $kp_import — skipping wallet setup"
        else
          WALLET_KEYPAIR_PATH="$kp_import"
          log_ok "Using keypair: $WALLET_KEYPAIR_PATH"
        fi
      else
        WALLET_KEYPAIR_PATH="$SCRIPT_DIR/wallet.json"
        # Generate keypair via Python; pass path through env to avoid heredoc quoting issues
        ANON0MESH_KP_PATH="$WALLET_KEYPAIR_PATH" python << 'PYEOF'
import os, json
from solders.keypair import Keypair
kp   = Keypair()
path = os.environ["ANON0MESH_KP_PATH"]
with open(path, "w") as f:
    json.dump(list(bytes(kp)), f)
print(f"  Public key : {kp.pubkey()}")
print(f"  Saved to   : {path}")
PYEOF
        if [[ $? -ne 0 ]]; then
          log_err "Keypair generation failed"; WALLET_KEYPAIR_PATH=""
        else
          log_ok "Keypair saved → $WALLET_KEYPAIR_PATH"
          log_warn "Keep wallet.json safe — it contains your private key!"
        fi
      fi
    fi

    # ── Show public key + funding instructions ────────────────────────────────
    if [[ -n "$WALLET_KEYPAIR_PATH" ]]; then
      WALLET_PUBKEY=$(ANON0MESH_KP_PATH="$WALLET_KEYPAIR_PATH" python << 'PYEOF'
import os, json
from solders.keypair import Keypair
with open(os.environ["ANON0MESH_KP_PATH"]) as f:
    kp = Keypair.from_bytes(bytes(json.load(f)))
print(kp.pubkey(), end="")
PYEOF
)
      log_ok "Wallet public key: $WALLET_PUBKEY"
      echo ""
      if [[ "$SOLANA_NETWORK" == "devnet" ]]; then
        log_info "Get free devnet SOL (needed for nonce account):"
        log_info "  https://faucet.solana.com"
        log_info "  solana airdrop 2 $WALLET_PUBKEY --url devnet"
      else
        log_info "Fund this address with SOL before creating the nonce account:"
        log_info "  $WALLET_PUBKEY"
      fi

      # ── 9b: Durable nonce account ─────────────────────────────────────────
      echo ""
      log_info "Step 9b: Durable nonce account"
      echo ""
      echo "  A durable nonce account replaces the expiring blockhash in transactions."
      echo "  Transactions signed with a nonce stay valid until relayed — perfect for"
      echo "  off-grid / mesh-delayed scenarios where timing is unpredictable."
      echo "  Cost: ~0.00144768 SOL (rent-exempt deposit, recoverable on close)."
      echo ""
      read -rp "  Create nonce account now? [y/N]: " nonce_choice

      if [[ "$nonce_choice" =~ ^[Yy]$ ]]; then
        # Resolve direct RPC URL for setup (internet available here, no mesh needed)
        case "$SOLANA_NETWORK" in
          mainnet) _SETUP_RPC="https://api.mainnet-beta.solana.com" ;;
          testnet) _SETUP_RPC="https://api.testnet.solana.com" ;;
          *)       _SETUP_RPC="https://api.devnet.solana.com" ;;
        esac

        log_info "Connecting to $_SETUP_RPC ..."

        # Run Python once.
        # Status/progress messages  → stderr  (shown directly to the user)
        # KEY=VALUE result lines    → stdout  (captured into _nonce_out)
        _nonce_out=$(
          ANON0MESH_KP_PATH="$WALLET_KEYPAIR_PATH" \
          ANON0MESH_RPC="$_SETUP_RPC" \
          ANON0MESH_DIR="$SCRIPT_DIR" \
          python << 'PYEOF'
import os, sys, json, base64
import requests
from solders.keypair     import Keypair
from solders.pubkey      import Pubkey
from solders.transaction import Transaction
from solders.system_program import (
    create_account, CreateAccountParams,
    initialize_nonce_account, InitializeNonceAccountParams,
)
from solders.message import Message
from solders.hash    import Hash

RPC       = os.environ["ANON0MESH_RPC"]
SAVE_DIR  = os.environ["ANON0MESH_DIR"]
NONCE_LEN = 80  # fixed by the Solana runtime

def rpc(method, params):
    r = requests.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"].get("message", str(d["error"]))
                           if isinstance(d["error"], dict) else str(d["error"]))
    return d

try:
    with open(os.environ["ANON0MESH_KP_PATH"]) as f:
        payer = Keypair.from_bytes(bytes(json.load(f)))

    nonce_kp   = Keypair()
    nonce_path = os.path.join(SAVE_DIR, f"nonce_{str(nonce_kp.pubkey())[:8]}.json")
    with open(nonce_path, "w") as f:
        json.dump(list(bytes(nonce_kp)), f)

    payer_pub = payer.pubkey()
    nonce_pub = nonce_kp.pubkey()
    print(f"  Nonce keypair:  {nonce_pub}", file=sys.stderr)

    rent      = rpc("getMinimumBalanceForRentExemption", [NONCE_LEN])["result"]
    print(f"  Rent required:  {rent} lamports", file=sys.stderr)
    blockhash = rpc("getLatestBlockhash", [])["result"]["value"]["blockhash"]

    create_ix = create_account(CreateAccountParams(
        from_pubkey = payer_pub,
        to_pubkey   = nonce_pub,
        lamports    = rent,
        space       = NONCE_LEN,
        owner       = Pubkey.from_string("11111111111111111111111111111111"),
    ))
    init_ix = initialize_nonce_account(InitializeNonceAccountParams(
        nonce_pubkey      = nonce_pub,
        authority = payer_pub,
    ))
    bh  = Hash.from_string(blockhash)
    msg = Message.new_with_blockhash([create_ix, init_ix], payer_pub, bh)
    tx  = Transaction.new_unsigned(msg)
    tx.sign([payer, nonce_kp], bh)

    print("  Sending transaction...", file=sys.stderr)
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                  {"encoding": "base64"}])["result"]

    # KEY=VALUE to stdout — captured by the shell
    print(f"NONCE_PUBKEY={nonce_pub}")
    print(f"NONCE_KEYPAIR_PATH={nonce_path}")
    print(f"NONCE_SIG={sig}")
    print(f"NONCE_LAMPORTS={rent}")

except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    # Remove the saved keypair only if we generated it and the tx failed
    try:
        os.remove(nonce_path)
    except Exception:
        pass
    sys.exit(1)
PYEOF
        )
        _nonce_exit=$?

        if [[ $_nonce_exit -eq 0 ]]; then
          while IFS='=' read -r key val; do
            case "$key" in
              NONCE_PUBKEY)       NONCE_ACCOUNT_PUBKEY="$val" ;;
              NONCE_KEYPAIR_PATH) NONCE_KEYPAIR_PATH="$val" ;;
              NONCE_SIG)          _nonce_sig="$val" ;;
              NONCE_LAMPORTS)     _nonce_rent="$val" ;;
            esac
          done <<< "$_nonce_out"
          log_ok "Nonce account created!"
          log_info "  Nonce pubkey:  $NONCE_ACCOUNT_PUBKEY"
          log_info "  Nonce keypair: $NONCE_KEYPAIR_PATH"
          log_info "  Funded:        $_nonce_rent lamports"
          log_info "  Signature:     $_nonce_sig"
        else
          log_warn "Nonce account creation failed."
          log_info "  Most likely cause: wallet not funded yet."
          if [[ "$SOLANA_NETWORK" == "devnet" ]]; then
            log_info "  Fund with:   solana airdrop 2 $WALLET_PUBKEY --url devnet"
          fi
          log_info "  Retry later: ./run_client.sh <BEACON_HASH>"
          log_info "    In the menu: DURABLE NONCE → Create nonce account"
        fi
      else
        log_info "Skipping nonce account — create later from the client menu:"
        log_info "  ./run_client.sh <BEACON_HASH>"
        log_info "  In the menu: DURABLE NONCE → Create nonce account"
      fi
    fi

  fi  # solders available
fi  # INSTALL_CLIENT && SETUP_WALLET

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
[[ "$BACKUP_DONE" == true ]]        && echo -e "${DIM}  Old config backed up (see $RNS_CONFIG_DIR/*.bak.*)${R}"
[[ -n "$WALLET_KEYPAIR_PATH" ]]     && echo -e "${DIM}  Signing keypair:   $WALLET_KEYPAIR_PATH  ($WALLET_PUBKEY)${R}"
[[ -n "$NONCE_ACCOUNT_PUBKEY" ]]    && echo -e "${DIM}  Nonce account:     $NONCE_ACCOUNT_PUBKEY${R}"
[[ -n "$NONCE_KEYPAIR_PATH" ]]      && echo -e "${DIM}  Nonce keypair:     $NONCE_KEYPAIR_PATH${R}"
echo ""
