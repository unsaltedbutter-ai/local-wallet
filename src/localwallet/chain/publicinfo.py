"""The standalone PUBLIC-INFO fetcher — mempool.space for fees + prices only.

TCK-DESCOPE-M3A (USER REDIRECTION 2026-09-11; ADR-0003/0011 amendments):
wallet information comes ONLY from an Electrum or bitcoind backend. This
module keeps the ONE role mempool.space still plays — reading the PUBLIC
aggregations the fee floor-follower and the price oracle already pinned
against since FEE-001/TCK-P2-001:

* ``GET {base}/v1/fees/recommended`` (via :meth:`get_json`),
* ``GET {base}/v1/fees/mempool-blocks`` (via :meth:`get_json`),
* ``GET {base}/blocks/tip/height`` (via :meth:`get_tip_height` — the
  recent-blocks floor's tip, self-contained: it rides THIS host, never the
  wallet backend),
* ``GET {base}/v1/blocks/{tip}`` and ``GET {base}/v1/prices`` (via
  :meth:`get_json`).

There are deliberately NO wallet methods here (no address/UTXO/history/
broadcast/tx_status) and no wallet acceptance anywhere: the payloads carry
no addresses, so a request discloses only the app's IP and timing to the
public mempool.space operator (the honest ADR-0011 amendment note).

The base defaults to :attr:`localwallet.config.Settings.esplora_base_url`
(``https://mempool.space/api``, env/config-overridable via
``LOCALWALLET_ESPLORA_BASE_URL``) and is constructed ONCE per session —
independent of the wallet backend, never rebuilt on a hot-swap. Transport,
retry/backoff and error-scrubbing are the :class:`EsploraClient`'s own
(bounded retries on 429/5xx/connection errors; everything else fails closed
as a value-free :class:`~localwallet.chain.esplora.ChainError`).
"""

from __future__ import annotations

from typing import Any

import httpx

from localwallet.chain.esplora import EsploraClient
from localwallet.config import Settings

__all__ = ["PublicInfoClient"]


class PublicInfoClient:
    """Read-only public fee/price fetcher over the mempool.space API.

    Structurally satisfies exactly what ``chain.fees`` and ``chain.price``
    consume off the old combined client: ``get_json``, ``get_tip_height``
    and the ``supports_price`` capability flag. Args mirror
    :class:`EsploraClient` so tests can inject a mock ``transport``.

    Raises:
        ValueError: If the resolved base URL is malformed (fail closed at
            construction, the same discipline as every chain config).
    """

    #: The public payload carries a price feed (capability seam unchanged).
    supports_price: bool = True

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        settings = settings if settings is not None else Settings.from_env()
        base = base_url if base_url is not None else settings.esplora_base_url
        self._client = EsploraClient(
            base_url=base,
            timeout_s=settings.request_timeout_s,
            max_retries=settings.max_retries,
            transport=transport,
        )

    def get_json(self, path: str, kind: str) -> Any:
        """Raw GET + retry + parse for one PUBLIC path (see the module
        docstring for the endpoints the fee/price consumers pass here).
        Same contract as :meth:`EsploraClient.get_json`: kind-named,
        address-free :class:`ChainError` on any failure."""
        return self._client.get_json(path, kind)

    def get_tip_height(self) -> int:
        """The public tip height (the recent-blocks floor's anchor for
        ``/v1/blocks/{tip}`` — self-contained, never the wallet backend's
        tip)."""
        return self._client.get_tip_height()

    def close(self) -> None:
        """Close the underlying HTTP transport (bounded, local)."""
        self._client.close()
