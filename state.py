"""
state.py — shared mutable runtime state
========================================
All globals that main() initialises and other modules read.
Import this module and access attributes directly: state.pool, state.active_wallet, etc.
"""
from shared import RNS_REQUEST_TIMEOUT

pool:            object       = None   # BeaconPool instance
client_identity: object       = None   # RNS.Identity instance
identify_self:   bool         = True
request_timeout: float        = float(RNS_REQUEST_TIMEOUT)
active_wallet:   dict | None  = None   # {"pubkey": str, "path": str}
