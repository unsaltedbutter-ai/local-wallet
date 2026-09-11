"""Chain subsystem: THE ONLY networked module (Esplora, Electrum, bitcoind, fees, price).

Network I/O lives exclusively here and is lint-enforced by
``tools/lint_network.py``: only ``src/localwallet/chain/**`` may import
network modules.
"""

from localwallet.chain.bitcoind import BitcoindClient
from localwallet.chain.config import BITCOIND_SCHEME, ELECTRUM_SCHEME, ChainConfig
from localwallet.chain.electrum import ElectrumClient
from localwallet.chain.esplora import (
    MAINNET_GENESIS_HASH,
    Balance,
    ChainClient,
    ChainError,
    EsploraClient,
    TipBlock,
    TxStatus,
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
    minor_per_unit,
)
from localwallet.chain.watch import (
    IncomingEvent,
    IncomingWatcher,
    WatchedTx,
    time_since_last_block,
)

__all__ = [
    "BITCOIND_SCHEME",
    "ELECTRUM_SCHEME",
    "MAINNET_GENESIS_HASH",
    "Balance",
    "BitcoindClient",
    "ChainClient",
    "ChainConfig",
    "ChainError",
    "ConfigDisabled",
    "ElectrumClient",
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
    "TxStatus",
    "WatchedTx",
    "balance_from_utxos",
    "check_backend",
    "estimate_eta",
    "minor_per_unit",
    "time_since_last_block",
]
