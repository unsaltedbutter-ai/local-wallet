"""DEPRECATED Phase 0 shim — superseded by descriptor.py / derivation.py.

**Remove this module in TCK-P1-004.** It exists only so the Phase 0
imports (``localwallet.app`` and ``tests/test_e2e_skeleton.py``) keep
working unchanged while the wallet engine lands (TCK-P1-002). All names
are plain re-exports with identical semantics:

- :func:`parse_watch_key` — now lives in
  :mod:`localwallet.wallet.descriptor` (same detect-only default; the
  Phase 1 testnet gate is available via ``require_testnet=True`` and is
  always on for the wallet engine through
  :func:`localwallet.wallet.descriptor.parse_wallet_key`).
- :func:`derive_receive_addresses` — now lives in
  :mod:`localwallet.wallet.derivation` (same signature, messages, and
  derive-side testnet gate).
- :class:`ParsedKey` / :class:`WatchKeyError` — moved to
  :mod:`localwallet.wallet.descriptor`.

Caveat: the re-exports are semantically equivalent for real SLIP-132 keys,
with two microscopic deltas versus the Phase 0 implementations — a stricter
prefix/version cross-check (a key whose version bytes disagree with its
string prefix is now refused at parse time, fail closed) and a changed
validation order for doubly-invalid derive arguments (which of two
value-free errors surfaces first may differ).

No new functionality may be added here.
"""

from localwallet.wallet.derivation import derive_receive_addresses
from localwallet.wallet.descriptor import (
    ParsedKey,
    WatchKeyError,
    parse_watch_key,
)

__all__ = [
    "ParsedKey",
    "WatchKeyError",
    "derive_receive_addresses",
    "parse_watch_key",
]
