"""TCK-UTXO-001 (coin labels) as UNIFIED by TCK-LABELS-UNIFY — the address
label set is the ONE labeling surface (schema v6).

The user-ratified model (2026-09-13): one address = one private key = one
provenance. An address-keyed label SET is the source of truth; coins INHERIT
it for the selection engine; a per-UTXO label is an ADDITION to that
address's set (union), never a separate per-coin fact. Pins kept from the
v2-era file, re-expressed on the set surface:

* the migration ladder still upgrades a real v1 file (now through v6, with
  the per-address union fold pinned in tests/test_labels_unify.py);
* typed accessor CRUD: canonical order (closed tags first), free text ≤ 500
  chars verbatim, "KYC"→kyc canonicalization (the tag set survives as ENGINE
  VOCABULARY), fail-closed value-free refusals, UNION idempotence (there is
  no per-coin clear anymore — one coin's command never rewrites the
  address's whole set);
* RESCAN SURVIVAL (the crux, unchanged in kind): ``persist_scan_result``
  rewrites the utxos snapshot only — ``address_label_set`` rows survive every
  rescan and stay after the coin is spent (they key the ADDRESS, not the
  coin);
* the ``/label`` command matrix, adapted: a target resolves to the tx's
  wallet-owned output ADDRESSES (plus the session-stamped own addresses of
  ``last``, replacing the v5 lineage-row trick), adds union members, the ack
  states ADDRESS-level set membership verbatim from the store read-back, and
  a bare target SHOWS (clearing a whole address set from one coin is not
  offered);
* the one post-broadcast capture hint (§1.2): static code-owned line, sets
  the session's ``last`` target, never repeated for the same tx;
* NEVER-IN-MODEL-CONTEXT (§1.1/§7.10): /label lines never reach the turn
  path; label text appears in no prompt/FACTS/envelope;
* one e2e through the REAL handlers: fund → create → confirm → sign →
  broadcast carries the funding address's ``kyc`` member onto the CHANGE
  address's set, then ``/label last`` union-adds before any rescan.
"""

from __future__ import annotations

import json
import queue
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import localwallet.app as app_module
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    _LABEL_NO_TARGET,
    SendSession,
    _handle_transcript_command,
)
from localwallet.protocol import IntentName
from localwallet.store import (
    ADDRESS_LABEL_MAX_CHARS,
    COIN_TAGS,
    DIR_OUT,
    SCHEMA_VERSION,
    Store,
    StoreError,
    TxRecord,
    UtxoRecord,
)

DESCRIPTOR = "wpkh([abcd1234/84'/0'/0']xpub/0/*)"
TXID_A = "a" * 64
TXID_B = "b" * 64
TXID_C = "c" * 64
ADDR_A = "bc1qexamplea"
ADDR_B = "bc1qexampleb"


def _wallet(store: Store) -> int:
    return store.create_wallet("main", DESCRIPTOR).id


def _utxo(
    wallet_id: int, txid: str, vout: int = 0, value: int = 100_000, address: str = ADDR_A
) -> UtxoRecord:
    return UtxoRecord(
        wallet_id=wallet_id,
        txid=txid,
        vout=vout,
        address=address,
        value_sats=value,
        confirmed=1,
        height=900_000,
    )


# ------------------------------------------------- schema-ladder smoke (v1 → v6)


def test_v1_db_upgrades_to_v6_on_reopen(tmp_path: Path) -> None:
    """A real v1 file (no label tables at all, user_version=1) climbs the
    whole ladder on reopen: version stamped v6, the set table usable,
    existing rows intact, and the upgrade is stable across reopens. (The
    per-address UNION of v5-era coin rows has no v1 data to migrate — here
    the tables simply arrive, ride, and fold empty.)"""
    db = tmp_path / "store.db"
    with Store(db) as store:
        _wallet(store)
        store.add_address_labels(ADDR_A, ["kyc"])
    # Simulate the pre-v2 file: drop BOTH label tables, stamp version 1.
    raw = sqlite3.connect(db)
    raw.executescript("DROP TABLE address_label_set;\nPRAGMA user_version=1;")
    raw.commit()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    raw.close()

    with Store(db) as store:  # the ladder runs here (v1→v2 coin_labels,
        # v3 columns, v4 registry, v5 address_labels, v6 fold — ending empty
        # at v6 because the v1 file never had label rows to fold).
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
        assert store.get_wallet_by_name("main") is not None  # v1 data intact
        assert store.get_address_label_sets() == {}  # recreated EMPTY
        store.add_address_labels(ADDR_B, ["p2p"])
    with Store(db) as store:  # idempotent reopen at the top version
        assert store.get_address_label_set(ADDR_B) == ("p2p",)
        # v5's single-label table is GONE entirely (folded, total map).
        assert (
            store._conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'address_labels'"
            ).fetchone()
            is None
        )
        # v2's coin table is RETAINED (write-frozen history contract).
        assert (
            store._conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'coin_labels'"
            ).fetchone()
            is not None
        )


# ------------------------------------------------------------- set accessors


def test_canonical_order_free_text_verbatim_and_kyc_canonicalization() -> None:
    with Store.memory() as store:
        store.add_address_labels(ADDR_A, ["p2p", "kyc", "p2p", "Alice's refund"])
        got = store.get_address_label_set(ADDR_A)
        # Multi-member allowed; CLOSED tags first in §1.4 canonical order,
        # free text sorted; exact-string dedupe; free text verbatim.
        assert got == ("kyc", "p2p", "Alice's refund")
        # The tag-word canonicalization (the engine reads ids only):
        assert store.add_address_labels(ADDR_B, ["KYC"]) == ("kyc",)
        assert store.add_address_labels(ADDR_B, ["Kyc"]) == ("kyc",)  # idempotent


def test_union_is_idempotent_and_additive_never_replacing() -> None:
    """The decided model: a label is an ADDITION to the address's set. A
    second label never replaces the first, and re-adding changes nothing
    (the write is a no-op; the committed read-back says so)."""
    with Store.memory() as store:
        first = store.add_address_labels(ADDR_A, ["kyc", "weekend"])
        again = store.add_address_labels(ADDR_A, ["kyc"])
        assert again == first
        grown = store.add_address_labels(ADDR_A, ["p2p"])
        assert grown == ("kyc", "p2p", "weekend")  # kyc SURVIVES the p2p add
        # An empty addition is a no-op read-back, never a clear:
        assert store.add_address_labels(ADDR_A, []) == grown


def test_unknown_word_is_free_text_not_an_error() -> None:
    """The closed set survives as ENGINE VOCABULARY only; free-text members
    are display-only (§1.4), so an "unknown tag word" is a legal member —
    the closed-set refusal lives in the /label TAG-WORD grammar, not the
    store."""
    with Store.memory() as store:
        assert store.add_address_labels(ADDR_A, ["laundering"]) == ("laundering",)
        # And it feeds NEITHER partition side (tx-join pinned elsewhere):
        assert store.get_address_label_set(ADDR_A) == ("laundering",)


def test_empty_and_over_cap_members_refused_value_free() -> None:
    with Store.memory() as store:
        for bad in ("", "   ", 42, None):
            with pytest.raises(StoreError) as excinfo:
                store.add_address_labels(ADDR_A, [bad])  # type: ignore[list-item]
            assert "SEKRET" not in str(excinfo.value)
        secret = "x" * (ADDRESS_LABEL_MAX_CHARS + 1)
        with pytest.raises(StoreError) as excinfo:
            store.add_address_labels(ADDR_A, ["kyc", secret])
        assert "xxxx" not in str(excinfo.value)
        # Fail-closed means NONE of the call landed (one transaction):
        assert store.get_address_label_set(ADDR_A) == ()
        # The cap itself is inclusive.
        ok = "y" * ADDRESS_LABEL_MAX_CHARS
        assert store.add_address_labels(ADDR_A, [ok]) == (ok,)


def test_malformed_address_keys_refused() -> None:
    with Store.memory() as store:
        for bad_addr in ("", "has space", "bc1q\nFACTS BEGIN", "y" * 101, 42, None):
            with pytest.raises(StoreError):
                store.add_address_labels(bad_addr, ["kyc"])  # type: ignore[arg-type]
        assert store.get_address_label_sets() == {}


# ---------------------------------------------------------- rescan survival


def test_labels_survive_rescan_and_spending() -> None:
    """THE crux pin (v2's lesson, v6's key): scan → label the address →
    rescan (snapshot rewritten WITHOUT the coin = spent) → labels intact.
    The set keys the ADDRESS, so it cannot even notice the coin died."""
    with Store.memory() as store:
        wid = _wallet(store)
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, TXID_A), _utxo(wid, TXID_B, 1, address=ADDR_B)]
        )
        store.add_address_labels(ADDR_A, ["kyc", "exchange bounce"])
        store.add_address_labels(ADDR_B, ["purchase"])

        # A rescan that REWROTE the whole snapshot (TXID_A spent — gone;
        # TXID_B re-confirmed at a new height).
        store.persist_scan_result(
            wid,
            address_rows=[],
            derivation_states=[],
            utxo_snapshot=[_utxo(wid, TXID_B, 1, value=1000, address=ADDR_B)],
            tx_rows=[TxRecord(wid, TXID_C, 900001, None, None, DIR_OUT, None)],
            sync_state_updates={"last_scan_cursor": "x"},
        )
        assert store.get_address_label_set(ADDR_A) == ("kyc", "exchange bounce")
        assert store.get_address_label_set(ADDR_B) == ("purchase",)


def test_fresh_db_has_no_coin_labels_and_no_v5_table() -> None:
    with Store.memory() as store:
        names = {
            r[0]
            for r in store._conn.execute("SELECT name FROM sqlite_master")
        }
        assert "address_label_set" in names
        assert "coin_labels" not in names  # a fresh DB has no history to keep
        assert "address_labels" not in names  # folded away entirely


# --------------------------------------------------- /label command matrix


def _cmd_store(tmp_path: Path | None = None) -> tuple[Store, int]:
    store = Store(tmp_path / "label.db") if tmp_path else Store.memory()
    wid = _wallet(store)
    store.set_active_wallet(wid)
    return store, wid


def _run_label(
    command: str,
    store: Store | None,
    session: SendSession | None = None,
) -> list[str]:
    out: list[str] = []
    loop = AgentLoop(app_module.stub_generate, {})  # never invoked by the handler
    _handle_transcript_command(command, loop, out.append, session=session, store=store)
    return out


def test_label_on_a_coin_adds_to_its_address_set_and_acks_store_truth() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    out = _run_label(f"/label {TXID_A} kyc exchange | weekend", store, SendSession())
    joined = "\n".join(out)
    # The ack names the ADDRESS and echoes the committed set verbatim —
    # address-level set membership, exactly what was stored.
    assert ADDR_A in joined
    assert "your labels: kyc, exchange, weekend" in joined
    assert "Every coin at this address" in joined
    assert store.get_address_label_set(ADDR_A) == ("kyc", "exchange", "weekend")


def test_label_unknown_tag_word_still_refused_nothing_stored() -> None:
    """The command grammar keeps the §1.4 closed-set gate for TAG WORDS
    (free text rides after the |); the store would accept the word as a
    member — the refusal is the command's, and nothing is stored."""
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    out = _run_label(f"/label {TXID_A} laundering", store)
    assert out == [
        (
            "I don't know that label. Known ones: kyc, exchange, p2p, purchase, "
            'consolidation — or type your own words after "|" for a note.'
        )
    ]
    assert store.get_address_label_sets() == {}


def test_label_note_cap_refused() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    out = _run_label(f"/label {TXID_A} kyc | " + "x" * (ADDRESS_LABEL_MAX_CHARS + 1), store)
    assert len(out) == 1 and "too long" in out[0] and "500" in out[0]
    assert store.get_address_label_sets() == {}


def test_label_bare_target_shows_the_set_never_clears() -> None:
    """Union-only semantics: a bare target has nothing to add, so it SHOWS
    the address's committed set — and cannot rewrite or clear it (labels
    belong to the address; one coin's command never rewrites the whole set)."""
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    store.add_address_labels(ADDR_A, ["p2p", "note"])
    out = _run_label(f"/label {TXID_A}", store)
    joined = "\n".join(out)
    assert ADDR_A in joined and "p2p, note" in joined
    assert store.get_address_label_set(ADDR_A) == ("p2p", "note")  # nothing cleared


def test_label_readd_is_honest_about_idempotence() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    store.add_address_labels(ADDR_A, ["kyc"])
    out = _run_label(f"/label {TXID_A} kyc", store)
    assert len(out) == 1
    assert "already carries those labels — nothing changed" in out[0]
    assert store.get_address_label_set(ADDR_A) == ("kyc",)


def test_label_lists_unspent_coins_with_their_address_sets() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(
        wid, [_utxo(wid, TXID_A, address=ADDR_A), _utxo(wid, TXID_B, 1, address=ADDR_B)]
    )
    store.add_address_labels(ADDR_B, ["purchase", "bike"])
    out = _run_label("/label", store)
    joined = "\n".join(out)
    assert "YOU marked" in joined  # §9 honesty frame — never "this is KYC"
    assert f"{TXID_A}:0" in joined and "(unlabeled)" in joined
    assert f"{TXID_B}:1" in joined and "bike" in joined and "purchase" in joined
    # The listing shows the ADDRESS each coin inherits from.
    assert ADDR_B in joined


def test_label_last_and_validation_matrix() -> None:
    store, _wid = _cmd_store()
    # last with nothing broadcast this session → value-free doc line.
    assert _run_label("/label last kyc", store, SendSession()) == [
        'Nothing to label yet — "last" is the most recent payment you\'ve sent.'
    ]
    # malformed txid refused; a tag word with no target gets the usage line.
    assert _run_label("/label deadbeef kyc", store) == [
        'That doesn\'t look like a transaction id — 64 hex characters, or use "last".'
    ]
    assert any("Usage: /label" in line for line in _run_label("/label kyc", store))
    # no store / no active wallet → plain refusals, never a crash.
    assert _run_label("/label", None) == ["Labels are unavailable — no wallet store is open."]
    empty = Store.memory()
    try:
        assert _run_label("/label", empty) == ["No wallet is open yet — nothing to label."]
    finally:
        empty.close()


def test_label_target_resolution_covers_session_addresses_before_rescan() -> None:
    """/label <txid> resolves through the utxo snapshot; ``last`` ALSO rides
    the session's broadcast-stamped own addresses (the v5 lineage-row trick
    is replaced by session state — a just-broadcast change coin's ADDRESS is
    labelable before any scan). An unknown txid says so value-free."""
    store, _wid = _cmd_store()
    session = SendSession(
        last_broadcast_txid=TXID_C, last_broadcast_addresses=(ADDR_A,)
    )
    out = _run_label("/label last purchase", store, session)
    assert any("Noted on address" in line for line in out)
    assert store.get_address_label_set(ADDR_A) == ("purchase",)
    # An unknown txid (no coin anywhere) says so value-free.
    assert _run_label(f"/label {'9' * 64} kyc", store) == [
        "Nothing to label for that transaction yet — its coins show up after a scan."
    ]


# --------------------------------------------------------- capture hint (§1.2)


def test_broadcast_hint_prints_once_and_sets_last() -> None:
    session = SendSession()
    out: list[str] = []
    result = {"status": "broadcast", "txid": TXID_C}
    app_module._print_broadcast_tx(result, out.append, session=session)
    assert any(line.startswith("Sent!") for line in out)
    assert out[-1] == 'Want to remember what this was? Type /label last [tag] ["note"]'
    assert session.last_broadcast_txid == TXID_C
    # Never repeated within the session for the SAME tx; a later tx gets it.
    out2: list[str] = []
    app_module._print_broadcast_tx(result, out2.append, session=session)
    assert not any("Want to remember" in line for line in out2)
    out3: list[str] = []
    app_module._print_broadcast_tx(
        {"status": "broadcast", "txid": TXID_A}, out3.append, session=session
    )
    assert any("Want to remember" in line for line in out3)
    # Failure paths print no hint and move `last` nowhere.
    out4: list[str] = []
    app_module._print_broadcast_tx(
        {"error": "broadcast_failed", "detail": "x"}, out4.append, session=session
    )
    assert not any("Want to remember" in line for line in out4)
    assert session.last_broadcast_txid == TXID_A


# ------------------------------------------------ never-in-model-context pin


def test_label_never_reaches_the_model_or_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§1.1 / §7.10 closed: /label rides the transcript channel ONLY.

    Drives the real pump: one /label command carrying a distinctive note and
    tags (they land on the coin's ADDRESS set now), then one genuine chat
    turn. The model (recording generate_fn) must see exactly ONE turn
    ("hi") and no label text anywhere in its prompt — no FACTS injection, no
    envelope, no transcript entry.
    """
    prompts: list[str] = []

    def recording_generate(prompt: str, grammar: str | None) -> str:
        prompts.append(prompt)
        return '{"v": 0, "intent": "respond", "params": {"text": "ok"}}'

    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    loop = AgentLoop(
        recording_generate,
        {
            IntentName.RESPOND: app_module._respond_handler,
            IntentName.CLARIFY: app_module._clarify_handler,
        },
    )
    commands: queue.Queue[Any] = queue.Queue()
    for line in (
        f"/label {TXID_A} kyc | alice refund zebra",
        "hi",
        app_module.QUIT,
    ):
        commands.put(line)
    outputs: list[str] = []
    app_module._pump(
        loop,
        outputs.append,
        commands,
        flow=app_module.TxFlow(),
        session=SendSession(),
        table={IntentName.RESPOND: app_module._respond_handler},
        store=store,
    )
    # The label was written (command worked)…
    assert store.get_address_label_set(ADDR_A) == ("kyc", "alice refund zebra")
    # …the model ran exactly once, for "hi", and never saw label material.
    assert len(prompts) == 1
    assert "alice refund zebra" not in prompts[0]
    assert TXID_A not in prompts[0]
    # The word "kyc" must not ride the prompt merely because a label exists:
    # the selection join gives the engine booleans, never tag text.
    assert "kyc" not in prompts[0]
    # No transcript/envelope for the /label line: history carries ONE turn.
    assert len(loop.history) == 1


# ------------------------------------------------- e2e through the handlers


def test_broadcast_lineage_then_label_last_before_rescan(
    tmp_path: Path,
) -> None:
    """Fund → create → confirm → sign → broadcast: the change coin's ADDRESS
    inherits the funding address's kyc member (no rescan happened; the
    broadcast derives the change address through the same single-pending
    recovery the sign gate uses), the renderer hint arms ``last``, and
    ``/label last`` UNION-ADDS to that address before any rescan (the v5
    replace semantics are gone: kyc STAYS — additions only)."""
    from tests.test_e2e_skeleton import (
        SEND_RECIPIENT,
        SEND_UTXO,
        _build_send_table,
        _extract_signed_tx,
        _send_chain_handler,
        derive_fixture_addresses,
    )
    from tests.test_phase3_ac import _ScriptedSignerOverride, _validate

    addr0 = derive_fixture_addresses(1)[0]
    state: dict = {}
    signer = _ScriptedSignerOverride(script=[False])
    table, store, _wallet_rec, client, _recorded, _flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state
        ),
        signer_selection=app_module.SignerSelection(
            kind="hwi",
            dir_path=tmp_path / "transfer",
            fingerprint_hex=_fixture_fingerprint(),
        ),
        signer=signer,
    )
    try:
        # Label the funding coin's ADDRESS BEFORE spending it.
        store.add_address_labels(addr0, ["kyc", "from exchange"])

        created = table[IntentName.CREATE_TX](
            _validate(
                json.dumps(
                    {
                        "v": 0,
                        "intent": "create_tx",
                        "params": {
                            "recipient": SEND_RECIPIENT,
                            "amount_sats": 60_000,
                        },
                    }
                )
            )
        )
        tx_ref = created["tx_ref"]
        session.gate_decision = app_module.GateDecision.CONFIRM
        table[IntentName.CONFIRM_TX](
            _validate(json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}}))
        )
        table[IntentName.SIGN_TX](
            _validate(json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}}))
        )
        broadcast = table[IntentName.BROADCAST_TX](
            _validate(
                json.dumps({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": tx_ref}})
            )
        )
        assert broadcast["status"] == "broadcast"
        txid = _extract_signed_tx(signer.signed_psbts[0]).txid().hex()
        assert broadcast["txid"] == txid
        assert broadcast.get("store_warning") is None  # inheritance ran cleanly

        # Inheritance: the CHANGE address's set carries the funding address's
        # tag — union of closed members only, the free-text label NOT
        # inherited — with NO rescan involved (the broadcast derived the
        # change address itself and stamped it on the session).
        assert len(session.last_broadcast_addresses) == 1
        change_addr = session.last_broadcast_addresses[0]
        inherited = store.get_address_label_set(change_addr)
        assert inherited == ("kyc",)  # "from exchange" (free text) NOT inherited

        # Narration arms the capture moment: hint once, `last` set.
        out: list[str] = []
        app_module._print_broadcast_tx(broadcast, out.append, session=session)
        assert "Want to remember what this was?" in out[-1]
        assert session.last_broadcast_txid == txid

        # /label last works BEFORE any rescan (target = the session-stamped
        # ADDRESS) and UNION-ADDS: the inherited kyc SURVIVES (v6 replaces
        # v5's bare-relabel-replaces with additions-only).
        _handle_transcript_command(
            "/label last consolidation | weekend tidy-up",
            AgentLoop(app_module.stub_generate, {}),
            out.append,
            session=session,
            store=store,
        )
        assert store.get_address_label_set(change_addr) == (
            "kyc",
            "consolidation",
            "weekend tidy-up",
        )
    finally:
        client.close()
        store.close()


def _run_send_cycle(
    table: dict[IntentName, Any],
    signer: Any,
    session: SendSession,
    amount: int,
    out: list[str],
) -> str:
    """Drive one create → confirm → sign → broadcast cycle through the REAL
    handlers (mirrors the sibling e2e test's steps) and advance the session's
    ``last_broadcast_txid`` via the narration seam. Returns the broadcast txid."""
    from tests.test_e2e_skeleton import SEND_RECIPIENT
    from tests.test_phase3_ac import _validate

    created = table[IntentName.CREATE_TX](
        _validate(
            json.dumps(
                {
                    "v": 0,
                    "intent": "create_tx",
                    "params": {"recipient": SEND_RECIPIENT, "amount_sats": amount},
                }
            )
        )
    )
    tx_ref = created["tx_ref"]
    session.gate_decision = app_module.GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](
        _validate(json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}}))
    )
    table[IntentName.SIGN_TX](
        _validate(json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}}))
    )
    broadcast = table[IntentName.BROADCAST_TX](
        _validate(
            json.dumps({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    assert broadcast["status"] == "broadcast"
    app_module._print_broadcast_tx(broadcast, out.append, session=session)
    return str(broadcast["txid"])


def test_second_changeless_broadcast_clears_last_broadcast_addresses(
    tmp_path: Path,
) -> None:
    """FINDING 3 regression pin: a broadcast-with-change, then a plain
    no-change send. The SECOND broadcast's own-output analysis sees no change
    and no self-payment, so it CLEARS ``last_broadcast_addresses`` to () —
    and ``/label last`` then answers the honest no-target line instead of
    labeling the FIRST broadcast's stale change address."""
    from tests.test_e2e_skeleton import (
        SEND_UTXO,
        _build_send_table,
        _send_chain_handler,
        derive_fixture_addresses,
    )
    from tests.test_phase3_ac import _ScriptedSignerOverride

    addr0 = derive_fixture_addresses(1)[0]
    state: dict = {}
    signer = _ScriptedSignerOverride(script=[False, False])  # honest for BOTH sends
    table, store, _wallet_rec, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state
        ),
        signer_selection=app_module.SignerSelection(
            kind="hwi",
            dir_path=tmp_path / "transfer",
            fingerprint_hex=_fixture_fingerprint(),
        ),
        signer=signer,
    )
    try:
        out: list[str] = []

        # Send 1: broadcast WITH change — arms ``last`` at the change address.
        txid1 = _run_send_cycle(table, signer, session, 60_000, out)
        assert len(session.last_broadcast_addresses) == 1
        assert session.last_broadcast_txid == txid1
        first_change_addr = session.last_broadcast_addresses[0]

        # Start the second send clean.
        flow.reset()

        # Send 2: a plain no-change send (99_700 from 100_000 leaves only
        # below-dust residue, so no change output and no self-payment). The
        # broadcast's own-output analysis yields NOTHING, so it clears the
        # prior broadcast's addresses instead of leaving them stale.
        txid2 = _run_send_cycle(table, signer, session, 99_700, out)
        assert txid2 != txid1
        assert session.last_broadcast_txid == txid2
        assert session.last_broadcast_addresses == ()

        # ``/label last`` after the changeless broadcast must NOT label the
        # first send's change address: it answers the honest no-target line.
        last_out: list[str] = []
        _handle_transcript_command(
            "/label last kyc",
            AgentLoop(app_module.stub_generate, {}),
            last_out.append,
            session=session,
            store=store,
        )
        assert last_out == [_LABEL_NO_TARGET]
        # And nothing was stored against the FIRST broadcast's address.
        assert store.get_address_label_set(first_change_addr) == ()
    finally:
        client.close()
        store.close()


def _fixture_fingerprint() -> str:
    from tests.test_e2e_skeleton import _fixture_parsed

    return _fixture_parsed().hd_key.my_fingerprint.hex()


# ------------------------------------------------ CLI wiring pin (store → pump)


def test_repl_passes_store_so_label_works_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI ``_repl`` → ``_pump`` → transcript-handler chain carries the
    engine store, so ``/label`` works in a real REPL session: label the
    funded coin's address mid-session, list it back, then union-add to the
    just-broadcast change address via ``last`` (pre-rescan, session-carried)."""
    from tests.test_e2e_skeleton import (
        SEND_RECIPIENT,
        SEND_UTXO,
        _run_send_repl,
        _send_chain_handler,
        _store_path,
        derive_fixture_addresses,
    )
    from tests.test_phase3_ac import _device_sign_before_line

    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict = {}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)

    labeled = {"done": False}
    device_hook = _device_sign_before_line(transfer)

    def before_line() -> None:
        device_hook()
        if not labeled["done"]:
            labeled["done"] = True
            # A second WAL connection (the feeder-thread seam): label the
            # funding coin's address while the engine holds its own store open.
            with Store(_store_path(tmp_path)) as side:
                active = side.get_active_wallet()
                assert active is not None
                side.add_address_labels(addr0, ["kyc"])

    code, outputs, _flow_obj = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",  # confirm + chained export
            "sign it",  # import → revalidate → SIGNED
            "broadcast it",
            "/label",  # list: the funded coin shows the inherited KYC claim
            "/label last purchase | coffee",  # union-add on the change address
            "exit",
        ],
        ["create", "confirm", "sign", "broadcast"],
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert "Want to remember what this was? Type /label last" in joined
    assert joined.count("Want to remember") == 1  # hint once, terminal state
    assert "YOU marked" in joined and "kyc" in joined  # the list line
    assert "Noted on address" in joined and "coffee" in joined
    # Inheritance landed at broadcast, then /label last UNION-added; the
    # funding address's kyc rides the change set (additions, not replace).
    with Store(_store_path(tmp_path)) as store:
        sets = store.get_address_label_sets()
        change_sets = [m for m in sets.values() if "purchase" in m]
        assert change_sets and all("kyc" in m for m in change_sets)
        assert "coffee" in change_sets[0]


def test_transcript_help_lists_label() -> None:
    out = _run_label("/help", None)
    assert len(out) == 1 and "/label" in out[0]


def test_label_closed_set_matches_doc() -> None:
    assert COIN_TAGS == ("kyc", "exchange", "p2p", "purchase", "consolidation")
