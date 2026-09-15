"""Store subsystem: SQLite persistence (TCK-P1-001).

Public surface: the :class:`~localwallet.store.db.Store` class (context-managed,
versioned SQLite/WAL), the error types, and the typed row records from
:mod:`localwallet.store.models`. No raw SQL outside ``store/``.
"""

from localwallet.store.db import (
    SCHEMA_VERSION,
    Store,
    StoreError,
    StoreIntegrityError,
)
from localwallet.store.models import (
    ADDRESS_ALLOCATED,
    ADDRESS_LABEL_MAX_CHARS,
    ADDRESS_UNUSED,
    ADDRESS_USED,
    BRANCH_CHANGE,
    BRANCH_RECEIVE,
    COIN_TAGS,
    DIR_IN,
    DIR_OUT,
    DIR_SELF,
    SUPERSEDED_EVICTED,
    SUPERSEDED_REPLACED,
    AddressRecord,
    AddressRegistryRecord,
    DerivationRecord,
    SettingRecord,
    TxRecord,
    UtxoRecord,
    WalletRecord,
    superseded_states,
)

__all__ = [
    "ADDRESS_ALLOCATED",
    "ADDRESS_LABEL_MAX_CHARS",
    "ADDRESS_UNUSED",
    "ADDRESS_USED",
    "BRANCH_CHANGE",
    "BRANCH_RECEIVE",
    "COIN_TAGS",
    "DIR_IN",
    "DIR_OUT",
    "DIR_SELF",
    "SCHEMA_VERSION",
    "SUPERSEDED_EVICTED",
    "SUPERSEDED_REPLACED",
    "AddressRecord",
    "AddressRegistryRecord",
    "DerivationRecord",
    "SettingRecord",
    "Store",
    "StoreError",
    "StoreIntegrityError",
    "TxRecord",
    "UtxoRecord",
    "WalletRecord",
    "superseded_states",
]
