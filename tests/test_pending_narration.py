"""TCK-PENDING-001: "what's pending?" — the get_utxos pending block.

Unconfirmed receives (from the cached UTXO snapshot) and broadcast-but-
unconfirmed spends (from the cached transactions table) are narrated as
ONE compact tool-owned block on the get_utxos answer: counts and the
incoming sat total verbatim from the store, the static no-ETA honesty
line (the store records no fee target for cached transactions ⇒ NEVER a
fabricated probability or minute figure), and NO age line (the store has
no first-seen timestamps — narrated from what IS stored, the documented
bound). Keys are strictly additive: a clean wallet's result shape and
narration are byte-identical to before. Freshness interplay (ADR-0022):
during the first scan the block still prints (partial-but-verbatim
figures) under the existing stale note.

Also pinned here: the prompt phrasing map (in tests/test_agent_prompt_-
context.py) and the stub-model routing of the utxo-count / pending
phrasings → get_utxos (the MW-11 #3 empty-turn class, deterministic
layer).
"""

from __future__ import annotations

import json

from localwallet.app import (
    FRESHNESS_NOTE,
    PENDING_NO_ETA_NOTE,
    _make_get_utxos_handler,
    _print_utxos,
    stub_generate,
)
from localwallet.protocol import Envelope, validate_payload
from localwallet.store import DIR_IN, DIR_OUT, DIR_SELF, Store, TxRecord, UtxoRecord
from localwallet.wallet import scan as wallet_scan
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB, derive_fixture_addresses

GET_UTXOS_JSON = '{"v": 0, "intent": "get_utxos", "params": {}}'


def _store_with_wallet() -> tuple[Store, int]:
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    return store, wallet.id


def _utxo(wallet_id: int, txid: str, vout: int, address: str, sats: int, confirmed: int) -> UtxoRecord:
    return UtxoRecord(
        wallet_id=wallet_id,
        txid=txid,
        vout=vout,
        address=address,
        value_sats=sats,
        confirmed=confirmed,
        height=800_000 if confirmed == 1 else None,
    )


def _tx(wallet_id: int, txid: str, direction: str, height: int | None, fee: int | None = None) -> TxRecord:
    return TxRecord(
        wallet_id=wallet_id,
        txid=txid,
        height=height,
        block_time=None if height is None else 1_700_000_000,
        fee_sats=fee,
        direction=direction,
        raw_summary=None,
    )


def _ask(store: Store, wallet_id: int, *, completed_scan: bool = False) -> dict[str, object]:
    if completed_scan:
        store.set_sync_state(wallet_id, wallet_scan.CURSOR_KEY, "[]")
    handler = _make_get_utxos_handler(store, wallet_id)
    envelope: Envelope = validate_payload(GET_UTXOS_JSON)
    result = handler(envelope)
    assert "error" not in result
    return result


def _narrate(result: dict[str, object]) -> list[str]:
    outputs: list[str] = []
    _print_utxos(result, outputs.append)
    return outputs


class TestCleanWalletUnchanged:
    def test_result_shape_byte_identical(self) -> None:
        store, wallet_id = _store_with_wallet()
        addr0, _addr1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wallet_id,
            [_utxo(wallet_id, "a" * 64, 0, addr0, 50_000, confirmed=1)],
        )
        result = _ask(store, wallet_id, completed_scan=True)
        # Additive-only: NO pending keys on a clean wallet.
        assert set(result) == {"utxos", "count", "freshness"}
        assert result["freshness"] == "fresh"

    def test_narration_grows_no_pending_line(self) -> None:
        store, wallet_id = _store_with_wallet()
        addr0, _addr1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wallet_id,
            [_utxo(wallet_id, "a" * 64, 0, addr0, 50_000, confirmed=1)],
        )
        lines = _narrate(_ask(store, wallet_id, completed_scan=True))
        assert not any("Pending" in line for line in lines)
        assert not any(PENDING_NO_ETA_NOTE in line for line in lines)

    def test_empty_store_still_no_unspent_outputs(self) -> None:
        store, wallet_id = _store_with_wallet()
        lines = _narrate(_ask(store, wallet_id))
        assert "No unspent outputs." in lines


class TestIncomingPending:
    def _fund(self, store: Store, wallet_id: int) -> None:
        addr0, addr1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wallet_id,
            [
                _utxo(wallet_id, "a" * 64, 0, addr0, 50_000, confirmed=1),
                _utxo(wallet_id, "b" * 64, 1, addr1, 12_345, confirmed=0),
                _utxo(wallet_id, "c" * 64, 0, addr1, 100, confirmed=0),
            ],
        )

    def test_count_and_verbatim_sat_total(self) -> None:
        store, wallet_id = _store_with_wallet()
        self._fund(store, wallet_id)
        result = _ask(store, wallet_id, completed_scan=True)
        assert result["pending_incoming_count"] == 2
        assert result["pending_incoming_sats"] == 12_345 + 100
        assert result["pending_outgoing_count"] == 0
        assert result["pending_eta_note"] == PENDING_NO_ETA_NOTE

    def test_narration_one_compact_block(self) -> None:
        store, wallet_id = _store_with_wallet()
        self._fund(store, wallet_id)
        addr0, addr1 = derive_fixture_addresses(2)
        lines = _narrate(_ask(store, wallet_id, completed_scan=True))
        assert f"Pending: 2 incoming for {12_345 + 100} sats" in lines
        assert PENDING_NO_ETA_NOTE in lines
        # one compact block: exactly two block lines, no /details verbosity
        pending_lines = [ln for ln in lines if ln.startswith("Pending: ")]
        assert len(pending_lines) == 1
        # the block lines are address-free (amounts only; per-UTXO lines
        # already carry the addresses verbatim where they belong)
        block = [ln for ln in lines if ln.startswith("Pending: ") or ln == PENDING_NO_ETA_NOTE]
        assert all(addr0 not in ln and addr1 not in ln for ln in block)
        # no fabricated minute figure anywhere in the block: the existing
        # ladder's wording markers ("~N-M min") must never appear here
        assert all(" min" not in ln and "~" not in ln for ln in block)


class TestOutgoingPending:
    def test_broadcast_unconfirmed_tx_counted(self) -> None:
        store, wallet_id = _store_with_wallet()
        addr0, addr1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wallet_id,
            [
                _utxo(wallet_id, "a" * 64, 0, addr0, 50_000, confirmed=1),
                # our own change from the pending spend — must NOT read
                # as an incoming payment (txid join, pure store data)
                _utxo(wallet_id, "f" * 64, 1, addr1, 30_000, confirmed=0),
            ],
        )
        store.upsert_txs(
            [
                _tx(wallet_id, "f" * 64, DIR_OUT, None, fee=1000),
                _tx(wallet_id, "e" * 64, DIR_SELF, None),
                _tx(wallet_id, "d" * 64, DIR_IN, None),  # incoming, not ours to count as outgoing
                _tx(wallet_id, "a" * 64, DIR_OUT, 800_000),  # confirmed — not pending
            ]
        )
        result = _ask(store, wallet_id, completed_scan=True)
        assert result["pending_outgoing_count"] == 2
        assert result["pending_incoming_count"] == 0  # the 30k change coin excluded
        assert result["pending_incoming_sats"] == 0

    def test_narration_states_the_amount_bound(self) -> None:
        store, wallet_id = _store_with_wallet()
        store.upsert_txs([_tx(wallet_id, "f" * 64, DIR_OUT, None, fee=1000)])
        lines = _narrate(_ask(store, wallet_id, completed_scan=True))
        assert "Pending: 1 outgoing (amount not recorded)" in lines
        assert PENDING_NO_ETA_NOTE in lines
        # honest degrade, never a made-up figure: the note carries no digits
        assert not any(ch.isdigit() for ch in PENDING_NO_ETA_NOTE)


class TestFreshnessInterplay:
    def test_stale_answer_still_carries_verbatim_pending_block(self) -> None:
        store, wallet_id = _store_with_wallet()
        addr0, addr1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wallet_id, [_utxo(wallet_id, "b" * 64, 1, addr1, 12_345, confirmed=0)]
        )
        del addr0
        result = _ask(store, wallet_id)  # no cursor → stale (first scan incomplete)
        assert result["freshness"] == "stale"
        assert result["pending_incoming_sats"] == 12_345  # verbatim, not hidden
        lines = _narrate(result)
        assert lines[0] == FRESHNESS_NOTE  # stale note leads, block follows
        assert "Pending: 1 incoming for 12345 sats" in lines
        assert PENDING_NO_ETA_NOTE in lines

    def test_fresh_answer_prints_no_stale_note(self) -> None:
        store, wallet_id = _store_with_wallet()
        addr0, addr1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wallet_id, [_utxo(wallet_id, "b" * 64, 1, addr1, 500, confirmed=0)]
        )
        del addr0
        lines = _narrate(_ask(store, wallet_id, completed_scan=True))
        assert FRESHNESS_NOTE not in lines
        assert any(ln.startswith("Pending: ") for ln in lines)


class TestStubPhrasingRouting:
    """MW-11 #3 class, deterministic layer: the utxo-count and pending
    phrasings must reach get_utxos through the stub's phrase table (the
    real-model mapping is pinned in the prompt test + golden fixtures)."""

    def _intent(self, utterance: str) -> str:
        raw = stub_generate(f"user: {utterance}\n\nenvelope:", None)
        return json.loads(raw)["intent"]

    def test_how_many_utxo_do_i_have(self) -> None:
        assert self._intent("how many utxo do I have?") == "get_utxos"

    def test_whats_pending(self) -> None:
        assert self._intent("what's pending?") == "get_utxos"

    def test_whats_incoming(self) -> None:
        assert self._intent("what's incoming?") == "get_utxos"

    def test_no_txid_confirm_ask_routes_to_get_utxos(self) -> None:
        assert self._intent("when will my transaction confirm?") == "get_utxos"
