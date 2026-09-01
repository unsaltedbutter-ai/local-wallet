"""Wallet subsystem: descriptors, derivation, scanning, cache (TCK-P1-002).

Public surface:

- :mod:`localwallet.wallet.descriptor` — SLIP-132 watch-key parsing with
  the testnet gate enforced at parse, prefix→script-type detection, and
  the :class:`WalletDescriptor` model (canonical checksummed output
  descriptor).
- :mod:`localwallet.wallet.derivation` — batched receive/change address
  derivation.
- :mod:`localwallet.wallet.scan` — gap-limited chain scan, UTXO
  snapshot, history cache, and full rescan, persisted via the store.
"""

from localwallet.wallet.derivation import (
    BranchDeriver,
    DerivedAddress,
    derive_addresses,
    derive_receive_addresses,
)
from localwallet.wallet.descriptor import (
    ParsedKey,
    PrefixInfo,
    WalletDescriptor,
    WatchKeyError,
    detect_script_type,
    parse_wallet_key,
    parse_watch_key,
)
from localwallet.wallet.scan import (
    BRANCHES,
    DEFAULT_GAP_LIMIT,
    GAP_LIMIT_SETTING,
    OUT_OF_WINDOW_KEY,
    BranchScanSummary,
    ScanError,
    ScanSummary,
    rescan_wallet,
    scan_wallet,
)

__all__ = [
    "BRANCHES",
    "DEFAULT_GAP_LIMIT",
    "GAP_LIMIT_SETTING",
    "OUT_OF_WINDOW_KEY",
    "BranchDeriver",
    "BranchScanSummary",
    "DerivedAddress",
    "ParsedKey",
    "PrefixInfo",
    "ScanError",
    "ScanSummary",
    "WalletDescriptor",
    "WatchKeyError",
    "derive_addresses",
    "derive_receive_addresses",
    "detect_script_type",
    "parse_wallet_key",
    "parse_watch_key",
    "rescan_wallet",
    "scan_wallet",
]
