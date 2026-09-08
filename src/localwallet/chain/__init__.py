"""Chain subsystem: THE ONLY networked module (Esplora, fees, price).

Network I/O lives exclusively here and is lint-enforced by
``tools/lint_network.py``: only ``src/localwallet/chain/**`` may import
network modules.
"""

from localwallet.chain.config import ChainConfig
from localwallet.chain.esplora import (
    MAINNET_GENESIS_HASH,
    Balance,
    ChainError,
    EsploraClient,
    TipBlock,
    balance_from_utxos,
    check_backend,
)
from localwallet.chain.eta import EtaEstimate, estimate_eta
from localwallet.chain.fees import FeeEstimate, FeeEstimator, FeeSource, FeeTarget
from localwallet.chain.price import (
    ConfigDisabled,
    PriceOracle,
    PriceUnavailableError,
    Rate,
)
from localwallet.chain.watch import (
    IncomingEvent,
    IncomingWatcher,
    WatchedTx,
    time_since_last_block,
)

__all__ = [
    "MAINNET_GENESIS_HASH",
    "Balance",
    "ChainConfig",
    "ChainError",
    "ConfigDisabled",
    "EsploraClient",
    "EtaEstimate",
    "FeeEstimate",
    "FeeEstimator",
    "FeeSource",
    "FeeTarget",
    "IncomingEvent",
    "IncomingWatcher",
    "PriceOracle",
    "PriceUnavailableError",
    "Rate",
    "TipBlock",
    "WatchedTx",
    "balance_from_utxos",
    "check_backend",
    "estimate_eta",
    "time_since_last_block",
]
