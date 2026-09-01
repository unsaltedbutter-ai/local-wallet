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
    ADDRESS_UNUSED,
    ADDRESS_USED,
    BRANCH_CHANGE,
    BRANCH_RECEIVE,
    DIR_IN,
    DIR_OUT,
    DIR_SELF,
    AddressRecord,
    DerivationRecord,
    SettingRecord,
    TxRecord,
    UtxoRecord,
    WalletRecord,
)

__all__ = [
    "ADDRESS_ALLOCATED",
    "ADDRESS_UNUSED",
    "ADDRESS_USED",
    "BRANCH_CHANGE",
    "BRANCH_RECEIVE",
    "DIR_IN",
    "DIR_OUT",
    "DIR_SELF",
    "SCHEMA_VERSION",
    "AddressRecord",
    "DerivationRecord",
    "SettingRecord",
    "Store",
    "StoreError",
    "StoreIntegrityError",
    "TxRecord",
    "UtxoRecord",
    "WalletRecord",
]
