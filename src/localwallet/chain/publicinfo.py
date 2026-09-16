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
  :meth:`get_json`),
* ``POST {base}/tx`` (via :meth:`broadcast_tx` — the ONE write, only ever
  reached through the app's consented public-broadcast fallback,
  TCK-PUBLICBCAST-001).

There are deliberately NO wallet READ methods here (no address/UTXO/
history/tx_status) and no wallet acceptance anywhere: the GET payloads
carry no addresses, so a request discloses only the app's IP and timing
to the public mempool.space operator (the honest ADR-0011 amendment note).

ONE write is the sanctioned exception (TCK-PUBLICBCAST-001):
:meth:`PublicInfoClient.broadcast_tx` — ``POST {base}/tx`` (the endpoint
mempool.space's own broadcast form posts to), reached ONLY by the app's
public-broadcast fallback AFTER the dispatcher's deterministic
explicit-consent gate. Unlike the GETs, a broadcast discloses the FULL
signed transaction (inputs, outputs — the input-clustering exposure) to
the public operator; that disclosure is exactly what the consent sentence
names. It is single-attempt with the SEC-004 txid bind — the inherited
:class:`EsploraClient` semantics reused verbatim (see
:meth:`EsploraClient.broadcast_tx`).

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
    """Public fee/price fetcher + the consented public-broadcast POST.

    Structurally satisfies exactly what ``chain.fees`` and ``chain.price``
    consume off the old combined client: ``get_json``, ``get_tip_height``
    and the ``supports_price`` capability flag — plus the single
    TCK-PUBLICBCAST-001 write (:meth:`broadcast_tx`, which the fee/price
    consumers never touch). Args mirror
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

    def broadcast_tx(self, tx_hex: str) -> str:
        """Broadcast a signed transaction to the PUBLIC mempool
        (``POST {base}/tx``, TCK-PUBLICBCAST-001) — the ONE write.

        Delegates verbatim to :meth:`EsploraClient.broadcast_tx`, which
        owns the whole money-path discipline this inherits and must not
        re-implement: tx-hex validation BEFORE anything is sent, the
        single-attempt no-retry policy (a POST is not idempotent —
        double-broadcast risk), the SEC-004 TXID BIND (the answer is
        re-validated 64-lowercase-hex and bound to the expected txid
        computed as ``sha256d`` of this transaction's serialization —
        embit's witness-stripped ``Transaction.txid()`` — a well-formed
        foreign txid is a value-free hard stop), and value-free errors
        (no tx hex, no txid, no server text ever surface).

        The caller (the app's broadcast-failure fallback) reaches this ONLY
        through its deterministic explicit-consent gate — this client
        carries no wallet state and no gate; it is the transport half of
        that flow. The request hands the full transaction to the public
        operator (the disclosure the consent sentence names verbatim).

        Raises:
            ChainError: everything :meth:`EsploraClient.broadcast_tx`
                raises — malformed argument/answer, bind mismatch, any
                non-2xx or transport failure of the single attempt.
        """
        return self._client.broadcast_tx(tx_hex)

    def close(self) -> None:
        """Close the underlying HTTP transport (bounded, local)."""
        self._client.close()
