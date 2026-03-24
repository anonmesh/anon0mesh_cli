"""
menu.py — interactive menu REPL
=================================
All _do_* action handlers, the menu section table, and the REPL loop.

To add a new command:
  1. Write a _do_<name>() function below.
  2. Add it to _MENU_SECTIONS in the right section.
"""

import io
import json
import threading

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_spinner_idx    = 0

import sys
import state
from shared import (
    log_info, log_ok, log_warn, log_err,
    BOLD, CYAN, GREEN, YELLOW, RED, RESET, DIM,
    set_quiet,
)
from rpc import (
    rpc_call,
    get_balance, confidential_get_balance,
    get_slot, get_block_height, get_transaction_count, get_recent_blockhash,
    get_token_accounts, send_transaction, simulate_transaction,
    get_nonce_account, get_beacon_pubkey, cosign_and_send,
)
from wallet import (
    generate_wallet, import_wallet,
    offline_sign_nonce_transfer,
    create_nonce_account, partial_sign_arcium_transfer,
    scan_nonce_accounts, HAS_SOLDERS,
)

_RELAY_PROMPT      = "  Relay now? [y/N]: "
_MAX_RETRIES       = 3
_NEED_SOLDERS      = "Requires: pip install solders"
_PROMPT_TO         = "Recipient address"
_PROMPT_AMOUNT     = "Amount  (SOL, e.g. 0.5)"
_PROMPT_NONCE_ACCT = "Nonce account pubkey"
_INVALID_AMOUNT    = "Invalid amount"
_NO_WALLET         = "No wallet loaded — use WALLET → Generate or Import first"


# ═══════════════════════════════════════════════════════════════════════════════
# Input helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ask(prompt: str) -> str:
    """Prompt for a single line. Returns '' on empty or Ctrl-C."""
    try:
        return input(f"  {CYAN}›{RESET} {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _ask_int(prompt: str) -> int | None:
    raw = _ask(prompt)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log_warn("Expected an integer")
        return None


def _pick(prompt: str, options: list[str]) -> int | None:
    """
    Display a numbered list and return the 0-based index of the chosen item.
    Auto-selects if only one option. Returns None on cancel.
    """
    if not options:
        return None
    if len(options) == 1:
        print(f"  {DIM}Auto-selected:{RESET} {options[0]}")
        return 0
    print()
    for i, opt in enumerate(options, 1):
        print(f"  {CYAN}{BOLD}{i:>2}{RESET}  {opt}")
    print()
    raw = _ask(prompt)
    if not raw:
        return None
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return idx
        log_warn("Out of range")
        return None
    except ValueError:
        log_warn("Expected a number")
        return None


class _Spinner:
    """Inline spinner that overwrites the same line without polluting scroll history."""
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str):
        self._label = label
        self._idx   = 0
        self._active = False

    def __enter__(self):
        self._active = True
        set_quiet(True)
        self._tick()
        return self

    def tick(self, label: str | None = None):
        if label:
            self._label = label
        self._tick()

    def _tick(self):
        frame = self._FRAMES[self._idx % len(self._FRAMES)]
        self._idx += 1
        sys.stdout.write(f"\r  {YELLOW}{frame}{RESET} {self._label}  ")
        sys.stdout.flush()

    def done(self, msg: str, ok: bool = True):
        col = GREEN if ok else RED
        sym = "✔" if ok else "✘"
        sys.stdout.write(f"\r  {col}{sym}{RESET} {msg}\n")
        sys.stdout.flush()

    def __exit__(self, *_):
        self._active = False
        set_quiet(False)


# ═══════════════════════════════════════════════════════════════════════════════
# Action handlers — one per menu item
# ═══════════════════════════════════════════════════════════════════════════════

def _do_generate_wallet():
    if not HAS_SOLDERS:
        log_err(_NEED_SOLDERS); return
    path = _ask("Save path  (blank = wallet_<prefix>.json)")
    generate_wallet(path or None)


def _do_import_wallet():
    if not HAS_SOLDERS:
        log_err(_NEED_SOLDERS); return
    import getpass
    print(f"  {DIM}Accepts: base58 · hex (64 or 128 chars) · JSON array [1,2,...]{RESET}")
    try:
        raw = getpass.getpass(f"  {CYAN}›{RESET} Private key (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print(); return
    if not raw:
        return
    path = _ask("Save path  (e.g. wallet.json)")
    if not path:
        return
    import_wallet(raw, path)


def _do_balance():
    addr = _ask("Wallet address (blank = active wallet)")
    if not addr and state.active_wallet:
        addr = state.active_wallet["pubkey"]
    if addr:
        get_balance(addr)


def _do_cbalance():
    addr = _ask("Wallet address (blank = active wallet)")
    if not addr and state.active_wallet:
        addr = state.active_wallet["pubkey"]
    if addr:
        confidential_get_balance(addr)


def _do_tokens():
    addr = _ask("Owner address (blank = active wallet)")
    if not addr and state.active_wallet:
        addr = state.active_wallet["pubkey"]
    if addr:
        get_token_accounts(addr)


def _do_slot():      get_slot()
def _do_height():    get_block_height()
def _do_txcount():   get_transaction_count()
def _do_blockhash(): get_recent_blockhash()


# ── Simple send ────────────────────────────────────────────────────────────────

def _select_nonce_account() -> str | None:
    """Pick a nonce account pubkey: auto-detect nonce_*.json files, else prompt."""
    found = scan_nonce_accounts()
    if found:
        labels = [f"{n['pubkey'][:8]}…{n['pubkey'][-4:]}  {DIM}{n['path']}{RESET}" for n in found]
        idx = _pick("Select nonce account", labels)
        if idx is None:
            return None
        return found[idx]["pubkey"]
    pubkey = _ask(_PROMPT_NONCE_ACCT)
    return pubkey or None


def _do_send_sol():
    """SOL send via durable nonce: sign once, relay via beacon, auto-retry broadcast."""
    if not HAS_SOLDERS:
        log_err(_NEED_SOLDERS); return
    if not state.active_wallet:
        log_warn(_NO_WALLET); return

    kp_path = state.active_wallet["path"]
    pubkey  = state.active_wallet["pubkey"]

    print(f"\n  {BOLD}From:{RESET} {GREEN}{pubkey}{RESET}")
    get_balance(pubkey)

    nonce_pubkey = _select_nonce_account()
    if not nonce_pubkey: return

    nonce_info = get_nonce_account(nonce_pubkey)
    if not nonce_info: return

    to = _ask(_PROMPT_TO)
    if not to: return

    raw = _ask(_PROMPT_AMOUNT)
    if not raw: return
    try:
        lamports = int(float(raw) * 1_000_000_000)
    except ValueError:
        log_warn(_INVALID_AMOUNT); return
    if lamports <= 0:
        log_warn("Amount must be greater than 0"); return

    tx_b64 = offline_sign_nonce_transfer(
        kp_path, nonce_pubkey, kp_path, to, lamports, nonce_info["nonce"]
    )
    if not tx_b64: return

    _broadcast_with_retry(tx_b64)


def _broadcast_with_retry(tx_b64: str) -> None:
    """Relay a signed tx, retrying up to _MAX_RETRIES times. Sign only once — nonce never expires."""
    with _Spinner("Broadcasting…") as sp:
        for attempt in range(1, _MAX_RETRIES + 1):
            if attempt > 1:
                sp.tick(f"Retry {attempt}/{_MAX_RETRIES}…")
            resp = rpc_call("sendTransaction", [tx_b64, {"encoding": "base64"}])
            if resp and "result" in resp:
                sp.done(f"Confirmed — sig: {resp['result'][:20]}…")
                print(f"\n  {GREEN}{BOLD}Signature: {resp['result']}{RESET}\n")
                return
            err = resp.get("error", {}).get("message", "no response") if resp else "no response"
            sp.tick(f"Attempt {attempt} failed: {err[:40]}")
        sp.done("All broadcast attempts failed", ok=False)


def _do_arcium_transfer():
    """Arcium-secured SOL transfer: durable nonce + mandatory beacon co-signature."""
    if not HAS_SOLDERS:
        log_err(_NEED_SOLDERS); return
    if not state.active_wallet:
        log_warn(_NO_WALLET); return

    kp_path = state.active_wallet["path"]
    pubkey  = state.active_wallet["pubkey"]

    print(f"\n  {BOLD}From:{RESET} {pubkey}")
    get_balance(pubkey)

    nonce = _select_nonce_account()
    if not nonce: return
    to    = _ask(_PROMPT_TO)
    if not to: return
    raw   = _ask(_PROMPT_AMOUNT)
    if not raw: return
    try:
        lamports = int(float(raw) * 1_000_000_000)
    except ValueError:
        log_warn(_INVALID_AMOUNT); return
    if lamports <= 0:
        log_warn("Amount must be greater than 0"); return
    nval  = None  # always fetch from chain — ensures fresh nonce

    log_info("Fetching beacon co-signing pubkey...")
    beacon_pk = get_beacon_pubkey()
    if not beacon_pk:
        log_err("Could not get beacon pubkey — is ARCIUM_PAYER_KEYPAIR set on the beacon?")
        return
    log_ok(f"Beacon: {beacon_pk}")

    partial_tx = partial_sign_arcium_transfer(
        kp_path, beacon_pk, nonce, to, lamports, nval or None
    )
    if partial_tx:
        cosign_and_send(partial_tx)


# ── Advanced ───────────────────────────────────────────────────────────────────

def _do_relay_raw():
    tx = _ask("Signed transaction (base64)")
    if tx:
        send_transaction(tx)


def _do_simulate():
    tx = _ask("Signed transaction (base64)")
    if tx:
        simulate_transaction(tx)


def _do_create_nonce():
    if not state.active_wallet:
        log_warn(_NO_WALLET); return
    kp       = state.active_wallet["path"]
    nonce_kp = _ask("Nonce keypair JSON path  (blank = generate new)")
    create_nonce_account(kp, nonce_kp or None, None)


def _do_get_nonce():
    pubkey = _ask(_PROMPT_NONCE_ACCT)
    if pubkey:
        get_nonce_account(pubkey)


def _do_sign_nonce():
    kp    = _ask("Payer keypair JSON path")
    if not kp: return
    nonce = _ask(_PROMPT_NONCE_ACCT)
    if not nonce: return
    auth  = _ask("Authority keypair JSON path  (blank = use payer)")
    to    = _ask(_PROMPT_TO)
    if not to: return
    raw   = _ask(_PROMPT_AMOUNT)
    if not raw: return
    try:
        lamps = int(float(raw) * 1_000_000_000)
    except ValueError:
        log_warn(_INVALID_AMOUNT); return
    nval  = _ask("Nonce value  (blank = fetch from chain)")
    tx_b64 = offline_sign_nonce_transfer(kp, nonce, auth or kp, to, lamps, nval or None)
    if tx_b64 and input(f"  {CYAN}›{RESET} {_RELAY_PROMPT}").strip().lower() == "y":
        send_transaction(tx_b64)


def _do_beacons():
    print(state.pool.status_table())


def _do_add():
    h = _ask("Beacon destination hash")
    if h:
        threading.Thread(
            target=state.pool.add,
            args=(h,),
            kwargs={"label": f"manual:{h[:12]}...", "connect": True},
            daemon=True,
        ).start()


def _do_remove():
    h = _ask("Beacon destination hash")
    if h:
        state.pool.remove(h)


def _do_strategy():
    s = _ask("Strategy  [race / fallback]").lower()
    if s in ("race", "fallback"):
        state.pool.strategy = s
        log_ok(f"Strategy → {s}")
    elif s:
        log_warn("Use 'race' or 'fallback'")


def _do_raw():
    method = _ask("RPC method  (e.g. getSlot)")
    if not method: return
    params_str = _ask("Params JSON  (blank = [])")
    try:
        params = json.loads(params_str) if params_str else []
    except json.JSONDecodeError:
        log_warn("Invalid JSON"); return
    resp = rpc_call(method, params)
    if resp:
        print(json.dumps(resp, indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
# Menu definition
# ═══════════════════════════════════════════════════════════════════════════════
# Each section: (title, [(label, handler), ...])
# To add a command: write a handler above, then add it here.

_MENU_SECTIONS: list[tuple[str, list[tuple[str, callable]]]] = [
    ("WALLET", [
        ("Generate new wallet",                   _do_generate_wallet),
        ("Import private key",                    _do_import_wallet),
        ("SOL balance",                           _do_balance),
        ("SOL balance  (confidential · Arcium)",  _do_cbalance),
        ("SPL token accounts",                    _do_tokens),
    ]),
    ("SEND", [
        ("Send SOL",                              _do_send_sol),
        ("Arcium payment  (beacon co-sign)",      _do_arcium_transfer),
    ]),
    ("DURABLE NONCE", [
        ("Create nonce account",                  _do_create_nonce),
        ("View nonce value",                      _do_get_nonce),
        ("Sign transfer with nonce",              _do_sign_nonce),
    ]),
    ("NETWORK", [
        ("Current slot",                          _do_slot),
        ("Block height",                          _do_height),
        ("Latest blockhash",                      _do_blockhash),
        ("Transaction count",                     _do_txcount),
    ]),
    ("BEACON POOL", [
        ("View pool status",                      _do_beacons),
        ("Add beacon",                            _do_add),
        ("Remove beacon",                         _do_remove),
        ("Switch dispatch strategy",              _do_strategy),
    ]),
    ("ADVANCED", [
        ("Relay raw transaction  (base64)",       _do_relay_raw),
        ("Simulate transaction",                  _do_simulate),
        ("Raw JSON-RPC call",                     _do_raw),
    ]),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Menu renderer
# ═══════════════════════════════════════════════════════════════════════════════

def _wallet_qr_lines(pubkey: str) -> list[str]:
    """Render a pubkey as terminal QR code lines. Returns [] if qrcode not installed."""
    try:
        import qrcode as _qr
        qr = _qr.QRCode(border=1, error_correction=_qr.constants.ERROR_CORRECT_L)
        qr.add_data(pubkey)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue().splitlines()
    except ImportError:
        return []


def _render_menu() -> dict[str, callable]:
    """
    Print the full menu and return {number_string: handler}.
    Called at the start of every loop iteration so beacon status stays live.
    """
    n_active  = len(state.pool.active_links())
    n_pending = state.pool.pending_count()

    if n_active > 0:
        active_col = GREEN
    elif n_pending > 0:
        active_col = YELLOW
    else:
        active_col = RED
    global _spinner_idx
    active_str = f"{active_col}●{RESET} {BOLD}{n_active}{RESET} active"
    if n_pending > 0:
        frame       = _SPINNER_FRAMES[_spinner_idx % len(_SPINNER_FRAMES)]
        _spinner_idx += 1
        pending_str = f"  {YELLOW}{frame}{RESET} {n_pending} connecting"
    else:
        pending_str = ""

    print()
    print(f"{BOLD}{CYAN}  ┌─ anon0mesh ─────────────────────────────────────────┐{RESET}")
    print(f"{BOLD}{CYAN}  │{RESET}  {active_str}{pending_str}  {DIM}·  strategy: {state.pool.strategy}{RESET}")
    print(f"{BOLD}{CYAN}  └─────────────────────────────────────────────────────┘{RESET}")

    if state.active_wallet:
        qr_lines = _wallet_qr_lines(state.active_wallet["pubkey"])
        if qr_lines:
            print()
            for line in qr_lines:
                print(f"  {line}")
        print(f"\n  {BOLD}{GREEN}{state.active_wallet['pubkey']}{RESET}")
        print(f"  {DIM}{state.active_wallet['path']}{RESET}")

    mapping: dict[str, callable] = {}
    idx = 1

    for section_title, items in _MENU_SECTIONS:
        print(f"\n  {DIM}── {section_title} {'─' * (44 - len(section_title))}{RESET}")
        for label, handler in items:
            num = str(idx)
            print(f"  {CYAN}{BOLD}{num:>2}{RESET}  {label}")
            mapping[num] = handler
            idx += 1

    print(f"\n  {DIM}────────────────────────────────────────────────{RESET}")
    print(f"  {CYAN}{BOLD} 0{RESET}  Quit    {DIM}c  Clear screen{RESET}\n")
    return mapping


# ═══════════════════════════════════════════════════════════════════════════════
# REPL loop
# ═══════════════════════════════════════════════════════════════════════════════

def _run_handler(handler: callable) -> None:
    print()
    try:
        handler()
    except KeyboardInterrupt:
        print()
        log_info("Cancelled")


def _read_choice() -> str | None:
    """Return the user's input, or None on EOF/Ctrl-C (signals exit)."""
    n   = len(state.pool.active_links())
    col = GREEN if n > 0 else RED
    try:
        return input(f"  {col}select ›{RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def repl() -> None:
    mapping = _render_menu()
    while True:
        choice = _read_choice()
        if choice is None:
            break
        if not choice:
            continue
        if choice in ("0", "q", "quit", "exit"):
            break
        if choice in ("c", "clear"):
            print("\033[2J\033[H", end="")
            mapping = _render_menu()
            continue
        if choice in ("m", "menu"):
            mapping = _render_menu()
            continue
        handler = mapping.get(choice)
        if handler:
            _run_handler(handler)
        else:
            log_warn(f"  '{choice}' is not a valid option")
