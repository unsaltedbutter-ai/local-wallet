"""TEST-ONLY wallet-shape chain double (TCK-DESCOPE-M4).

Production's wallet backends are exactly Electrum and bitcoind; the
Esplora client's wallet-data paths (address txs/utxo, tip block, tx
status) were DELETED with this ticket. App/scan/watch/e2e-level tests
still need a stand-in that satisfies the :class:`ChainClient` wallet
surface while their existing ``httpx.MockTransport`` fixtures keep
serving the CANONICAL translated payload shapes (the "Esplora shape"
electrum/bitcoind both translate into), with request recording intact.

This class is test scaffolding, NOT a re-live of the deleted production
path: it lives only under ``tests/``, nothing in ``src/`` imports it,
and it deliberately skips the production path's hardening (its guards
exist so a mis-scripted fixture fails loudly, not to re-pin deleted
behavior — those pins retired with the code). The live adapters keep
their own full suites (``test_chain_electrum.py`` / ``test_chain_bitcoind.py``).
"""

from __future__ import annotations

from typing import Any

from localwallet.chain.esplora import ChainError, EsploraClient, TxStatus


class WalletShapeClient(EsploraClient):
    """Wallet-surface double over the surviving public-info transport.

    Reuses the real client's retry/parse plumbing (``_request_json``) and
    serves the canonical translated payload shape the live wallet backends
    emit for the address-txs/utxos and tx-status surfaces. Broadcast, tip
    height, ``get_json`` and ``supports_price`` are the inherited
    public-info surface (unchanged production code). Deliberately NOT a
    full ``ChainClient`` (no ``estimate_fee``) — the shared-surface tests
    it scaffolds never need one.
    """

    def get_address_txs(self, address: str) -> list[dict[str, Any]]:
        payload = self._request_json("address-txs", f"/address/{address}/txs")
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise ChainError("address-txs response was not a list of objects")
        return payload

    def get_address_utxos(self, address: str) -> list[dict[str, Any]]:
        payload = self._request_json("address-utxos", f"/address/{address}/utxo")
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise ChainError("address-utxos response was not a list of objects")
        return payload

    def get_tx_status(self, txid: str) -> TxStatus:
        payload = self._request_json("tx-status", f"/tx/{txid}/status")
        if not isinstance(payload, dict):
            raise ChainError("tx-status response was not an object")
        confirmed = payload.get("confirmed")
        if not isinstance(confirmed, bool):
            raise ChainError("tx-status response has missing or non-boolean 'confirmed'")
        parsed: list[int | None] = []
        for name in ("block_height", "block_time"):
            value = payload.get(name)
            if value is None:
                parsed.append(None)
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ChainError(f"tx-status response has invalid '{name}'")
            else:
                parsed.append(value)
        return TxStatus(
            txid=txid,
            confirmed=confirmed,
            block_height=parsed[0],
            block_time=parsed[1],
        )
