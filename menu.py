"""
menu.py — interactive menu REPL
=================================
To add a command:
  1. Write a _do_<name>() function.
  2. Add an entry to MENU at the bottom.
"""

import io
import sys
import json
import threading

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

_RELAY_PROMPT      = "Relay now? [y/N]"
_MAX_RETRIES       = 3
_NEED_SOLDERS      = "Requires: pip install solders"
_PROMPT_TO         = "Recipient address"
_PROMPT_AMOUNT     = "Amount  (SOL, e.g. 0.5)"
_PROMPT_NONCE_ACCT = "Nonce account pubkey"
_INVALID_AMOUNT    = "Invalid amount"
_NO_WALLET         = "No wallet loaded — use WALLET › Generate or Import first"

_W              = 56       # visible width of section fill
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_spinner_idx    = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Input helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ask(prompt: str) -> str:
    try:
        return input(f"  {CYAN}›{RESET} {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _pick(prompt: str, options: list[str]) -> int | None:
    """Numbered list picker. Auto-selects when only one option. Returns 0-based index."""
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
    """Overwrites a single terminal line with a spinning status indicator."""
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str):
        self._label  = label
        self._idx    = 0

    def __enter__(self):
        set_quiet(True)
        self._draw()
        return self

    def tick(self, label: str | None = None):
        if label:
            self._label = label
        self._draw()

    def _draw(self):
        frame = self._FRAMES[self._idx % len(self._FRAMES)]
        self._idx += 1
        sys.stdout.write(f"\r  {YELLOW}{frame}{RESET} {self._label}   ")
        sys.stdout.flush()

    def done(self, msg: str, ok: bool = True):
        col = GREEN if ok else RED
        sym = "✔" if ok else "✘"
        sys.stdout.write(f"\r  {col}{BOLD}{sym}{RESET} {msg}\n")
        sys.stdout.flush()

    def __exit__(self, *_):
        set_quiet(False)


# ═══════════════════════════════════════════════════════════════════════════════
# Action handlers
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


def _copy_to_clipboard(text: str) -> bool:
    """Try xclip → xsel → wl-copy → pyperclip. Returns True on success."""
    import subprocess
    for cmd in (
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["wl-copy"],
    ):
        try:
            subprocess.run(cmd, input=text, text=True, check=True,
                           capture_output=True, timeout=3)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            continue
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def _do_copy_pubkey():
    if not state.active_wallet:
        log_warn(_NO_WALLET); return
    pk = state.active_wallet["pubkey"]
    if _copy_to_clipboard(pk):
        log_ok(f"Copied  {pk}")
    else:
        print(f"\n  {BOLD}{GREEN}{pk}{RESET}\n")
        log_warn("Clipboard unavailable — key printed above (install xclip or pyperclip)")


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


# ── Send ───────────────────────────────────────────────────────────────────────

def _fetch_balance_sol(pubkey: str) -> float | None:
    resp = rpc_call("getBalance", [pubkey])
    if not (resp and "result" in resp):
        return None
    lamps = resp["result"]
    if isinstance(lamps, dict):
        lamps = lamps.get("value", 0)
    return lamps / 1_000_000_000


def _select_nonce_account() -> str | None:
    """Pick a nonce account with live SOL balance shown for each candidate."""
    import concurrent.futures

    found = scan_nonce_accounts()
    if not found:
        return _ask(_PROMPT_NONCE_ACCT) or None

    with _Spinner("Fetching balances…") as sp:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(found)) as ex:
            futs = {ex.submit(_fetch_balance_sol, n["pubkey"]): n["pubkey"] for n in found}
            bals = {pk: fut.result() for fut, pk in
                    ((f, futs[f]) for f in concurrent.futures.as_completed(futs))}
        sp.done(f"{len(found)} nonce account(s)")

    labels = []
    for n in found:
        bal = bals.get(n["pubkey"])
        bal_str = (f"  {GREEN}{bal:.9f} SOL{RESET}" if bal is not None
                   else f"  {DIM}balance unknown{RESET}")
        labels.append(
            f"{n['pubkey'][:8]}…{n['pubkey'][-4:]}  {DIM}{n['path']}{RESET}{bal_str}"
        )

    idx = _pick("Select nonce account", labels)
    return found[idx]["pubkey"] if idx is not None else None


def _do_send_sol():
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
    if tx_b64:
        _broadcast_with_retry(tx_b64)


def _broadcast_with_retry(tx_b64: str) -> None:
    with _Spinner("Broadcasting…") as sp:
        for attempt in range(1, _MAX_RETRIES + 1):
            if attempt > 1:
                sp.tick(f"Retry {attempt}/{_MAX_RETRIES}…")
            resp = rpc_call("sendTransaction", [tx_b64, {"encoding": "base64"}])
            if resp and "result" in resp:
                sp.done(f"Confirmed  sig: {resp['result'][:20]}…")
                print(f"\n  {GREEN}{BOLD}Signature:{RESET} {resp['result']}\n")
                return
            err = resp.get("error", {}).get("message", "no response") if resp else "no response"
            sp.tick(f"Attempt {attempt} failed: {err[:50]}")
        sp.done("All broadcast attempts failed", ok=False)


def _do_arcium_transfer():
    if not HAS_SOLDERS:
        log_err(_NEED_SOLDERS); return
    if not state.active_wallet:
        log_warn(_NO_WALLET); return

    kp_path = state.active_wallet["path"]
    pubkey  = state.active_wallet["pubkey"]

    print(f"\n  {BOLD}From:{RESET} {GREEN}{pubkey}{RESET}")
    get_balance(pubkey)

    nonce = _select_nonce_account()
    if not nonce: return
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

    log_info("Fetching beacon co-signing pubkey…")
    beacon_pk = get_beacon_pubkey()
    if not beacon_pk:
        log_err("Could not get beacon pubkey — is ARCIUM_PAYER_KEYPAIR set on the beacon?")
        return
    log_ok(f"Beacon: {beacon_pk}")

    # SPL token context required for the Arcium execute_payment computation.
    # Without these the beacon can relay the tx but won't trigger Arcium MPC.
    print(f"\n  {DIM}Arcium stats — SPL token context (required for MPC logging){RESET}")
    mint     = _ask("SPL token mint")
    payer_ta = _ask("Payer token account") if mint else None
    recip_ta = _ask("Recipient token account") if mint else None

    partial_tx = partial_sign_arcium_transfer(kp_path, beacon_pk, nonce, to, lamports)
    if partial_tx:
        arcium_meta = None
        if mint and payer_ta and recip_ta:
            arcium_meta = {
                "amount":       lamports,
                "mint":         mint,
                "payer_ta":     payer_ta,
                "recipient":    to,
                "recipient_ta": recip_ta,
            }
        cosign_and_send(partial_tx, arcium_meta)


# ── Durable nonce ──────────────────────────────────────────────────────────────

def _do_create_nonce():
    if not state.active_wallet:
        log_warn(_NO_WALLET); return
    nonce_kp = _ask("Nonce keypair JSON path  (blank = generate new)")
    create_nonce_account(state.active_wallet["path"], nonce_kp or None, None)


def _do_get_nonce():
    pubkey = _select_nonce_account()
    if pubkey:
        get_nonce_account(pubkey)


def _do_sign_nonce():
    kp    = _ask("Payer keypair JSON path")
    if not kp: return
    nonce = _select_nonce_account()
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
    nval   = _ask("Nonce value  (blank = fetch from chain)")
    tx_b64 = offline_sign_nonce_transfer(kp, nonce, auth or kp, to, lamps, nval or None)
    if tx_b64 and _ask(_RELAY_PROMPT).lower() == "y":
        send_transaction(tx_b64)


# ── Beacon pool ────────────────────────────────────────────────────────────────

def _do_beacons():
    print(state.pool.status_table())


def _do_add():
    h = _ask("Beacon destination hash")
    if h:
        threading.Thread(
            target=state.pool.add,
            args=(h,),
            kwargs={"label": f"manual:{h[:12]}…", "connect": True},
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


# ── Advanced ───────────────────────────────────────────────────────────────────

def _do_relay_raw():
    tx = _ask("Signed transaction (base64)")
    if tx:
        send_transaction(tx)


def _do_simulate():
    tx = _ask("Signed transaction (base64)")
    if tx:
        simulate_transaction(tx)


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
# Menu definition  —  flat array, one dict per item
_SEC_WALLET  = "WALLET"
_SEC_SEND    = "SEND"
_SEC_NONCE   = "DURABLE NONCE"
_SEC_NETWORK = "NETWORK"
_SEC_BEACON  = "BEACON POOL"
_SEC_ADV     = "ADVANCED"

# Flat array — one dict per item. Keys: section (str), label (str), fn (callable).
# Numbers are auto-assigned in display order; add/remove items freely.
MENU: list[dict] = [
    {"section": _SEC_WALLET,  "label": "Generate new wallet",                  "fn": _do_generate_wallet},
    {"section": _SEC_WALLET,  "label": "Import private key",                   "fn": _do_import_wallet},
    {"section": _SEC_WALLET,  "label": "Copy public key",                      "fn": _do_copy_pubkey},
    {"section": _SEC_WALLET,  "label": "SOL balance",                          "fn": _do_balance},
    {"section": _SEC_WALLET,  "label": "SOL balance  (confidential · Arcium)", "fn": _do_cbalance},
    {"section": _SEC_WALLET,  "label": "SPL token accounts",                   "fn": _do_tokens},
    {"section": _SEC_SEND,    "label": "Send SOL",                             "fn": _do_send_sol},
    {"section": _SEC_SEND,    "label": "Arcium payment  (beacon co-sign)",     "fn": _do_arcium_transfer},
    {"section": _SEC_NONCE,   "label": "Create nonce account",                 "fn": _do_create_nonce},
    {"section": _SEC_NONCE,   "label": "View nonce value",                     "fn": _do_get_nonce},
    {"section": _SEC_NONCE,   "label": "Sign transfer with nonce",             "fn": _do_sign_nonce},
    {"section": _SEC_NETWORK, "label": "Current slot",                         "fn": _do_slot},
    {"section": _SEC_NETWORK, "label": "Block height",                         "fn": _do_height},
    {"section": _SEC_NETWORK, "label": "Latest blockhash",                     "fn": _do_blockhash},
    {"section": _SEC_NETWORK, "label": "Transaction count",                    "fn": _do_txcount},
    {"section": _SEC_BEACON,  "label": "View pool status",                     "fn": _do_beacons},
    {"section": _SEC_BEACON,  "label": "Add beacon",                           "fn": _do_add},
    {"section": _SEC_BEACON,  "label": "Remove beacon",                        "fn": _do_remove},
    {"section": _SEC_BEACON,  "label": "Switch dispatch strategy",             "fn": _do_strategy},
    {"section": _SEC_ADV,     "label": "Relay raw transaction  (base64)",      "fn": _do_relay_raw},
    {"section": _SEC_ADV,     "label": "Simulate transaction",                 "fn": _do_simulate},
    {"section": _SEC_ADV,     "label": "Raw JSON-RPC call",                    "fn": _do_raw},
]


# Renderer

def _wallet_qr_lines(pubkey: str) -> list[str]:
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


def _render_header() -> None:
    global _spinner_idx
    n_active  = len(state.pool.active_links())
    n_pending = state.pool.pending_count()

    if n_active > 0:
        dot_col = GREEN
    elif n_pending > 0:
        dot_col = YELLOW
    else:
        dot_col = RED

    beacon_str = f"{dot_col}●{RESET} {BOLD}{n_active}{RESET} active"
    if n_pending > 0:
        frame       = _SPINNER_FRAMES[_spinner_idx % len(_SPINNER_FRAMES)]
        _spinner_idx += 1
        beacon_str += f"  {YELLOW}{frame}{RESET} {n_pending} connecting"
    beacon_str += f"  {DIM}·  {state.pool.strategy}{RESET}"

    bar = f"  {CYAN}{'═' * (_W + 2)}{RESET}"
    print(f"\n{bar}")
    print(f"  {BOLD}anon0mesh{RESET}  {DIM}mesh-first solana rpc{RESET}")
    print(f"  {beacon_str}")

    if state.active_wallet:
        pk    = state.active_wallet["pubkey"]
        short = f"{pk[:8]}…{pk[-8:]}"
        path  = state.active_wallet["path"]
        print(f"  {GREEN}◆{RESET}  {BOLD}{short}{RESET}  {DIM}{path}{RESET}")

    print(bar)

    if state.active_wallet:
        qr = _wallet_qr_lines(state.active_wallet["pubkey"])
        if qr:
            print()
            for line in qr:
                print(f"  {line}")


def _section_header(title: str) -> None:
    fill = "─" * (_W - len(title) - 1)
    print(f"\n  {BOLD}{title}{RESET}  {DIM}{fill}{RESET}")


def _render_menu() -> dict[str, callable]:
    _render_header()

    mapping: dict[str, callable] = {}
    current_section = None

    for num, item in enumerate(MENU, 1):
        if item["section"] != current_section:
            current_section = item["section"]
            _section_header(current_section)

        key = str(num)
        print(f"  {CYAN}{BOLD}{num:>3}{RESET}  {item['label']}")
        mapping[key] = item["fn"]

    print(f"\n  {DIM}{'─' * (_W + 2)}{RESET}")
    print(f"  {CYAN}{BOLD}  0{RESET}  Quit"
          f"   {DIM}m  Refresh menu   c  Clear screen{RESET}\n")
    return mapping


# REPL

def _run_handler(handler: callable) -> None:
    print()
    try:
        handler()
    except KeyboardInterrupt:
        print()
        log_info("Cancelled")


def _read_choice() -> str | None:
    n   = len(state.pool.active_links())
    col = GREEN if n > 0 else RED
    try:
        return input(f"  {col}›{RESET} ").strip()
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
            log_warn(f"'{choice}' is not a valid option")
