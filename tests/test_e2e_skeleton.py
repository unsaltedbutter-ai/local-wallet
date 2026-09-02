"""End-to-end tests (TCK-P0-006 skeleton + TCK-P1-004 wallet wiring).

Full pipeline WITHOUT network or model:

    user text → AgentLoop (stub/scripted generate_fn) → handle_raw
    validation → allowlist dispatch → store-backed handlers →
    EsploraClient over httpx.MockTransport (via wallet scan) → result
    dict → CLI printing.

Phase 1 shape: the app persists to a real SQLite store (tmp file via
``LOCALWALLET_STORE_PATH``), the startup/lazy scan populates it through
the gap-limited scanner, and every handler reads the store. Addresses in
results/narration are verbatim from store/tool output; nothing else
prints values.

Key fixtures are real SLIP-132 keys derived deterministically via embit
from a fixed seed; the hardcoded constants below are re-derived in
``test_fixture_keys_match_embit_rederivation`` to guard against typos,
and address derivation is cross-checked against embit's descriptor
engine (an independent code path).

The one live-network integration test is deselected unless
``LOCALWALLET_E2E_LIVE=1`` (module-level env check per the ticket; no
pyproject changes).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import localwallet.app as app_module
from localwallet.agent.loop import AgentLoop, AgentTurnStatus
from localwallet.agent.runtime import GenerateFn
from localwallet.app import (
    AUTO_SCAN_ENV_VAR,
    OUT_OF_WINDOW_NOTICE,
    PRIVACY_INDICATOR,
    ZPUB_ENV_VAR,
    SendSession,
    build_dispatch_table,
    run,
    stub_generate,
)
from localwallet.chain import EsploraClient, PriceOracle
from localwallet.protocol import Envelope, IntentName, validate_payload
from localwallet.store import (
    AddressRecord,
    Store,
    TxRecord,
    UtxoRecord,
)
from localwallet.tx.flow import GateDecision, TxFlow, TxFlowStatus
from localwallet.wallet import GAP_LIMIT_SETTING, scan_wallet
from localwallet.wallet.derivation import derive_addresses, derive_receive_addresses
from localwallet.wallet.descriptor import (
    ParsedKey,
    WalletDescriptor,
    WatchKeyError,
    parse_wallet_key,
    parse_watch_key,
)

# --------------------------------------------------------------- fixtures
# All keys below are derived via embit from this fixed seed (see
# _rederive_fixture_key). NOT a real wallet; public keys only.

FIXTURE_SEED: Final = b"local-wallet phase 0 test fixture seed (not a real wallet)"

VPUB: Final[str] = (
    "vpub5ZJ3cDEGGk61yWWUHFHgmG3M4je4yFD3ebC6jWHsqV8Cxh2K5zz8c6X5Hk7FkUAB"
    "FTjRkQBz3g84MYeRhjAdnq1QmrmyTRTrzs8rFVCJUyh"
)
UPUB: Final[str] = (
    "upub5EZAYmn7rXyfeYphHTqJ3m8umysXpxE1Fz8rYCcMBCjJ2KdSuCNf24pxTYGDyDyz"
    "aVwW7KKyF7HezfVT9APYvons4NPiRm4oht646o9zVi9"
)
TPUB: Final[str] = (
    "tpubDCPxzVARcvNjZZjy5nZi1GS2NJsRG3TkDZeuncBCT2eWFMbMhkcf5WLeMiTsY6Ae"
    "N7CfrtRKkAJFTC8VqRgPwza2kDfgBEqJ3hkN8GcfXn9"
)
MAINNET_ZPUB: Final[str] = (
    "zpub6rgMkYLjy1UMKp8DKDd75zsFRxWLZ9PqD1rMiFU7rKtojttHq3F7SwvsfKEZ4B9M"
    "f1v79VxU34nbw4dQApHKxTEiyLh7fXb18VojW2ae944"
)
# BIP32 test vector 1 root key — a *private* extended key (watch-only refusal).
XPRV: Final[str] = (
    "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKm"
    "PGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
)

# Corrupted-checksum variant of the fixture vpub (last char replaced).
CORRUPT_VPUB: Final[str] = VPUB[:-1] + ("1" if VPUB[-1] != "1" else "2")

# Fixture UTXOs served by the MockTransport chain (branch 0, indices 0/1).
UTXOS_ADDR0: Final[list[dict[str, Any]]] = [
    {"txid": "a" * 64, "vout": 0, "value": 50_000, "status": {"confirmed": True}},
    {"txid": "b" * 64, "vout": 1, "value": 12_345, "status": {"confirmed": False}},
]
UTXOS_ADDR1: Final[list[dict[str, Any]]] = [
    {"txid": "c" * 64, "vout": 0, "value": 7_000, "status": {"confirmed": True}},
]

EXPECTED_CONFIRMED: Final[int] = 50_000 + 7_000
EXPECTED_UNCONFIRMED: Final[int] = 12_345
EXPECTED_TOTAL: Final[int] = EXPECTED_CONFIRMED + EXPECTED_UNCONFIRMED
TIP_HEIGHT: Final[int] = 870_000

# Small gap for store-backed tests: window = [0, 1] per branch when no
# transactions are observed (2 consecutive unused addresses).
TEST_GAP: Final[int] = 2

GET_BALANCE_JSON: Final[str] = '{"v": 0, "intent": "get_balance", "params": {}}'
GET_HISTORY_LIMIT_JSON: Final[str] = (
    '{"v": 0, "intent": "get_history", "params": {"limit": 5}}'
)
NEW_ADDRESS_JSON: Final[str] = '{"v": 0, "intent": "new_address", "params": {}}'
NEW_ADDRESS_CHANGE_JSON: Final[str] = (
    '{"v": 0, "intent": "new_address", "params": {"branch": 1}}'
)
GARBAGE: Final[str] = "this is not json at all <<<>>>"

_EXTERNAL: Final[str] = "tb1qexternalsenderaddressnotpartofthewallet000000"


def _rederive_fixture_key(purpose: int, coin: int, prv_version: bytes, pub_version: bytes) -> str:
    """Deterministically derive a fixture key via embit from FIXTURE_SEED.

    Derives m/{purpose}'/{coin}'/0' with a private root key of
    ``prv_version`` and serializes the account-level *public* key with
    ``pub_version`` (embit refuses prv version bytes on a public key).
    """
    from embit.bip32 import HDKey

    root = HDKey.from_seed(FIXTURE_SEED, version=prv_version)
    account = root.derive([purpose + 2**31, coin + 2**31, 0])
    return account.to_public().to_base58(version=pub_version)


def _expected_addresses(count: int, branch: int = 0) -> list[str]:
    """Addresses per embit's descriptor engine — independent cross-check."""
    from embit.descriptor import Descriptor
    from embit.networks import NETWORKS

    descriptor = Descriptor.from_string(f"wpkh({VPUB}/{branch}/*)")
    return [
        descriptor.derive(i, branch_index=0).address(network=NETWORKS["test"])
        for i in range(count)
    ]


def _fixture_parsed() -> ParsedKey:
    """The fixture wallet's parsed key through the wallet-engine gate."""
    return WalletDescriptor.from_key(VPUB).parsed


def derive_fixture_addresses(count: int = 5, branch: int = 0) -> list[str]:
    """Addresses for the fixture wallet through the module under test."""
    return [d.address for d in derive_addresses(_fixture_parsed(), branch, 0, count)]


def _tx_entry(
    txid: str,
    *,
    vout_addresses: tuple[str, ...] = (),
    fee: int | None = 1000,
    confirmed: bool = True,
    height: int | None = 800_000,
    block_time: int | None = 1_700_000_000,
) -> dict[str, Any]:
    """Build an Esplora address-txs entry (mirrors the scan fixtures)."""
    entry: dict[str, Any] = {
        "txid": txid,
        "version": 1,
        "locktime": 0,
        "vin": [{"prevout": {"scriptpubkey_address": _EXTERNAL, "value": 100_000}}],
        "vout": [
            {"scriptpubkey_address": a, "value": 90_000} for a in vout_addresses
        ],
        "size": 222,
        "weight": 564,
        "status": {"confirmed": confirmed},
    }
    if fee is not None:
        entry["fee"] = fee
    if confirmed:
        if height is not None:
            entry["status"]["block_height"] = height
        if block_time is not None:
            entry["status"]["block_time"] = block_time
    return entry


class ScriptedGenerate:
    """generate_fn stub: scripted responses, then GARBAGE forever."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        self.calls.append((prompt, grammar_text))
        if self.responses:
            return self.responses.pop(0)
        return GARBAGE


def _scan_handler(
    recorded: list[httpx.Request],
    *,
    txs_by_addr: dict[str, list[dict[str, Any]]] | None = None,
    utxos_by_addr: dict[str, list[dict[str, Any]]] | None = None,
    tip: int | list[dict[str, Any]] = TIP_HEIGHT,
    tip_status: int = 200,
    txs_status: int = 200,
    utxo_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """MockTransport handler serving per-address txs/utxo payloads + tip."""

    txs_by_addr = txs_by_addr or {}
    utxos_by_addr = utxos_by_addr or {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        path = request.url.path
        if path.endswith("/blocks/tip"):
            return httpx.Response(tip_status, json=tip if tip_status == 200 else None)
        parts = path.rstrip("/").split("/")
        address, kind = parts[-2], parts[-1]
        if kind == "txs":
            if txs_status != 200:
                return httpx.Response(txs_status, json=None)
            return httpx.Response(200, json=txs_by_addr.get(address, []))
        if kind == "utxo":
            if utxo_status != 200:
                return httpx.Response(utxo_status, json=None)
            return httpx.Response(200, json=utxos_by_addr.get(address, []))
        return httpx.Response(404, json=None)

    return handler


def _utxo_handler(
    utxos_by_addr: dict[str, list[dict[str, Any]]],
    recorded: list[httpx.Request],
    *,
    tip: int | list[dict[str, Any]] = TIP_HEIGHT,
    tip_status: int = 200,
    utxo_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """Backwards-compatible alias over :func:`_scan_handler` (utxos only)."""
    return _scan_handler(
        recorded,
        utxos_by_addr=utxos_by_addr,
        tip=tip,
        tip_status=tip_status,
        utxo_status=utxo_status,
    )


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response], *, max_retries: int = 0
) -> EsploraClient:
    """EsploraClient wired to a MockTransport (no real network)."""
    return EsploraClient(
        base_url="https://mempool.space/testnet4/api",
        timeout_s=5.0,
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
    )


def _build_table(
    make_handler: Callable[[list[httpx.Request]], Callable[[httpx.Request], httpx.Response]],
    *,
    gap_limit: int | None = TEST_GAP,
) -> tuple[dict[IntentName, Any], Store, Any, EsploraClient, list[httpx.Request]]:
    """Store-backed dispatch table wired to a mock-transport client.

    ``make_handler`` receives the recorded-request list and returns the
    MockTransport handler (``_scan_handler`` fits directly). Returns
    ``(table, store, wallet, client, recorded)``. The store is in-memory
    with the fixture wallet row; ``gap_limit`` seeds the ``gap_limit``
    setting so scan windows stay small and request counts deterministic
    (``None`` leaves the default gap of 20).
    """
    recorded: list[httpx.Request] = []
    client = _mock_client(make_handler(recorded))
    store = Store.memory()
    wd = WalletDescriptor.from_key(VPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    if gap_limit is not None:
        store.set_setting(GAP_LIMIT_SETTING, str(gap_limit))
    table = build_dispatch_table(
        store, wallet, wd.parsed, client, lambda: scan_wallet(store, client, wallet)
    )
    return table, store, wallet, client, recorded


# ------------------------------------------------------------ key fixtures


def test_fixture_keys_match_embit_rederivation() -> None:
    """The hardcoded key constants are exactly what embit derives from the
    fixed seed — proving they are real, checksum-valid SLIP-132 keys."""
    from embit.bip32 import NETWORKS

    assert VPUB == _rederive_fixture_key(
        84, 1, NETWORKS["test"]["zprv"], NETWORKS["test"]["zpub"]
    )
    assert UPUB == _rederive_fixture_key(
        49, 1, NETWORKS["test"]["yprv"], NETWORKS["test"]["ypub"]
    )
    assert TPUB == _rederive_fixture_key(
        44, 1, NETWORKS["test"]["xprv"], NETWORKS["test"]["xpub"]
    )
    assert MAINNET_ZPUB == _rederive_fixture_key(
        84, 0, NETWORKS["main"]["zprv"], NETWORKS["main"]["zpub"]
    )


def _mainnet_fixture_key(script: str) -> str:
    """Mainnet ypub/xpub fixtures derived from the same fixed seed."""
    from embit.bip32 import NETWORKS

    purpose = {"ypub": 49, "xpub": 44}[script]
    prv = NETWORKS["main"][f"{script[0]}prv"]
    pub = NETWORKS["main"][script]
    return _rederive_fixture_key(purpose, 0, prv, pub)


@pytest.mark.parametrize(
    ("key_id", "expected_network", "expected_script"),
    [
        ("vpub", "testnet", "p2wpkh"),
        ("zpub(main)", "main", "p2wpkh"),
        ("upub", "testnet", "p2sh_p2wpkh"),
        ("tpub", "testnet", "p2pkh"),
        ("ypub(main)", "main", "p2sh_p2wpkh"),
        ("xpub(main)", "main", "p2pkh"),
    ],
)
def test_parse_watch_key_detects_network_and_script_type(
    key_id: str, expected_network: str, expected_script: str
) -> None:
    key = {
        "vpub": VPUB,
        "zpub(main)": MAINNET_ZPUB,
        "upub": UPUB,
        "tpub": TPUB,
        "ypub(main)": _mainnet_fixture_key("ypub"),
        "xpub(main)": _mainnet_fixture_key("xpub"),
    }[key_id]

    parsed = parse_watch_key(key)
    assert isinstance(parsed, ParsedKey)
    assert parsed.network == expected_network
    assert parsed.script_type == expected_script
    assert not parsed.hd_key.is_private


def test_vpub_derives_deterministic_tb1_addresses_matching_descriptor() -> None:
    parsed = parse_watch_key(VPUB)
    assert parsed.network == "testnet"
    assert parsed.script_type == "p2wpkh"

    addresses = derive_receive_addresses(parsed, count=5)
    # The ticket's testnet-prefix assertion: vpub path → bech32 tb1...
    assert all(addr.startswith("tb1") for addr in addresses)
    # Cross-checked against embit's descriptor engine (independent path).
    assert addresses == _expected_addresses(5)
    # Deterministic and prefix-stable across calls and counts.
    assert derive_receive_addresses(parsed, count=5) == addresses
    assert derive_receive_addresses(parsed, count=3) == addresses[:3]
    assert len(addresses) == 5


def test_change_branch_derivation_differs_from_receive() -> None:
    parsed = parse_watch_key(VPUB)
    receive = derive_receive_addresses(parsed, count=2, branch=0)
    change = derive_receive_addresses(parsed, count=2, branch=1)
    assert receive != change
    assert change == _expected_addresses(2, branch=1)


@pytest.mark.parametrize("count", [0, -1, True, 1001, 2.5, "3", None])
def test_derive_rejects_out_of_range_count(count: object) -> None:
    parsed = parse_watch_key(VPUB)
    with pytest.raises(WatchKeyError, match="count"):
        derive_receive_addresses(parsed, count=count)  # type: ignore[arg-type]


@pytest.mark.parametrize("branch", [2, -1, True, "0"])
def test_derive_rejects_invalid_branch(branch: object) -> None:
    parsed = parse_watch_key(VPUB)
    with pytest.raises(WatchKeyError, match="branch"):
        derive_receive_addresses(parsed, count=1, branch=branch)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "\t\n", "not-a-key", "1" * 30, CORRUPT_VPUB, XPRV, f"{VPUB} {VPUB}"],
)
def test_unparseable_and_private_keys_fail_closed_without_echo(bad_key: str) -> None:
    with pytest.raises(WatchKeyError) as excinfo:
        parse_watch_key(bad_key)
    message = str(excinfo.value)
    assert message.strip() != ""
    # Value-free error contract: the key material is never echoed.
    # (Empty-key cases have nothing to echo.)
    if bad_key.strip():
        assert bad_key.strip() not in message


def test_private_key_refusal_names_the_watch_only_rule() -> None:
    with pytest.raises(WatchKeyError, match="watch-only"):
        parse_watch_key(XPRV)


def test_mainnet_zpub_refused_by_testnet_gate() -> None:
    parsed = parse_watch_key(MAINNET_ZPUB)
    assert parsed.network == "main"  # parse detects; derive enforces the gate
    with pytest.raises(WatchKeyError) as excinfo:
        derive_receive_addresses(parsed)
    message = str(excinfo.value)
    assert "Phase 0 is testnet-only" in message
    assert "vpub/upub/tpub" in message
    assert MAINNET_ZPUB not in message  # key never echoed


def test_parse_wallet_key_enforces_testnet_gate_at_parse() -> None:
    """Phase 1 gate: parse_wallet_key refuses mainnet keys outright."""
    with pytest.raises(WatchKeyError) as excinfo:
        parse_wallet_key(MAINNET_ZPUB)
    message = str(excinfo.value)
    assert "testnet-only" in message
    assert MAINNET_ZPUB not in message  # key never echoed
    # Testnet keys pass.
    assert parse_wallet_key(VPUB).network == "testnet"


def test_testnet_keys_pass_the_gate() -> None:
    for key in (VPUB, UPUB, TPUB):
        addresses = derive_receive_addresses(parse_watch_key(key), count=2)
        assert len(addresses) == 2


def test_wallet_descriptor_is_canonical_and_checksummed() -> None:
    wd = WalletDescriptor.from_key(VPUB)
    assert wd.descriptor.startswith("wpkh([")
    assert "#" in wd.descriptor
    # Round-trip: rebuilt from the stored string, identical canonical form.
    rebuilt = WalletDescriptor.from_descriptor_string(wd.descriptor)
    assert rebuilt.descriptor == wd.descriptor


# ------------------------------------------------------- stub model routing


@pytest.mark.parametrize(
    ("statement", "expected_intent"),
    [
        ("What's my balance?", IntentName.GET_BALANCE),
        ("show my recent transactions", IntentName.GET_HISTORY),
        ("what is my transaction history?", IntentName.GET_HISTORY),
        ("show my utxos", IntentName.GET_UTXOS),
        ("give me a new address", IntentName.NEW_ADDRESS),
        ("hello there", IntentName.RESPOND),
    ],
)
def test_stub_generate_routes_wallet_phrases(
    statement: str, expected_intent: IntentName
) -> None:
    prompt = f"SYSTEM...\n\nuser: {statement}\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is expected_intent


def test_stub_generate_emits_respond_otherwise() -> None:
    prompt = "SYSTEM...\n\nuser: hello there\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, "root ::= ..."))
    assert envelope.intent is IntentName.RESPOND
    assert isinstance(envelope.params.text, str) and envelope.params.text.strip()


# ------------------------------------------------ stub model: send phrases


def test_stub_generate_send_phrase_emits_create_tx_with_extracted_fields() -> None:
    """'send 60000 sats to <tb1…>' → create_tx with the tb1 token and the
    sats figure extracted verbatim from the user turn (canned dev model)."""
    prompt = f"SYSTEM...\n\nuser: send 60000 sats to {SEND_RECIPIENT}\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.CREATE_TX
    assert envelope.params.recipient == SEND_RECIPIENT
    assert envelope.params.amount_sats == 60_000
    assert envelope.params.amount_usd is None
    assert envelope.params.fee_target is None


def test_stub_generate_send_usd_phrase_emits_amount_usd() -> None:
    prompt = f"SYSTEM...\n\nuser: send $12.50 to {SEND_RECIPIENT} please\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.CREATE_TX
    assert envelope.params.amount_usd == 12.5
    assert envelope.params.amount_sats is None


def test_stub_generate_send_falls_back_to_fixture_recipient_and_amount() -> None:
    """No parsable amount/usable tb1 token → canned fixture recipient
    (the P0 fixture address) and the canned 10000-sat amount."""
    prompt = "SYSTEM...\n\nuser: send to tb1\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.CREATE_TX
    assert envelope.params.recipient == app_module._STUB_RECIPIENT
    assert envelope.params.amount_sats == 10_000
    # The canned recipient IS the P0 fixture address (valid testnet P2WPKH).
    assert app_module._STUB_RECIPIENT == derive_fixture_addresses(1)[0]


def test_stub_generate_confirmation_utterance_emits_canned_confirm_tx() -> None:
    """'yes please' → canned confirm_tx. The placeholder tx_ref
    deliberately cannot match a real pending reference (the stub cannot
    see the flow's id factory) — dispatching it exercises the flow's
    refusal path in dev mode; deterministic tests inject closures."""
    prompt = "SYSTEM...\n\nuser: yes please\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.CONFIRM_TX
    assert envelope.params.tx_ref == "dev-stub-pending-tx"


# ------------------------------------------------ stub model: lifecycle phrases


def test_stub_generate_sign_phrase_emits_canned_sign_tx() -> None:
    prompt = "SYSTEM...\n\nuser: sign it\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.SIGN_TX
    assert envelope.params.tx_ref == "dev-stub-pending-tx"  # refusal demo


def test_stub_generate_broadcast_phrase_emits_canned_broadcast_tx() -> None:
    prompt = "SYSTEM...\n\nuser: broadcast it\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.BROADCAST_TX
    assert envelope.params.tx_ref == "dev-stub-pending-tx"  # refusal demo


def test_stub_generate_status_phrase_emits_tx_status_with_quoted_txid() -> None:
    """'status of txid <64-hex>' → tx_status quoting the hex token
    VERBATIM from the user turn; without one, the canned placeholder."""
    txid = "0123456789abcdef" * 4
    prompt = f"SYSTEM...\n\nuser: status of txid {txid}\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.TX_STATUS
    assert envelope.params.txid == txid
    fallback = validate_payload(stub_generate("SYSTEM...\n\nuser: status?\n\nenvelope:", None))
    assert fallback.intent is IntentName.TX_STATUS
    assert fallback.params.txid == app_module._STUB_TX_STATUS_TXID


def test_stub_lifecycle_placeholders_refuse_cleanly_through_repl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dev-mode refusal demo: with the stub model and NO flow in progress,
    the canned sign_tx/broadcast_tx placeholders dispatch to value-free
    refusals (the placeholders cannot match a real flow record)."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["sign it", "broadcast it", "exit"],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "Not signed — no confirmed transaction to sign." in joined
    assert "Not broadcast — no signed transaction to broadcast." in joined


# ------------------------------------------------- config: store_path env


def test_settings_store_path_env_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", "/tmp/lw-test/store.db")
    assert app_module.Settings.from_env().store_path == "/tmp/lw-test/store.db"
    monkeypatch.delenv("LOCALWALLET_STORE_PATH", raising=False)
    assert app_module.Settings.from_env().store_path == "localwallet.db"


# ------------------------------------------------------- e2e: loop → store


def test_balance_end_to_end_agent_to_dispatcher_to_scan_to_store() -> None:
    """'What's my balance?' flows: scripted model → envelope validation →
    allowlist dispatch → get_balance handler → lazy scan (mock chain) →
    store → totals."""
    addr0, addr1 = derive_fixture_addresses(2)
    change0, change1 = derive_fixture_addresses(2, branch=1)
    table, store, wallet, client, recorded = _build_table(
        lambda rec: _scan_handler(rec, utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1})
    )

    gen = ScriptedGenerate([GET_BALANCE_JSON])
    turn = AgentLoop(gen, table).run("What's my balance?", {})

    assert turn.status is AgentTurnStatus.OK
    assert turn.envelope is not None
    assert turn.envelope.intent is IntentName.GET_BALANCE
    assert turn.turns_used == 1
    assert turn.result == {
        "confirmed_sats": EXPECTED_CONFIRMED,
        "unconfirmed_sats": EXPECTED_UNCONFIRMED,
        "total_sats": EXPECTED_TOTAL,
        "addresses_scanned": 2,  # addresses WITH utxos (ticket contract)
        "tip_height": TIP_HEIGHT,
    }
    # The lazy scan really hit the chain adapter: one txs + one utxo call
    # per window address (gap 2 → 2 per branch) plus one tip request.
    utxo_paths = {r.url.path for r in recorded if r.url.path.endswith("/utxo")}
    assert utxo_paths == {
        f"/testnet4/api/address/{a}/utxo"
        for a in (addr0, addr1, change0, change1)
    }
    assert any(r.url.path.endswith("/blocks/tip") for r in recorded)
    # The scripted model received the real envelope grammar via the seam.
    assert "root ::=" in (gen.calls[0][1] or "")
    # The scan populated the store (single wallet row, UTXO snapshot).
    assert len(store.list_wallets()) == 1
    assert len(store.get_utxos_for_wallet(wallet.id)) == 3
    client.close()
    store.close()


def test_second_balance_read_hits_store_only() -> None:
    """After one scan, later balance reads come from the cache: no new
    chain requests."""
    addr0, addr1 = derive_fixture_addresses(2)
    table, store, _wallet, client, recorded = _build_table(
        lambda rec: _scan_handler(rec, utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1})
    )
    loop = AgentLoop(ScriptedGenerate([GET_BALANCE_JSON, GET_BALANCE_JSON]), table)

    first = loop.run("What's my balance?", {})
    assert first.result is not None and "error" not in first.result
    after_first = len(recorded)
    second = loop.run("and now?", {})
    assert second.result is not None and "error" not in second.result
    assert second.result["total_sats"] == EXPECTED_TOTAL
    assert len(recorded) == after_first  # no chain I/O on the cached read
    client.close()
    store.close()


def test_respond_intent_passthrough_through_the_real_table() -> None:
    recorded: list[httpx.Request] = []
    table, store, _wallet, client, recorded = _build_table(_scan_handler)
    respond_json = '{"v": 0, "intent": "respond", "params": {"text": "Hello!"}}'
    turn = AgentLoop(ScriptedGenerate([respond_json]), table).run("hi", {})
    assert turn.status is AgentTurnStatus.OK
    assert turn.result == {"text": "Hello!"}
    assert recorded == []  # no chain I/O for a respond turn
    client.close()
    store.close()


def test_clarify_intent_passthrough_through_the_real_table() -> None:
    recorded: list[httpx.Request] = []
    table, store, _wallet, client, recorded = _build_table(_scan_handler)
    clarify_json = '{"v": 0, "intent": "clarify", "params": {"question": "How much?"}}'
    turn = AgentLoop(ScriptedGenerate([clarify_json]), table).run("send", {})
    assert turn.status is AgentTurnStatus.OK
    assert turn.result == {"question": "How much?"}
    assert turn.user_message == "How much?"  # model-emitted clarify surfaces
    assert recorded == []
    client.close()
    store.close()


@pytest.mark.parametrize(
    "bad_output",
    [
        GARBAGE,
        "",
        '{"v": 1, "intent": "respond", "params": {"text": "hi"}}',
        '{"v": 0, "intent": "self_destruct", "params": {}}',
        '{"v": 0, "intent": "respond", "params": {}}',
        "{'v': 0, 'intent': 'respond', 'params': {'text': 'single-quoted json'}}",
        '{"v": 0, "intent": "respond", "params": {"text": ""}}',
        '{"v": 0, "intent": "get_balance"}',
    ],
)
def test_malformed_model_output_takes_clean_reject_path(bad_output: str) -> None:
    """AC #2 support: malformed/nonsense model output is rejected cleanly —
    exactly one re-prompt, then a synthesized clarify; never a dispatch."""
    recorded: list[httpx.Request] = []
    table, store, _wallet, client, recorded = _build_table(_scan_handler)
    gen = ScriptedGenerate([bad_output])  # then GARBAGE forever
    loop = AgentLoop(gen, table)

    turn = loop.run("What's my balance?", {})

    assert turn.status is AgentTurnStatus.CLARIFIED
    assert turn.envelope is None
    assert turn.result is None
    assert turn.user_message is not None and turn.user_message.strip() != ""
    assert len(gen.calls) == 2  # one retry, then escalate
    assert "RETRY NOTE" in gen.calls[1][0]
    assert recorded == []  # nothing reached the chain layer
    client.close()
    store.close()


# ------------------------------------------------------- new_address flow


def test_new_address_allocates_bumps_and_needs_no_network() -> None:
    """'give me a new address': derivation from the parsed key, store
    allocation bookkeeping, next call → NEXT index. No chain I/O."""
    recorded: list[httpx.Request] = []
    table, store, wallet, client, recorded = _build_table(_scan_handler)
    loop = AgentLoop(
        ScriptedGenerate([NEW_ADDRESS_JSON, NEW_ADDRESS_CHANGE_JSON]), table
    )

    first = loop.run("give me a new address", {})
    assert first.status is AgentTurnStatus.OK
    expected_receive = derive_addresses(_fixture_parsed(), 0, 0, 1)[0].address
    assert first.result == {"address": expected_receive, "branch": 0, "index": 0}

    second = loop.run("and a change address", {})
    assert second.status is AgentTurnStatus.OK
    expected_change = derive_addresses(_fixture_parsed(), 1, 0, 1)[0].address
    assert second.result == {"address": expected_change, "branch": 1, "index": 0}

    # Allocation consumed exactly one index per branch in the store.
    assert store.get_derivation(wallet.id, 0).next_index == 1
    assert store.get_derivation(wallet.id, 1).next_index == 1
    rows0 = store.get_addresses(wallet.id, 0)
    assert [(r.index, r.status) for r in rows0] == [(0, "allocated")]
    rows1 = store.get_addresses(wallet.id, 1)
    assert [(r.index, r.status) for r in rows1] == [(0, "allocated")]
    # Allocation must NOT require the network: zero chain requests.
    assert recorded == []
    client.close()
    store.close()


def test_new_address_derivation_is_pure_same_index_same_address() -> None:
    """Idempotency contract: re-deriving the same index returns the same
    address (allocation bookkeeping lives in the store, derivation is a
    pure function)."""
    parsed = _fixture_parsed()
    table, store, wallet, client, _recorded = _build_table(_scan_handler)
    handler = table[IntentName.NEW_ADDRESS]
    envelope: Envelope = validate_payload(NEW_ADDRESS_JSON)
    first = handler(envelope)
    assert first == {
        "address": derive_addresses(parsed, 0, 0, 1)[0].address,
        "branch": 0,
        "index": 0,
    }
    # Direct re-derivation of the consumed index (no allocation): identical.
    assert derive_addresses(parsed, 0, 0, 1)[0].address == first["address"]
    # A second allocation consumes the NEXT index — never the same one.
    second = handler(envelope)
    assert second["index"] == 1 and second["address"] != first["address"]
    assert store.get_derivation(wallet.id, 0).next_index == 2
    client.close()
    store.close()


# --------------------------------------------------------- get_history flow


def _seed_txs(store: Store, wallet_id: int) -> list[TxRecord]:
    """25 confirmed txs (heights 800000..800024) + 1 unconfirmed."""
    records = [
        TxRecord(
            wallet_id=wallet_id,
            txid=f"{i:02x}" * 32,
            height=800_000 + i,
            block_time=1_700_000_000 + i * 600,
            fee_sats=1000 + i,
            direction="in" if i % 2 == 0 else "out",
            raw_summary=None,
        )
        for i in range(25)
    ]
    records.append(
        TxRecord(
            wallet_id=wallet_id,
            txid="ee" * 32,
            height=None,
            block_time=None,
            fee_sats=None,
            direction="out",
            raw_summary=None,
        )
    )
    store.upsert_txs(records)
    return records


def test_history_limit_param_and_ordering() -> None:
    """limit=5 over 26 cached txs → 5 shown, height DESC, unconfirmed first."""
    table, store, wallet, client, _recorded = _build_table(_scan_handler)
    seeded = _seed_txs(store, wallet.id)

    turn = AgentLoop(ScriptedGenerate([GET_HISTORY_LIMIT_JSON]), table).run(
        "show my recent transactions", {}
    )
    assert turn.status is AgentTurnStatus.OK
    assert turn.result is not None
    assert turn.result["shown"] == 5
    txs = turn.result["transactions"]
    assert len(txs) == 5
    # Newest first: the unconfirmed tx, then descending block heights.
    assert [t["height"] for t in txs] == [None, 800_024, 800_023, 800_022, 800_021]
    assert [t["txid"] for t in txs] == [
        "ee" * 32,
        f"{24:02x}" * 32,
        f"{23:02x}" * 32,
        f"{22:02x}" * 32,
        f"{21:02x}" * 32,
    ]
    assert all(set(t) == {"txid", "height", "direction", "fee_sats", "block_time"} for t in txs)
    assert len(seeded) == 26
    client.close()
    store.close()


def test_history_default_limit_is_20_without_params() -> None:
    table, store, wallet, client, _recorded = _build_table(_scan_handler)
    _seed_txs(store, wallet.id)
    turn = AgentLoop(
        ScriptedGenerate(['{"v": 0, "intent": "get_history", "params": {}}']), table
    ).run("history", {})
    assert turn.result is not None
    assert turn.result["shown"] == 20
    client.close()
    store.close()


def test_history_narration_lines_are_address_free() -> None:
    table, store, wallet, client, _recorded = _build_table(_scan_handler)
    _seed_txs(store, wallet.id)
    turn = AgentLoop(ScriptedGenerate([GET_HISTORY_LIMIT_JSON]), table).run(
        "show my recent transactions", {}
    )
    outputs: list[str] = []
    app_module._print_turn(turn, outputs.append)
    joined = "\n".join(outputs)
    lines = [line for line in outputs if line.startswith("tx ")]
    assert len(lines) == 5
    unconfirmed = "ee" * 32
    assert f"tx {unconfirmed[:12]}… out unconfirmed" in joined
    top_confirmed = f"{24:02x}" * 32
    assert f"tx {top_confirmed[:12]}… in 800024" in joined
    # P1 narration contract: no addresses in history output.
    for addr in derive_fixture_addresses(3):
        assert addr not in joined
    client.close()
    store.close()


def test_history_empty_store_prints_no_transactions() -> None:
    table, store, _wallet, client, _recorded = _build_table(_scan_handler)
    turn = AgentLoop(
        ScriptedGenerate(['{"v": 0, "intent": "get_history", "params": {}}']), table
    ).run("history", {})
    outputs: list[str] = []
    app_module._print_turn(turn, outputs.append)
    assert "No transactions found." in outputs
    client.close()
    store.close()


# ---------------------------------------------------------- get_utxos flow


def test_utxos_narration_quotes_addresses_verbatim() -> None:
    addr0, addr1 = derive_fixture_addresses(2)
    table, store, wallet, client, _recorded = _build_table(_scan_handler)
    store.replace_utxos_for_wallet(
        wallet.id,
        [
            UtxoRecord(
                wallet_id=wallet.id, txid="a" * 64, vout=0, address=addr0,
                value_sats=50_000, confirmed=1, height=800_000,
            ),
            UtxoRecord(
                wallet_id=wallet.id, txid="b" * 64, vout=1, address=addr1,
                value_sats=12_345, confirmed=0, height=None,
            ),
        ],
    )
    turn = AgentLoop(ScriptedGenerate(['{"v": 0, "intent": "get_utxos", "params": {}}']), table).run(
        "show my utxos", {}
    )
    assert turn.status is AgentTurnStatus.OK
    assert turn.result is not None
    assert turn.result["count"] == 2
    outputs: list[str] = []
    app_module._print_turn(turn, outputs.append)
    joined = "\n".join(outputs)
    # Addresses verbatim from the store (tool output) — quote-verbatim rule.
    assert addr0 in joined and addr1 in joined
    assert "50000 sats · confirmed" in joined
    assert "12345 sats · unconfirmed" in joined
    client.close()
    store.close()


def test_utxos_empty_store_prints_no_unspent_outputs() -> None:
    table, store, _wallet, client, _recorded = _build_table(_scan_handler)
    turn = AgentLoop(ScriptedGenerate(['{"v": 0, "intent": "get_utxos", "params": {}}']), table).run(
        "show my utxos", {}
    )
    outputs: list[str] = []
    app_module._print_turn(turn, outputs.append)
    assert "No unspent outputs." in outputs
    client.close()
    store.close()


# ------------------------------------------------------- chain-error path


def test_handler_chain_error_surfaces_as_result_without_raising() -> None:
    addr0, _addr1 = derive_fixture_addresses(2)
    table, store, _wallet, client, _recorded = _build_table(
        lambda rec: _scan_handler(rec, utxo_status=500)
    )

    envelope: Envelope = validate_payload(GET_BALANCE_JSON)
    result = table[IntentName.GET_BALANCE](envelope)  # direct call: no raise

    assert result["error"] == "chain_unavailable"
    detail = str(result["detail"])
    assert detail.strip() != ""
    # Scrubbing invariant: no address material in the surfaced detail.
    assert addr0 not in detail
    assert set(result.keys()) == {"error", "detail"}
    # Fail-closed scan: the store was left untouched (no cursor, no utxos).
    assert store.get_sync_state(_wallet_id(store), "last_scan_cursor") is None
    client.close()
    store.close()


def _wallet_id(store: Store) -> int:
    row = store.get_wallet_by_name("default")
    assert row is not None
    return row.id


def test_chain_error_flows_through_loop_as_ok_with_error_result() -> None:
    table, store, _wallet, client, _recorded = _build_table(
        lambda rec: _scan_handler(rec, utxo_status=503)
    )

    turn = AgentLoop(ScriptedGenerate([GET_BALANCE_JSON]), table).run(
        "What's my balance?", {}
    )
    assert turn.status is AgentTurnStatus.OK  # handler contained the failure
    assert turn.result is not None
    assert turn.result["error"] == "chain_unavailable"
    client.close()
    store.close()


def test_tip_failure_fails_the_scan_cleanly_store_untouched() -> None:
    """Phase 1 semantics: the tip is fetched inside the scan, so a tip
    failure aborts the scan (fail closed) and surfaces as
    chain_unavailable — the store is left untouched."""
    table, store, wallet, client, _recorded = _build_table(
        lambda rec: _scan_handler(rec, tip_status=500)
    )

    turn = AgentLoop(ScriptedGenerate([GET_BALANCE_JSON]), table).run(
        "What's my balance?", {}
    )
    assert turn.status is AgentTurnStatus.OK
    assert turn.result is not None
    assert turn.result["error"] == "chain_unavailable"
    assert store.get_utxos_for_wallet(wallet.id) == []
    client.close()
    store.close()


def test_tip_list_shape_yields_correct_tip_height() -> None:
    """A list-shaped tip (mempool.space divergence) yields the max height."""
    addr0, addr1 = derive_fixture_addresses(2)
    table, store, _wallet, client, _recorded = _build_table(
        lambda rec: _scan_handler(
            rec,
            utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1},
            tip=[{"height": 100}, {"height": 870_000}],
        )
    )

    envelope: Envelope = validate_payload(GET_BALANCE_JSON)
    result = table[IntentName.GET_BALANCE](envelope)
    assert result["tip_height"] == 870_000
    assert result["total_sats"] == EXPECTED_TOTAL
    client.close()
    store.close()


def test_print_balance_omitted_tip_prints_tip_unavailable_not_tip_height_0() -> None:
    """SR-006 minor 2: a result without tip_height prints 'tip unavailable'
    — never a fabricated 'tip height 0'."""
    outputs: list[str] = []
    app_module._print_balance(
        {
            "confirmed_sats": 1,
            "unconfirmed_sats": 2,
            "total_sats": 3,
            "addresses_scanned": 1,
        },
        outputs.append,
    )
    joined = "\n".join(outputs)
    assert "tip unavailable" in joined
    assert "tip height 0" not in joined


# --------------------------------------------------------------- CLI wiring


def _store_path(tmp_path: Path) -> Path:
    return tmp_path / "store.db"


def _preset_store(path: Path, *, gap_limit: int = TEST_GAP) -> WalletDescriptor:
    """Pre-create the wallet row (+ gap setting) the app will reuse.

    Exercises the duplicate-descriptor guard on every run: the startup
    must reuse this row, never create a second one.
    """
    wd = WalletDescriptor.from_key(VPUB)
    with Store(path) as store:
        wallet = store.create_wallet("default", wd.descriptor)
        store.set_active_wallet(wallet.id)
        store.set_setting(GAP_LIMIT_SETTING, str(gap_limit))
    return wd


def _run_captured(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    lines: list[str],
    *,
    store_path: Path | None = None,
    auto_scan: bool = False,
) -> tuple[int, list[str]]:
    """Run app.run() with stub I/O, a tmp store, and a mock chain client."""
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    if store_path is not None:
        monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "1" if auto_scan else "0")
    monkeypatch.setattr(
        app_module, "EsploraClient", lambda **_: _mock_client(handler)
    )
    inputs = iter(lines)

    def read_line(_prompt: str) -> str:
        return next(inputs)

    outputs: list[str] = []
    code = run(argv, input_fn=read_line, output_fn=outputs.append)
    return code, outputs


def test_startup_scan_populates_store_then_balance_reads_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Startup scan (default-on) fills the store; the balance turn reads
    the cache — and the wallet row was reused, not duplicated."""
    store_path = _store_path(tmp_path)
    wd = _preset_store(store_path)
    recorded: list[httpx.Request] = []
    addr0, addr1 = derive_fixture_addresses(2)
    handler = _scan_handler(
        recorded, utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1}
    )

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
        auto_scan=True,
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Banner: testnet notice + §9 privacy indicator verbatim.
    assert "TESTNET" in joined
    assert PRIVACY_INDICATOR in joined
    # Startup scan feedback: counts + tip only (no addresses/amounts).
    assert "Startup scan complete: 3 UTXOs · tip height 870000." in joined
    # Balance line verbatim from the handler result dict.
    assert (
        f"Balance (testnet): {EXPECTED_CONFIRMED} sats (confirmed) "
        f"+ {EXPECTED_UNCONFIRMED} sats (unconfirmed)" in joined
    )
    assert f"Total {EXPECTED_TOTAL} sats" in joined
    assert f"tip height {TIP_HEIGHT}" in joined
    # Privacy/secret hygiene: the zpub and addresses are never echoed.
    assert VPUB not in joined
    assert all(addr not in joined for addr in (addr0, addr1))
    assert wd.descriptor not in joined
    # 9 requests: 1 tip + (2 txs + 2 utxos) per branch at gap 2.
    assert len(recorded) == 9
    # The store holds exactly the pre-seeded wallet row + scanned state.
    with Store(store_path) as store:
        rows = store.list_wallets()
        assert len(rows) == 1
        assert rows[0].descriptor == wd.descriptor
        utxos = store.get_utxos_for_wallet(rows[0].id)
        assert sorted(u.value_sats for u in utxos) == [7_000, 12_345, 50_000]
        assert store.get_sync_state(rows[0].id, "last_scan_cursor") is not None


def test_repl_end_to_end_with_stub_llm_prints_verbatim_balance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    addr0, addr1 = derive_fixture_addresses(2)
    handler = _scan_handler(
        recorded, utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1}
    )

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "TESTNET" in joined
    assert PRIVACY_INDICATOR in joined
    assert (
        f"Balance (testnet): {EXPECTED_CONFIRMED} sats (confirmed) "
        f"+ {EXPECTED_UNCONFIRMED} sats (unconfirmed)" in joined
    )
    assert f"Total {EXPECTED_TOTAL} sats" in joined
    assert f"tip height {TIP_HEIGHT}" in joined
    assert VPUB not in joined
    assert all(addr not in joined for addr in (addr0, addr1))
    # Lazy scan only (auto-scan off): 1 tip + 4 txs + 4 utxos.
    assert len(recorded) == 9


def test_auto_scan_opt_out_balance_scans_lazily_on_first_ask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOCALWALLET_AUTO_SCAN=0: no chain I/O at startup; the first balance
    ask triggers the scan once lazily, then reads the store."""
    store_path = _store_path(tmp_path)
    recorded: list[httpx.Request] = []
    addr0, addr1 = derive_fixture_addresses(2)
    handler = _scan_handler(
        recorded, utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1}
    )
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(handler))
    lines = iter(["What's my balance?", "exit"])
    marks: dict[str, int] = {}

    def read_line(_prompt: str) -> str:
        value = next(lines)
        marks[value] = len(recorded)
        return value

    outputs: list[str] = []
    code = run(["--stub-llm", "--zpub", VPUB], input_fn=read_line, output_fn=outputs.append)

    assert code == 0
    assert marks["What's my balance?"] == 0  # startup made zero chain calls
    assert marks["exit"] > 0  # the balance turn performed the lazy scan
    joined = "\n".join(outputs)
    assert "Startup scan complete" not in joined  # startup scan skipped
    assert (
        f"Balance (testnet): {EXPECTED_CONFIRMED} sats (confirmed)" in joined
    )
    # Fresh-store path: the app created exactly one wallet row itself.
    with Store(store_path) as store:
        assert len(store.list_wallets()) == 1


def test_repl_reads_zpub_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ZPUB_ENV_VAR, VPUB)
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    addr0, addr1 = derive_fixture_addresses(2)
    handler = _scan_handler(
        recorded, utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1}
    )

    code, outputs = _run_captured(
        ["--stub-llm"], monkeypatch, handler, ["What's my balance?", "quit"],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert f"Balance (testnet): {EXPECTED_CONFIRMED} sats" in joined
    assert VPUB not in joined  # env-sourced key never echoed either


def test_zpub_cli_arg_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Env holds a key the Phase 1 gate would refuse; the CLI arg must win.
    monkeypatch.setenv(ZPUB_ENV_VAR, MAINNET_ZPUB)
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["exit"],
        store_path=store_path,
    )

    assert code == 0
    assert PRIVACY_INDICATOR in "\n".join(outputs)


def test_repl_reports_chain_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    handler = _scan_handler([], utxo_status=503)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
        auto_scan=True,
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Startup scan failed → scrubbed warning, but the REPL still started.
    assert "warning: startup scan failed" in joined
    assert "chain unavailable" in joined
    assert "Balance (testnet):" not in joined


def test_rescan_flag_repairs_stale_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--rescan: re-derives from the key, rebuilds derivation state and
    replaces the stale UTXO snapshot with chain truth (Phase 1 AC)."""
    store_path = _store_path(tmp_path)
    wd = WalletDescriptor.from_key(VPUB)
    parsed = wd.parsed
    addr0 = derive_addresses(parsed, 0, 0, 1)[0].address
    stale_txid = "f" * 64
    fresh_txid = "ab" * 32
    with Store(store_path) as store:
        wallet = store.create_wallet("default", wd.descriptor)
        store.set_active_wallet(wallet.id)
        store.set_setting(GAP_LIMIT_SETTING, str(TEST_GAP))
        # Stale cache: a bogus 1-sat UTXO, zeroed derivation, stale cursor.
        store.upsert_batch(
            [
                AddressRecord(
                    wallet_id=wallet.id, branch=0, index=0, address=addr0,
                    script_type="p2wpkh", status="unused",
                )
            ]
        )
        store.replace_utxos_for_wallet(
            wallet.id,
            [
                UtxoRecord(
                    wallet_id=wallet.id, txid=stale_txid, vout=0, address=addr0,
                    value_sats=1, confirmed=1, height=800_000,
                )
            ],
        )
        store.set_sync_state(wallet.id, "last_scan_cursor", '{"0": 2, "1": 2}')

    recorded: list[httpx.Request] = []
    handler = _scan_handler(
        recorded,
        txs_by_addr={addr0: [_tx_entry(fresh_txid, vout_addresses=(addr0,))]},
        utxos_by_addr={
            addr0: [
                {
                    "txid": fresh_txid,
                    "vout": 0,
                    "value": 50_000,
                    "status": {"confirmed": True, "block_height": 800_000},
                }
            ]
        },
    )

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB, "--rescan"],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Counts-only summary (no addresses, no amounts).
    rescan_line = next(line for line in outputs if line.startswith("Rescan complete:"))
    assert "branch 0: scanned 3, max used 0, next index 1" in rescan_line
    assert "branch 1: scanned 2, max used -1, next index 0" in rescan_line
    assert "1 UTXOs · tip height 870000" in rescan_line
    assert addr0 not in rescan_line and "50000" not in rescan_line
    # The stale snapshot was replaced wholesale by chain truth; derivation
    # was recomputed from chain usage (next = max_used + 1).
    with Store(store_path) as store:
        wallet_row = store.get_active_wallet()
        assert wallet_row is not None and wallet_row.id == wallet.id
        utxos = store.get_utxos_for_wallet(wallet.id)
        assert [(u.txid[:4], u.value_sats) for u in utxos] == [("abab", 50_000)]
        deriv = store.get_derivation(wallet.id, 0)
        assert (deriv.max_used_index, deriv.next_index) == (0, 1)
    # And the post-rescan balance reads the repaired cache.
    assert "Balance (testnet): 50000 sats (confirmed) + 0 sats (unconfirmed)" in joined
    # The out-of-window warning stays cleared: usage stayed inside the window.
    assert "usage was found beyond your usual address window" not in joined


def test_out_of_window_warning_printed_from_sync_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0009 UI surfacing: a non-empty out_of_window_detected payload in
    sync_state prints the generic startup warning line."""
    store_path = _store_path(tmp_path)
    wd = _preset_store(store_path)
    with Store(store_path) as store:
        wallet = store.get_wallet_by_name("default")
        assert wallet is not None
        store.set_sync_state(
            wallet.id,
            "out_of_window_detected",
            json.dumps(
                {
                    "detected_at": "2026-08-31T00:00:00+00:00",
                    "branches": {
                        "0": {"max_used_index": 30, "previous_window_end": 19}
                    },
                }
            ),
        )
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["exit"],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert OUT_OF_WINDOW_NOTICE in joined
    assert "usage was found beyond your usual address window" in joined
    # The warning line is index-free/scrubbed: no addresses, no key material.
    assert VPUB not in joined and wd.descriptor not in joined


def test_out_of_window_warning_absent_without_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["exit"],
        store_path=store_path,
    )

    assert code == 0
    assert "usage was found beyond your usual address window" not in "\n".join(outputs)


def test_duplicate_descriptor_startup_reuses_wallet_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Duplicate-descriptor guard: a store already holding the same
    descriptor is reused — repeated startups never duplicate the row."""
    store_path = _store_path(tmp_path)
    wd = _preset_store(store_path)
    with Store(store_path) as store:
        existing = store.get_wallet_by_name("default")
        assert existing is not None

    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)
    for _ in range(2):
        code, _outputs = _run_captured(
            ["--stub-llm", "--zpub", VPUB],
            monkeypatch,
            handler,
            ["exit"],
            store_path=store_path,
        )
        assert code == 0

    with Store(store_path) as store:
        rows = store.list_wallets()
        assert len(rows) == 1
        assert rows[0].id == existing.id
        assert rows[0].descriptor == wd.descriptor
        active = store.get_active_wallet()
        assert active is not None and active.id == existing.id


def test_repl_new_address_narration_and_persistence_across_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'give me a new address' via the REPL: verbatim narration, and the
    allocation state survives in the store — a second run yields the NEXT
    index."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)
    parsed = _fixture_parsed()
    expected0 = derive_addresses(parsed, 0, 0, 1)[0].address
    expected1 = derive_addresses(parsed, 0, 1, 1)[0].address

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["give me a new address", "exit"],
        store_path=store_path,
    )
    assert code == 0
    assert f"Fresh receive address (index 0): {expected0}" in "\n".join(outputs)
    assert recorded == []  # allocation is network-free

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["give me a new address", "exit"],
        store_path=store_path,
    )
    assert code == 0
    assert f"Fresh receive address (index 1): {expected1}" in "\n".join(outputs)

    with Store(store_path) as store:
        wallet = store.get_wallet_by_name("default")
        assert wallet is not None
        assert store.get_derivation(wallet.id, 0).next_index == 2
        statuses = {r.index: r.status for r in store.get_addresses(wallet.id, 0)}
        assert statuses == {0: "allocated", 1: "allocated"}


def test_repl_history_narration_with_stub_phrase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'show my recent transactions' through the stub model → get_history
    (default limit 20) → one narration line per tx, address-free."""
    store_path = _store_path(tmp_path)
    wd = _preset_store(store_path)
    with Store(store_path) as store:
        wallet = store.get_wallet_by_name("default")
        assert wallet is not None
        _seed_txs(store, wallet.id)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["show my recent transactions", "exit"],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    tx_lines = [line for line in outputs if line.startswith("tx ")]
    assert len(tx_lines) == 20  # default limit honored
    # Unconfirmed first, then height DESC.
    assert tx_lines[0] == f"tx {('ee' * 32)[:12]}… out unconfirmed"
    assert tx_lines[1] == f"tx {(f'{24:02x}' * 32)[:12]}… in 800024"
    for addr in derive_fixture_addresses(3):
        assert addr not in joined
    assert wd.descriptor not in joined


def test_repl_refuses_mainnet_zpub_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = _store_path(tmp_path)
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", MAINNET_ZPUB],
        monkeypatch,
        lambda _req: None,  # the client factory is never reached
        [],
        store_path=store_path,
    )
    assert code == 2
    joined = "\n".join(outputs)
    assert "testnet-only" in joined
    assert MAINNET_ZPUB not in joined
    # Fail-closed before any store side effects for the rejected key.
    assert not store_path.exists()


def test_repl_without_zpub_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ZPUB_ENV_VAR, raising=False)
    code, outputs = _run_captured(
        ["--stub-llm"], monkeypatch, lambda _req: None, []
    )
    assert code == 2
    assert "No watch key configured" in "\n".join(outputs)
    assert ZPUB_ENV_VAR in "\n".join(outputs)


def test_repl_without_model_or_stub_flag_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(handler))

    outputs: list[str] = []
    code = run(["--zpub", VPUB], input_fn=lambda _p: "exit", output_fn=outputs.append)

    assert code == 2
    joined = "\n".join(outputs)
    assert "No model configured" in joined
    assert "LOCALWALLET_MODEL_PATH" in joined
    assert "--stub-llm" in joined
    assert recorded == []  # no chain I/O on the config-error path


def test_store_path_into_a_file_fails_cleanly_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OSError on store open (mkdir fails because a path component is a
    file) is a clean exit-2 config failure with the store-failure message,
    not a traceback."""
    blocker = tmp_path / "f"
    blocker.write_text("not a directory")
    store_path = blocker / "db.sqlite"

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        lambda _req: None,
        [],
        store_path=store_path,
    )
    assert code == 2
    joined = "\n".join(outputs)
    assert "Could not open the wallet store" in joined
    assert "Traceback" not in joined


def test_startup_chain_down_repl_still_starts_and_balance_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fresh store + unreachable chain: the startup scan warns (scrubbed)
    and the REPL still starts; the balance handler's lazy scan fails and
    surfaces the graceful chain_unavailable error path."""
    store_path = _store_path(tmp_path)
    handler = _scan_handler([], utxo_status=503)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
        auto_scan=True,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "warning: startup scan failed" in joined
    assert "chain unavailable" in joined
    assert "Balance (testnet):" not in joined
    # The wallet row was still created; the store simply stays empty.
    with Store(store_path) as store:
        assert len(store.list_wallets()) == 1


# ----------------------------------------------- send flow (TCK-P2-004)
#
# Full destructive-flow pipeline WITHOUT network/model: REPL turns →
# AgentLoop (fake generate closures quoting the flow's REAL pending
# tx_ref) → handle_raw validation → allowlist dispatch → create/confirm
# handlers → store + mock chain (utxos, fees, prices) → TxFlow state
# machine → confirmation-card narration. The dual-key rule (ADR-0013) is
# exercised through the real REPL gate wiring (_run_turn), not by
# calling handlers directly.


SEND_FEES_PAYLOAD: Final[dict[str, int]] = {
    "fastestFee": 3,
    "halfHourFee": 2,
    "hourFee": 1,
    "economyFee": 1,
    "minimumFee": 1,
}
SEND_PRICE_USD: Final[float] = 20_000.0

#: Recipient fixture: branch-0 index 9 of the fixture key — a valid
#: testnet P2WPKH address OUTSIDE the gap-2 scan window, so the chain
#: mock never confuses it with a wallet address.
SEND_RECIPIENT: Final[str] = derive_addresses(_fixture_parsed(), 0, 9, 1)[0].address

#: One confirmed 100_000-sat UTXO at the first receive address: funds a
#: 60_000-sat send at 2 sat/vB → 1 input, vsize 141, fee 282, change 39718.
SEND_UTXO: Final[dict[str, Any]] = {
    "txid": "d" * 64,
    "vout": 0,
    "value": 100_000,
    "status": {"confirmed": True},
}
SEND_UTXO_SMALL: Final[dict[str, Any]] = {
    "txid": "e" * 64,
    "vout": 0,
    "value": 10_000,
    "status": {"confirmed": True},
}

RESPOND_NOTED_JSON: Final[str] = '{"v": 0, "intent": "respond", "params": {"text": "Noted."}}'

#: The txid the mock broadcast endpoint reports (64 lowercase hex); the
#: status endpoint serves the matching confirmed payload for it.
BROADCAST_TXID: Final[str] = "ee" * 32

GET_HISTORY_JSON: Final[str] = '{"v": 0, "intent": "get_history", "params": {}}'

# Expected card numbers for the canonical fixture send (deterministic
# selection math; fee == vsize × rate asserted independently below).
SEND_AMOUNT_SATS: Final[int] = 60_000
SEND_FEE_SATS: Final[int] = 282
SEND_VSIZE: Final[int] = 141
SEND_CHANGE_SATS: Final[int] = 39_718
SEND_USD_CENTS: Final[int] = 1_200  # 60000 sats @ 20000 USD/BTC


def _send_chain_handler(
    recorded: list[httpx.Request],
    *,
    utxos_by_addr: dict[str, list[dict[str, Any]]],
    state: dict[str, Any] | None = None,
    tip: int = TIP_HEIGHT,
) -> Callable[[httpx.Request], httpx.Response]:
    """MockTransport handler for send-flow tests.

    Serves per-address txs (always empty — keeps the scan window at the
    gap) and utxo payloads, plus ``/v1/fees/recommended``,
    ``/v1/prices``, the Phase 3 broadcast POST (``/tx``) and the tx
    status GET (``/tx/<txid>/status``). ``state`` is a mutable injection
    point for the tests:

    - ``state["fees_fail"]`` / ``state["prices_fail"]`` flip those
      endpoints to 500 mid-test;
    - ``state["broadcast_fail"]`` flips the broadcast POST to 500;
    - ``state["broadcast_txid"]`` overrides the txid the POST returns
      (default ``BROADCAST_TXID``);
    - ``state["broadcast_posts"]`` records every POST body (single-
      attempt assertions);
    - ``state["status_fail"]`` / ``state["status_404"]`` flip the status
      endpoint to 500 / 404;
    - ``state["tx_status_payload"]`` overrides the status JSON payload.
    """
    state = state if state is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        path = request.url.path
        if path.endswith("/v1/fees/recommended"):
            if state.get("fees_fail"):
                return httpx.Response(500, json=None)
            return httpx.Response(200, json=SEND_FEES_PAYLOAD)
        if path.endswith("/v1/prices"):
            if state.get("prices_fail"):
                return httpx.Response(500, json=None)
            return httpx.Response(
                200, json={"time": 1_700_000_000, "USD": state.get("usd", SEND_PRICE_USD)}
            )
        if path.endswith("/blocks/tip"):
            return httpx.Response(200, json=tip)
        if path.endswith("/tx") and request.method == "POST":
            state.setdefault("broadcast_posts", []).append(request.content.decode("ascii"))
            if state.get("broadcast_fail"):
                return httpx.Response(500, text="boom")
            return httpx.Response(200, text=state.get("broadcast_txid", BROADCAST_TXID))
        if path.endswith("/status"):
            if state.get("status_fail"):
                return httpx.Response(500, json=None)
            if state.get("status_404"):
                return httpx.Response(404, json=None)
            payload = state.get("tx_status_payload")
            if payload is not None:
                return httpx.Response(200, json=payload)
            return httpx.Response(
                200,
                json={"confirmed": True, "block_height": 870_001, "block_time": 1_700_000_500},
            )
        parts = path.rstrip("/").split("/")
        address, kind = parts[-2], parts[-1]
        if kind == "txs":
            return httpx.Response(200, json=[])
        if kind == "utxo":
            return httpx.Response(200, json=utxos_by_addr.get(address, []))
        return httpx.Response(404, json=None)

    return handler


def _build_send_table(
    make_handler: Callable[[list[httpx.Request]], Callable[[httpx.Request], httpx.Response]],
    *,
    flow: Any | None = None,
    make_price_oracle: Callable[[EsploraClient], Any] | None = None,
    gap_limit: int | None = TEST_GAP,
    signer: Any | None = None,
    signer_selection: Any | None = None,
) -> tuple[dict[IntentName, Any], Store, Any, EsploraClient, list[httpx.Request], Any, SendSession]:
    """Send-flow dispatch table: like :func:`_build_table` but returning
    the shared ``TxFlow``/``SendSession`` pair the handlers own, and
    accepting a price-oracle factory for oracle-behavior tests plus the
    TCK-P3-005 signer seams (``signer`` object override /
    ``signer_selection`` config override)."""
    recorded: list[httpx.Request] = []
    client = _mock_client(make_handler(recorded))
    store = Store.memory()
    wd = WalletDescriptor.from_key(VPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    if gap_limit is not None:
        store.set_setting(GAP_LIMIT_SETTING, str(gap_limit))
    tx_flow = flow if flow is not None else TxFlow()
    session = SendSession()
    table = build_dispatch_table(
        store,
        wallet,
        wd.parsed,
        client,
        lambda: scan_wallet(store, client, wallet),
        flow=tx_flow,
        session=session,
        price_oracle=None if make_price_oracle is None else make_price_oracle(client),
        signer=signer,
        signer_selection=signer_selection,
    )
    return table, store, wallet, client, recorded, tx_flow, session


def _create_tx_envelope_json(params: dict[str, Any] | None = None) -> str:
    """Canned ``create_tx`` model output.

    ``None`` → the canonical fixture send (60_000 sats to the fixture
    recipient). A ``params`` dict replaces the amount keys wholesale
    (merged over the recipient default only) so exactly-one-amount stays
    intact — e.g. ``{"recipient": ..., "amount_usd": 12}``.
    """
    if params is None:
        body: dict[str, Any] = {"recipient": SEND_RECIPIENT, "amount_sats": SEND_AMOUNT_SATS}
    else:
        body = {"recipient": SEND_RECIPIENT, **params}
    return json.dumps({"v": 0, "intent": "create_tx", "params": body})


def _send_generate(flow: Any, plan: list[str], create_params: dict[str, Any] | None = None) -> GenerateFn:
    """Fake generate_fn for send-flow e2e tests.

    ``plan`` entries: ``"create"`` → the canned create_tx envelope;
    ``"confirm"`` → a confirm_tx envelope quoting the flow's REAL pending
    ``tx_ref`` at call time (the stub cannot know it — this closure can);
    ``"sign"`` → sign_tx quoting the confirmed record's ``tx_ref``;
    ``"broadcast"`` → broadcast_tx quoting the signed record's ``tx_ref``;
    ``"respond"`` → a canned respond; any other string is emitted
    verbatim (e.g. a hand-written confirm_tx with a bogus ref). Beyond
    the plan, canned ``respond`` forever.
    """
    state = {"n": 0}

    def generate(prompt: str, grammar_text: str | None) -> str:
        del prompt, grammar_text
        step = plan[state["n"]] if state["n"] < len(plan) else RESPOND_NOTED_JSON
        state["n"] += 1
        if step == "create":
            return _create_tx_envelope_json(create_params)
        if step == "confirm":
            assert flow.pending is not None, "test bug: no pending tx to reference"
            return json.dumps(
                {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": flow.pending.tx_ref}}
            )
        if step == "sign":
            assert flow.confirmed is not None, "test bug: no confirmed tx to reference"
            return json.dumps(
                {"v": 0, "intent": "sign_tx", "params": {"tx_ref": flow.confirmed.tx_ref}}
            )
        if step == "broadcast":
            assert flow.signed is not None, "test bug: no signed tx to reference"
            return json.dumps(
                {"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": flow.signed.tx_ref}}
            )
        if step == "respond":
            return RESPOND_NOTED_JSON
        return step

    return generate


class FactsQuotingGenerate:
    """PRODUCTION-PATH fake model: quotes values from the injected FACTS.

    Unlike :func:`_send_generate` (the old seam, which captures the
    ``TxFlow`` object), this fake NEVER sees the flow — it only sees the
    assembled prompt, exactly like the real model. Plan steps:

    - ``"create"`` → the canned create_tx envelope;
    - ``"confirm"`` → extracts ``pending_tx_ref`` from the prompt's FACTS
      block and quotes it VERBATIM in a ``confirm_tx`` envelope;
    - ``"sign"`` → extracts ``confirmed_tx_ref`` → ``sign_tx``;
    - ``"broadcast"`` → extracts ``signed_tx_ref`` → ``broadcast_tx``;
    - ``"status"`` → extracts ``broadcast_txid`` → ``tx_status``;
    - ``"history"`` → the canned get_history envelope;
    - ``"respond"`` → a canned respond.

    This is precisely what the system prompt instructs the production
    model to do (quote verbatim from the FACTS/confirmation context) —
    the P2-004 lesson extended to the full TCK-P3-005 lifecycle. Every
    prompt received is recorded for assertions.
    """

    def __init__(self, plan: list[str]) -> None:
        self.plan = list(plan)
        self.prompts: list[str] = []
        self._n = 0

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        step = self.plan[self._n] if self._n < len(self.plan) else RESPOND_NOTED_JSON
        self._n += 1
        if step == "create":
            return _create_tx_envelope_json()
        if step in ("confirm", "sign", "broadcast", "status"):
            key = {
                "confirm": "pending_tx_ref",
                "sign": "confirmed_tx_ref",
                "broadcast": "signed_tx_ref",
                "status": "broadcast_txid",
            }[step]
            match = re.search(rf"^{key}: (\S+)$", prompt, re.MULTILINE)
            assert match is not None, f"test bug: no {key} fact in the prompt"
            intent = {
                "confirm": "confirm_tx",
                "sign": "sign_tx",
                "broadcast": "broadcast_tx",
                "status": "tx_status",
            }[step]
            param_key = "txid" if step == "status" else "tx_ref"
            return json.dumps(
                {"v": 0, "intent": intent, "params": {param_key: match.group(1)}}
            )
        if step == "history":
            return GET_HISTORY_JSON
        if step == "respond":
            return RESPOND_NOTED_JSON
        return step


def _run_send_repl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    lines: list[str],
    plan: list[str],
    *,
    flow: Any | None = None,
    create_params: dict[str, Any] | None = None,
    extra_env: dict[str, str | None] | None = None,
    generate: GenerateFn | None = None,
    before_line: Callable[[], None] | None = None,
) -> tuple[int, list[str], Any]:
    """Run app.run() over the real REPL with the send-flow fixtures.

    Returns ``(exit_code, output_lines, flow)`` — the flow is the very
    instance the handlers used, so tests assert dispatcher-owned state.
    ``generate`` replaces the default flow-capturing
    :func:`_send_generate` seam wholesale (production-path tests); when
    ``before_line`` is given it runs just before each input line is
    returned (e.g. to advance an injected clock mid-session).
    """
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    for name, value in (extra_env or {}).items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(handler))
    tx_flow = flow if flow is not None else TxFlow()
    fake = generate if generate is not None else _send_generate(tx_flow, plan, create_params)
    inputs = iter(lines)

    def read_line(_prompt: str) -> str:
        if before_line is not None:
            before_line()
        return next(inputs)

    outputs: list[str] = []
    code = run(
        ["--zpub", VPUB],
        input_fn=read_line,
        output_fn=outputs.append,
        flow=tx_flow,
        generate_fn=fake,
    )
    return code, outputs, tx_flow


def _card_refs(outputs: list[str]) -> list[str]:
    """All ``Ref:`` values shown by confirmation cards, in order."""
    return [line.split("Ref: ", 1)[1] for line in outputs if line.startswith("Ref: ")]


def test_send_flow_happy_path_card_then_dual_key_confirm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'send 60000 sats …' → card with EXACT selection values → 'yes
    please' + model confirm_tx (real tx_ref) → Approved + CONFIRMED."""
    addr0 = derive_fixture_addresses(1)[0]
    recorded: list[httpx.Request] = []
    handler = _send_chain_handler(recorded, utxos_by_addr={addr0: [SEND_UTXO]})

    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "exit"],
        ["create", "confirm"],
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Card lines, exact values verbatim from the handler result dict.
    assert "Pending transaction — review it carefully" in joined
    assert f"Amount: {SEND_AMOUNT_SATS} sats ($12.00 · rate age 0s)" in joined
    assert f"To: {SEND_RECIPIENT}" in joined
    assert f"Fee: {SEND_FEE_SATS} sats (2 sat/vB, medium target)" in joined
    assert f"Size: {SEND_VSIZE} vB" in joined
    assert "Inputs: 1" in joined
    assert f"Change: {SEND_CHANGE_SATS} sats" in joined
    assert "Expires: ~10 min" in joined
    # Independent money-math cross-checks of the card figures.
    assert SEND_FEE_SATS == SEND_VSIZE * 2  # fee == vsize × rate
    assert SEND_AMOUNT_SATS + SEND_FEE_SATS + SEND_CHANGE_SATS == 100_000
    # Dual-key confirm: same-turn "yes please" + matching tx_ref.
    assert (
        "Approved. The signed-transaction step arrives in Phase 3 — say 'status' later."
        in joined
    )
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.pending is None
    assert "Not confirmed" not in joined
    # The chain saw the lazy scan + fees + prices; the PSBT never prints.
    assert any(r.url.path.endswith("/v1/fees/recommended") for r in recorded)
    assert any(r.url.path.endswith("/v1/prices") for r in recorded)
    assert any(r.url.path.endswith("/utxo") for r in recorded)
    assert "cHNj" not in joined  # no base64 PSBT payload in narration


def test_send_flow_handler_result_card_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The create_tx handler result carries exactly the confirmation-card
    contract fields (plus the rate timestamp), verbatim from the flow."""
    addr0 = derive_fixture_addresses(1)[0]
    table, store, wallet, client, _recorded, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]})
    )
    envelope = validate_payload(_create_tx_envelope_json())

    result = table[IntentName.CREATE_TX](envelope)

    assert set(result.keys()) == {
        "tx_ref",
        "amount_sats",
        "recipient",
        "fee_sats",
        "fee_rate_sat_vb",
        "vsize",
        "change_sats",
        "inputs_count",
        "usd_cents",
        "rate_stale",
        "rate_age_s",
        "rate_fetched_at",
        "fee_target",
        "expires_in_s",
    }
    assert result["recipient"] == SEND_RECIPIENT
    assert result["fee_sats"] == result["vsize"] * result["fee_rate_sat_vb"]
    assert result["amount_sats"] == SEND_AMOUNT_SATS
    assert result["change_sats"] == SEND_CHANGE_SATS
    assert result["usd_cents"] == SEND_USD_CENTS
    assert result["rate_stale"] is False
    assert result["rate_age_s"] == 0
    assert result["fee_target"] == "medium"  # MEDIUM default when omitted
    assert result["expires_in_s"] == 600
    # Flow owns the staged record with identical numbers.
    pending = flow.pending
    assert pending is not None
    assert pending.tx_ref == result["tx_ref"]
    assert pending.fee_sats == result["fee_sats"]
    # ADR-0009: the fresh change index was allocated only after the build;
    # the scan's gap window had prefetched index 1 as 'unused' (kept).
    assert store.get_derivation(wallet.id, 1).next_index == 1
    assert [(r.index, r.status) for r in store.get_addresses(wallet.id, 1)] == [
        (0, "allocated"),
        (1, "unused"),
    ]
    client.close()
    store.close()


def test_send_flow_confirm_direct_with_gate_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """confirm_tx handler: CONFIRMED needs the session's same-turn gate
    decision — present → confirmed dict with the unsigned PSBT; absent →
    value-free refusal, flow untouched."""
    addr0 = derive_fixture_addresses(1)[0]
    table, store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]})
    )
    create_env = validate_payload(_create_tx_envelope_json())
    created = table[IntentName.CREATE_TX](create_env)
    confirm_env = validate_payload(
        json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": created["tx_ref"]}})
    )

    # Key 2 missing (user never said yes on this turn): refused, CREATED.
    refused = table[IntentName.CONFIRM_TX](confirm_env)
    assert refused["error"] == "confirm_refused"
    assert SEND_RECIPIENT not in str(refused["detail"])  # value-free detail
    assert str(SEND_AMOUNT_SATS) not in str(refused["detail"])
    assert flow.state is TxFlowStatus.CREATED

    # Both keys on the same turn: CONFIRMED with the signer handoff.
    session.gate_decision = GateDecision.CONFIRM
    confirmed = table[IntentName.CONFIRM_TX](confirm_env)
    assert confirmed["status"] == "confirmed"
    assert confirmed["tx_ref"] == created["tx_ref"]
    assert confirmed["message"] == "ready for signing (Phase 3)"
    psbt = base64.b64decode(confirmed["psbt_base64"])  # round-trips as base64
    assert psbt[:5] == b"psbt\xff"  # BIP 174 magic
    assert flow.state is TxFlowStatus.CONFIRMED
    client.close()
    store.close()


def test_send_flow_dual_key_model_confirms_user_silent_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(a) Model emits confirm_tx but the user's same-turn utterance
    ('whatever') is NOT_A_DECISION → confirm_refused narration, state
    stays CREATED (the refusal message IS the UX)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler(
        [], utxos_by_addr={addr0: [SEND_UTXO]}
    )
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "whatever", "exit"],
        ["create", "confirm"],
    )
    joined = "\n".join(outputs)
    assert "Not confirmed — confirmation gate not satisfied" in joined
    assert "the user has not explicitly confirmed this transaction" in joined
    assert flow.state is TxFlowStatus.CREATED
    assert "Approved." not in joined
    # Value-free refusal: the recipient never appears in it.
    refusal = next(line for line in outputs if line.startswith("Not confirmed"))
    assert SEND_RECIPIENT not in refusal


def test_send_flow_dual_key_user_yes_model_respond_stays_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(b) User says 'yes' but the model emits respond (no confirm_tx) →
    no flow transition; guidance line; state stays CREATED."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes", "exit"],
        ["create", "respond"],
    )
    joined = "\n".join(outputs)
    assert "Noted." in joined  # the model's respond passed through
    assert "The transaction is still pending — say 'confirm'" in joined
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is not None
    assert "Approved." not in joined


def test_send_flow_deny_cancels_and_allows_new_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'no thanks' while pending → the gate DENY is authoritative: the
    flow cancels proactively, the cancellation is narrated, and a new
    create succeeds afterwards (fresh tx_ref)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "no thanks",
            f"send 60000 sats to {SEND_RECIPIENT}",
            "exit",
        ],
        ["create", "respond", "create"],
    )
    joined = "\n".join(outputs)
    assert "Transaction cancelled." in joined
    refs = _card_refs(outputs)
    assert len(refs) == 2
    assert refs[0] != refs[1]  # a fresh pending transaction, new identity
    assert "already pending" not in joined
    assert flow.state is TxFlowStatus.CREATED


def test_send_flow_expired_pending_refuses_confirm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Injected clock past PENDING_TTL_S: the confirm attempt transitions
    CREATED → EXPIRED and the refusal names the expiry."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    times = iter([1_000.0, 1_700.0])

    def clock() -> float:
        return next(times, 1_700.0)

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "exit"],
        ["create", "confirm"],
        flow=TxFlow(clock=clock),
    )
    joined = "\n".join(outputs)
    assert "Not confirmed — pending transaction expired." in joined
    assert flow.state is TxFlowStatus.EXPIRED
    assert flow.pending is None


def test_send_flow_duplicate_pending_reshows_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'send …' while a transaction is already pending → tx_pending
    refusal plus the SAME pending card re-shown (same tx_ref)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            f"send 60000 sats to {SEND_RECIPIENT}",
            "exit",
        ],
        ["create", "create"],
    )
    joined = "\n".join(outputs)
    assert "A transaction is already pending — confirm or cancel it first." in joined
    refs = _card_refs(outputs)
    assert len(refs) == 2
    assert refs[0] == refs[1]  # the SAME pending transaction re-shown
    assert flow.state is TxFlowStatus.CREATED


def test_send_flow_insufficient_funds_friendly_line_no_flow_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Amount over balance → friendly needed/available line (user-facing
    amounts per ADR-0012) and NO pending entry; also proves the lazy
    scan ran before selection (empty store → chain → still short)."""
    addr0 = derive_fixture_addresses(1)[0]
    table, store, wallet, client, recorded, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO_SMALL]})
    )
    # table-level: no cursor → the handler itself must lazy-scan first.
    assert store.get_sync_state(wallet.id, "last_scan_cursor") is None
    envelope = validate_payload(_create_tx_envelope_json())

    result = table[IntentName.CREATE_TX](envelope)

    assert result == {
        "error": "insufficient_funds",
        "needed_sats": 60_220,  # 60000 + vsize(1-in, no change) 110 × 2
        "available_sats": 10_000,
    }
    assert store.get_sync_state(wallet.id, "last_scan_cursor") is not None
    assert any(r.url.path.endswith("/utxo") for r in recorded)
    assert flow.state is TxFlowStatus.IDLE
    assert flow.pending is None
    # No allocation happened on the failed selection (nothing to roll
    # back): the change branch keeps only the scan's 'unused' prefetch.
    assert store.get_derivation(wallet.id, 1).next_index == 0
    assert [r.status for r in store.get_addresses(wallet.id, 1)] == ["unused", "unused"]

    outputs: list[str] = []
    app_module._print_create_tx(result, outputs.append)
    assert "Insufficient funds: need 60220 sats, have 10000 sats." in outputs
    client.close()
    store.close()


def test_send_flow_amount_usd_resolves_via_fresh_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """USD-denominated send: oracle fresh rate → sats resolved by the
    oracle's floor policy, card shows sats + USD with rate age."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send $12 to {SEND_RECIPIENT}", "exit"],
        ["create"],
        create_params={"recipient": SEND_RECIPIENT, "amount_usd": 12},
    )
    joined = "\n".join(outputs)
    # $12 @ 20000 USD/BTC → exactly 60000 sats → identical card figures.
    assert f"Amount: {SEND_AMOUNT_SATS} sats ($12.00 · rate age 0s)" in joined
    assert flow.state is TxFlowStatus.CREATED


def test_send_flow_stale_rate_marked_with_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle degrade ladder: warm cache past its TTL + failing prices
    endpoint → fresh() serves the STALE rate; the card marks it with the
    age, and the send still stages (stale rate still resolves amounts)."""
    from localwallet.chain import price as price_module

    addr0 = derive_fixture_addresses(1)[0]
    state: dict[str, Any] = {"prices_fail": False}
    clock = {"now": 1_000.0}
    monkeypatch.setattr(price_module, "_now", lambda: clock["now"])
    table, _store, _wallet, client, _recorded, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state),
        make_price_oracle=lambda client_: PriceOracle(client_, ttl_s=0.000001),
    )
    envelope = validate_payload(_create_tx_envelope_json())

    first = table[IntentName.CREATE_TX](envelope)
    assert first.get("error") is None
    assert first["rate_stale"] is False
    assert first["rate_age_s"] == 0
    fetched_at = first["rate_fetched_at"]
    assert fetched_at == 1_000.0

    flow.cancel()  # explicit recovery path to stage a second send
    state["prices_fail"] = True
    clock["now"] = 2_000.0

    second = table[IntentName.CREATE_TX](envelope)
    assert second.get("error") is None
    assert second["rate_stale"] is True
    assert second["rate_age_s"] == 1_000  # age from the injected clock
    assert second["rate_fetched_at"] == fetched_at  # same cached rate
    assert second["usd_cents"] == SEND_USD_CENTS
    assert flow.state is TxFlowStatus.CREATED
    client.close()


def test_send_flow_price_disabled_usd_path_refuses_without_flow_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oracle disabled by config + USD amount → price_unavailable handler
    error, and DO NOT create a flow entry (user retries or gives sats)."""
    addr0 = derive_fixture_addresses(1)[0]
    table, _store, _wallet, client, _recorded, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
        make_price_oracle=lambda client_: PriceOracle(client_, enabled=False),
    )
    envelope = validate_payload(
        _create_tx_envelope_json({"recipient": SEND_RECIPIENT, "amount_usd": 12})
    )

    result = table[IntentName.CREATE_TX](envelope)

    assert result["error"] == "price_unavailable"
    assert "price oracle is disabled by configuration" in str(result["detail"])
    assert flow.state is TxFlowStatus.IDLE
    client.close()


def test_send_flow_price_disabled_sats_path_still_sends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOCALWALLET_PRICE_ENABLED=0 + sats amount → the send proceeds; the
    card simply omits the USD segment (display-only sugar)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "exit"],
        ["create"],
        extra_env={"LOCALWALLET_PRICE_ENABLED": "0"},
    )
    joined = "\n".join(outputs)
    assert f"Amount: {SEND_AMOUNT_SATS} sats" in joined
    assert "rate age" not in joined
    assert f"Fee: {SEND_FEE_SATS} sats (2 sat/vB, medium target)" in joined
    assert flow.state is TxFlowStatus.CREATED


def test_send_flow_ambiguous_gate_guidance_state_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'yes no' (mixed signals) while pending → guidance asking for an
    explicit confirm/cancel; the flow is untouched."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes no", "exit"],
        ["create", "respond"],
    )
    joined = "\n".join(outputs)
    assert "That was ambiguous — say 'confirm'" in joined
    assert "or 'cancel'" in joined
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is not None


def test_send_flow_confirm_refused_tx_ref_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A schema-valid confirm_tx quoting the WRONG ref → value-free
    refusal naming the mismatch; flow stays CREATED."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    bogus = json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "bogus-ref"}})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "exit"],
        ["create", bogus],
    )
    joined = "\n".join(outputs)
    assert "Not confirmed — tx_ref does not match the pending transaction." in joined
    assert flow.state is TxFlowStatus.CREATED
    refusal = next(line for line in outputs if line.startswith("Not confirmed"))
    assert SEND_RECIPIENT not in refusal  # value-free error contract


def test_send_flow_fee_estimate_failure_surfaces_chain_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fees endpoint down → existing chain_unavailable error pattern, and
    no flow entry (fail closed before any money math)."""
    addr0 = derive_fixture_addresses(1)[0]
    state: dict[str, Any] = {"fees_fail": True}
    table, _store, _wallet, client, _r, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state)
    )
    envelope = validate_payload(_create_tx_envelope_json())
    result = table[IntentName.CREATE_TX](envelope)
    assert result["error"] == "chain_unavailable"
    assert result["detail"].strip() != ""
    assert flow.state is TxFlowStatus.IDLE
    outputs: list[str] = []
    app_module._print_create_tx(result, outputs.append)
    assert "Could not create the transaction — chain unavailable" in outputs[0]
    client.close()
    _store.close()


# ---------------------------------- send-flow SR fixes (TCK-P2-004 review)
#
# FIX 1 (facts injection): the production confirm path needs no
# flow-capturing seam — the pending card reaches the model as a FACTS
# block and the model quotes ``tx_ref`` from it. FIX 3 (expiry honesty):
# re-shown cards advertise the REMAINING ttl, not the nominal one.


def test_send_flow_confirm_production_path_quotes_tx_ref_from_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PRODUCTION-PATH confirm (FIX 1): the fake model never sees the
    flow — it parses the ``pending_tx_ref`` out of the prompt's FACTS
    block (the verbatim-quote contract) and emits ``confirm_tx`` with
    it. The full happy path (card → FACTS → confirm_tx → dual key →
    CONFIRMED) works without the old seam; the same-turn 'yes please'
    still supplies the gate's key."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fake = FactsQuotingGenerate(["create", "confirm"])
    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "exit"],
        ["create", "confirm"],
        generate=fake,
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Create turn: nothing pending yet → no pending facts in that prompt.
    assert "pending_tx_ref" not in fake.prompts[0]
    # Confirm turn: the pending context WAS injected, and quoting it
    # completed the flow (a wrong ref would refuse with a mismatch).
    match = re.search(r"^pending_tx_ref: (\S+)$", fake.prompts[1], re.MULTILINE)
    assert match is not None
    fact_ref = match.group(1)
    card_ref = next(
        line.split("Ref: ", 1)[1] for line in outputs if line.startswith("Ref: ")
    )
    assert fact_ref == card_ref  # FACTS value == the printed card's ref
    assert "Pending transaction — review it carefully" in joined
    assert "Approved. The signed-transaction step arrives in Phase 3" in joined
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.pending is None
    assert "Not confirmed" not in joined


def test_send_flow_facts_absent_when_no_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FIX 1 (b): with no pending transaction the prompt carries no
    pending-tx FACTS at all (facts stay {} on plain chat turns)."""
    handler = _send_chain_handler([], utxos_by_addr={})
    fake = FactsQuotingGenerate(["respond"])
    code, _outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        ["what can this app do?", "exit"],
        ["respond"],
        generate=fake,
    )

    assert code == 0
    assert flow.state is TxFlowStatus.IDLE
    assert len(fake.prompts) == 1
    assert "FACTS BEGIN" not in fake.prompts[0]  # empty facts → no block
    assert "pending_tx" not in fake.prompts[0]


def test_send_flow_facts_show_remaining_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FIX 1 (c): with the clock injected, the pending FACTS carry the
    REMAINING ttl — ~599s one second after staging, less later — plus the
    ref/amount/recipient the confirm must quote; quoting them still
    completes the flow under the dual key."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    clock = {"now": 1000.0}
    reads = {"n": 0}

    def before_line() -> None:
        reads["n"] += 1
        if reads["n"] == 2:  # just before the 'whatever' turn
            clock["now"] = 1001.0
        elif reads["n"] == 3:  # just before the 'yes please' turn
            clock["now"] = 1050.0

    fake = FactsQuotingGenerate(["create", "respond", "confirm"])
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "whatever",  # NOT_A_DECISION: flow stays pending, facts flow
            "yes please",
            "exit",
        ],
        ["create", "respond", "confirm"],
        flow=TxFlow(clock=lambda: clock["now"]),
        generate=fake,
        before_line=before_line,
    )

    joined = "\n".join(outputs)
    # Turn 2 runs 1s after staging: 600 − 1 = 599 s remaining, and the
    # full pending context is present for the model to quote.
    assert "pending_tx_expires_in_s: 599" in fake.prompts[1]
    assert "pending_tx_ref: " in fake.prompts[1]
    assert "pending_tx_amount_sats: 60000" in fake.prompts[1]
    assert f"pending_tx_recipient: {SEND_RECIPIENT}" in fake.prompts[1]
    # Turn 3 runs 50s after staging: less remaining than turn 2.
    assert "pending_tx_expires_in_s: 550" in fake.prompts[2]
    # The confirm quoted the FACTS ref → dual-key confirm completed.
    assert "Approved." in joined
    assert flow.state is TxFlowStatus.CONFIRMED


def test_send_flow_reshowed_card_shows_remaining_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FIX 3 (expiry honesty): re-showing the pending card 500s after
    staging advertises the REMAINING ttl (~1 min), not the nominal
    600s/10 min."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    clock = {"now": 1000.0}
    reads = {"n": 0}

    def before_line() -> None:
        reads["n"] += 1
        if reads["n"] == 2:  # just before the SECOND send attempt
            clock["now"] = 1500.0  # 500 s have passed

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            f"send 60000 sats to {SEND_RECIPIENT}",
            "exit",
        ],
        ["create", "create"],
        flow=TxFlow(clock=lambda: clock["now"]),
        before_line=before_line,
    )

    joined = "\n".join(outputs)
    assert "A transaction is already pending — confirm or cancel it first." in joined
    # First card: the full ttl; the re-shown card: 100 s ≈ 1 min left.
    assert "Expires: ~10 min" in joined
    assert "Expires: ~1 min" in joined
    assert flow.state is TxFlowStatus.CREATED


# --------------------------------------- send lifecycle (TCK-P3-005)
#
# Full sign/broadcast/status lifecycle WITHOUT network or model: REPL
# turns → AgentLoop (production-path FactsQuotingGenerate quoting refs
# and the txid from the injected FACTS blocks) → validation → allowlist
# dispatch → sign handler (file airgap or HWI fake device) → deterministic
# re-validation → broadcast via the mocked chain → status. The revalidation
# gate, flow discipline, single-attempt POST policy, §10 narration, and the
# store history row are all exercised here.

LIFECYCLE_HISTORY_JSON: Final[str] = '{"v": 0, "intent": "get_history", "params": {}}'


#: The fixture wallet's TRUE account derivation: ``_rederive_fixture_key``
#: builds the fixture vpub at ``m/84'/1'/0`` — a NON-hardened account
#: index. The app's PSBTs record the interim hardened-label convention
#: ``[84', 1', 0', branch, index]`` (account-key-own-fingerprint origin,
#: ADR-0009/0010, OQ18 device registration pending); re-validation checks
#: keys and signatures, never path labels, so the fake device derives the
#: account node the way the fixture was actually built and uses the
#: recorded leaf indexes (branch, index) from the PSBT derivation.
FIXTURE_ACCOUNT_DERIVATION: Final[list[int]] = [84 + 2**31, 1 + 2**31, 0]


def _simulate_device_sign(unsigned_b64: str, *, tamper: bool = False) -> str:
    """Fake hardware device: sign every PSBT input with the fixture key.

    The unsigned PSBT carries per-input BIP32 derivations; the fake device
    walks from its account node (see :data:`FIXTURE_ACCOUNT_DERIVATION`)
    along the recorded leaf indexes and signs the consensus BIP-143 digest
    (``psbt.sighash(i)``) — the same digest the re-validation gate
    verifies. ``tamper`` bumps the recipient output value by 546 sats
    AFTER signing (a tampered container for the hard-stop test).
    """
    from embit import bip32, ec
    from embit.psbt import PSBT as _PSBT

    psbt = _PSBT.parse(base64.b64decode(unsigned_b64))
    account = bip32.HDKey.from_seed(FIXTURE_SEED).derive(FIXTURE_ACCOUNT_DERIVATION)
    for i, scope in enumerate(psbt.inputs):
        (pub, derivation), = scope.bip32_derivations.items()
        priv = account.derive(derivation.derivation[-2:]).key
        digest = psbt.sighash(i)
        stream = BytesIO()
        ec.Signature.write_to(priv.sign(digest), stream)
        scope.partial_sigs[pub] = stream.getvalue() + b"\x01"
    if tamper:
        psbt.outputs[0].value += 546  # recipient value +546 (tamper matrix)
    return psbt.to_base64()


def _extract_signed_tx(psbt_b64: str) -> Any:
    """Independent extraction (embit finalizer) for test-side cross-checks."""
    from embit import finalizer as embit_finalizer
    from embit.psbt import PSBT as _PSBT

    return embit_finalizer.finalize_psbt(_PSBT.parse(base64.b64decode(psbt_b64)))


class _FakeDeviceClient:
    """Fake hwilib client: exposes the post-open fingerprint getter."""

    def __init__(self, fingerprint_hex: str, recorder: dict[str, bool]) -> None:
        self._fingerprint = bytes.fromhex(fingerprint_hex)
        self._recorder = recorder

    def get_master_fingerprint(self) -> bytes:
        return self._fingerprint

    def close(self) -> None:
        self._recorder["closed"] = True


class _FakeDeviceCommands:
    """hwilib.commands stand-in whose device REALLY signs with the fixture
    wallet key (enumerate → fingerprint → signtx, hwi 3.2.0 API shape).
    ``fail_first_sign`` raises a name-mapped locked error on the first
    signtx (DeviceLockedError mid-flow → guidance → retry works)."""

    class DeviceNotReadyError(Exception): ...  # name-mapped: locked guidance

    def __init__(self, fingerprint_hex: str, *, fail_first_sign: bool = False) -> None:
        self.fingerprint_hex = fingerprint_hex
        self.fail_first_sign = fail_first_sign
        self.sign_calls = 0
        self.rec: dict[str, bool] = {}
        self.client = _FakeDeviceClient(fingerprint_hex, self.rec)

    def enumerate(self, password=None):
        assert password is None, "our layer must never pass host-side secrets"
        return [
            {
                "type": "trezor",
                "path": "hid:fake",
                "model": "trezor_t",
                "fingerprint": self.fingerprint_hex,
            }
        ]

    def get_client(self, device_type, device_path, password=None, chain=None):
        return self.client

    def signtx(self, client, psbt):
        self.sign_calls += 1
        if self.fail_first_sign and self.sign_calls == 1:
            raise _FakeDeviceCommands.DeviceNotReadyError("locked mid-flight")
        return {"psbt": _simulate_device_sign(psbt)}


def test_send_lifecycle_file_signer_full_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PRODUCTION-PATH full lifecycle with the file signer: send → card →
    dual-key confirm → sign (export; signed_file_missing) → device places
    the signed file → sign again (import → revalidate → SIGNED) →
    broadcast (POST hits the mock) → status quotes broadcast_txid from
    FACTS → confirmed-at-height narration → history shows the outbound
    row. The broadcast POST carries exactly the re-validated transaction."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict[str, Any] = {}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)
    fake = FactsQuotingGenerate(
        ["create", "confirm", "sign", "sign", "broadcast", "status", "history"]
    )

    def before_line() -> None:
        # Device simulation between the two sign turns (see helper).
        if transfer.exists():
            unsigned = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
            signed_existing = list(transfer.glob("localwallet-signed-*.psbt.b64"))
            if unsigned and not signed_existing:
                unsigned_b64 = unsigned[0].read_text(encoding="utf-8").strip()
                ref8 = unsigned[0].name[len("localwallet-unsigned-") : -len(".psbt.b64")]
                signed_path = transfer / f"localwallet-signed-{ref8}.psbt.b64"
                signed_path.write_text(
                    _simulate_device_sign(unsigned_b64) + "\n", encoding="utf-8"
                )
                side = transfer / (signed_path.name + ".sha256")
                side.write_text(
                    hashlib.sha256(signed_path.read_bytes()).hexdigest() + "\n",
                    encoding="utf-8",
                )

    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "sign it again",
            "broadcast it",
            "what's the status?",
            "show my transactions",
            "exit",
        ],
        ["create", "confirm", "sign", "sign", "broadcast", "status", "history"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )

    assert code == 0
    joined = "\n".join(outputs)
    # --- sign turn 1: export + file-missing handoff line (§10) ----------
    assert "Exported to " in joined
    assert "(say: signed localwallet-signed-" in joined
    exported = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
    assert len(exported) == 1
    assert (transfer / (exported[0].name + ".sha256")).exists()  # ADR-0014 sidecar
    # --- sign turn 2: import → revalidate → SIGNED ----------------------
    signed_files = list(transfer.glob("localwallet-signed-*.psbt.b64"))
    assert len(signed_files) == 1  # the device simulation placed it
    expected_txid = _extract_signed_tx(
        signed_files[0].read_text(encoding="utf-8").strip()
    ).txid().hex()
    assert (
        f"Signed and verified ✓ txid {expected_txid}. "
        f"Ready to broadcast — say 'broadcast'." in joined
    )
    # No sidecar note: the simulated device file carries a matching sidecar.
    assert "integrity not verified" not in joined
    # --- broadcast: single POST with EXACTLY the re-validated tx --------
    posts = state["broadcast_posts"]
    assert len(posts) == 1
    expected_hex = _extract_signed_tx(flow.signed.psbt_base64).serialize().hex()
    assert posts[0] == expected_hex
    assert f"Sent! txid {BROADCAST_TXID} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST
    assert flow.txid == BROADCAST_TXID
    # --- status: the model quoted broadcast_txid from the FACTS ---------
    status_prompt = fake.prompts[5]
    assert f"broadcast_txid: {BROADCAST_TXID}" in status_prompt
    assert "Confirmed at height 870001." in joined
    # --- history: the outbound row (store upsert after broadcast) -------
    assert f"tx {BROADCAST_TXID[:12]}… out unconfirmed" in joined
    store_path = tmp_path / "store.db"
    with Store(store_path) as store:
        wallet_row = store.get_wallet_by_name("default")
        assert wallet_row is not None
        rows = store.get_txs_for_wallet(wallet_row.id)
        assert [(r.txid, r.height, r.direction, r.fee_sats) for r in rows] == [
            (BROADCAST_TXID, None, "out", SEND_FEE_SATS)
        ]


def test_send_lifecycle_hwi_signer_locked_retry_then_broadcast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HWI path with a fake commands module: fingerprint gate (incl. the
    post-open re-check) → DeviceLockedError mid-flow → §10 guidance line →
    'retry' → sign → revalidate → broadcast. Flow discipline throughout."""
    from localwallet.signer.hwi import HwiUsbSigner as RealHwiUsbSigner

    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fingerprint = _fixture_parsed().hd_key.my_fingerprint.hex()
    commands = _FakeDeviceCommands(fingerprint, fail_first_sign=True)
    monkeypatch.setattr(
        app_module,
        "HwiUsbSigner",
        lambda fp: RealHwiUsbSigner(fp, commands_module=commands),
    )
    fake = FactsQuotingGenerate(["create", "confirm", "sign", "sign", "broadcast"])

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "retry",
            "broadcast it",
            "exit",
        ],
        ["create", "confirm", "sign", "sign", "broadcast"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER": "hwi"},
    )

    joined = "\n".join(outputs)
    # First attempt: the locked device error surfaces its guidance VERBATIM
    # (code-owned §10 text). The retry: the fake device signs; revalidation
    # passes; the broadcast completes the lifecycle (end state below).
    assert "Enter your PIN/passphrase on the device, then say 'retry'." in joined
    assert "Signed and verified ✓ txid " in joined
    assert commands.sign_calls == 2  # the locked attempt + the retry
    assert commands.rec["closed"] is True  # device handle released both times
    assert f"Sent! txid {BROADCAST_TXID} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST


def test_send_lifecycle_revalidation_hard_stop_tampered_psbt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fake device returns a TAMPERED signed PSBT (recipient value
    +546): revalidation fails → hard stop, flow stays CONFIRMED, and
    broadcast is refused (no signed record exists to broadcast)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict[str, Any] = {}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)

    def before_line() -> None:
        if transfer.exists():
            unsigned = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
            signed_existing = list(transfer.glob("localwallet-signed-*.psbt.b64"))
            if unsigned and not signed_existing:
                unsigned_b64 = unsigned[0].read_text(encoding="utf-8").strip()
                ref8 = unsigned[0].name[len("localwallet-unsigned-") : -len(".psbt.b64")]
                signed_path = transfer / f"localwallet-signed-{ref8}.psbt.b64"
                signed_path.write_text(
                    _simulate_device_sign(unsigned_b64, tamper=True) + "\n",
                    encoding="utf-8",
                )

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "sign it again",
            "broadcast it",
            "exit",
        ],
        [
            "create",
            "confirm",
            "sign",
            "sign",
            # A literal broadcast envelope (placeholder ref): the flow is
            # still CONFIRMED after the hard stop, so the state gate fires.
            json.dumps(
                {"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": "bogus-ref"}}
            ),
        ],
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )

    joined = "\n".join(outputs)
    assert (
        "The signed transaction failed verification "
        "(extracted transaction output value does not match the intended "
        "transaction) — nothing was signed or sent; try signing again." in joined
    )
    assert flow.state is TxFlowStatus.CONFIRMED  # hard stop: flow untouched
    assert flow.signed is None
    # No broadcast path exists from CONFIRMED — and no POST hit the chain.
    assert "Not broadcast — no signed transaction to broadcast." in joined
    assert state.get("broadcast_posts", []) == []


def test_send_lifecycle_broadcast_5xx_stays_signed_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Broadcast POST 5xx → broadcast_failed narration, flow STAYS SIGNED;
    the retry succeeds. Exactly ONE POST per attempt (the chain layer's
    single-attempt policy — no automatic retry storm)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict[str, Any] = {"broadcast_fail": True}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)

    def before_line() -> None:
        if transfer.exists():
            unsigned = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
            signed_existing = list(transfer.glob("localwallet-signed-*.psbt.b64"))
            if unsigned and not signed_existing:
                unsigned_b64 = unsigned[0].read_text(encoding="utf-8").strip()
                ref8 = unsigned[0].name[len("localwallet-unsigned-") : -len(".psbt.b64")]
                signed_path = transfer / f"localwallet-signed-{ref8}.psbt.b64"
                signed_path.write_text(
                    _simulate_device_sign(unsigned_b64) + "\n", encoding="utf-8"
                )
        # Fail only the FIRST broadcast POST (single-attempt semantics):
        # once one POST has happened, re-enable success for the retry.
        if len(state.get("broadcast_posts", [])) >= 1:
            state["broadcast_fail"] = False

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "sign it again",
            "broadcast it",
            "broadcast it again",
            "exit",
        ],
        ["create", "confirm", "sign", "sign", "broadcast", "broadcast"],
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )

    joined = "\n".join(outputs)
    # First attempt: 500 → scrubbed failure, signed transaction kept (the
    # retry below only succeeds because the flow STAYED SIGNED).
    assert (
        "Broadcast failed (broadcast failed: status 500) — the signed "
        "transaction is kept; say 'broadcast' to retry." in joined
    )
    # Retry (the mock flips to success after the first POST): succeeds;
    # exactly one POST per attempt in total.
    assert f"Sent! txid {BROADCAST_TXID} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST
    assert len(state["broadcast_posts"]) == 2  # 1 per attempt, no retries


def test_send_lifecycle_signed_file_missing_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """sign_tx with the file signer before the user places the signed
    file: the §10 handoff line names the export path and the EXPECTED
    signed filename (deterministic from tx_ref); the unsigned file + its
    sidecar exist; the flow stays CONFIRMED."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "sign it", "exit"],
        ["create", "confirm", "sign"],
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
    )

    handoff = next(
        line for line in outputs if line.startswith("Exported to ")
    )
    assert "Move it to your SD card, sign on your device" in handoff
    assert "(say: signed localwallet-signed-" in handoff
    assert ".psbt.b64)." in handoff
    # The export really happened (payload + sidecar), nothing signed yet.
    unsigned = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
    assert len(unsigned) == 1
    assert (transfer / (unsigned[0].name + ".sha256")).exists()
    assert not list(transfer.glob("localwallet-signed-*.psbt.b64"))
    assert flow.state is TxFlowStatus.CONFIRMED


def test_send_lifecycle_unknown_tx_eventual_consistency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Right after broadcast the explorer may not have indexed the
    transaction: a 404 for the FLOW's broadcast txid → the unknown_tx
    narration (eventual consistency), while a 404 for any other txid is
    the ordinary chain-unavailable path."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict[str, Any] = {"status_404": True}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)
    fake = FactsQuotingGenerate(["create", "confirm", "sign", "sign", "broadcast", "status"])

    def before_line() -> None:
        if transfer.exists():
            unsigned = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
            signed_existing = list(transfer.glob("localwallet-signed-*.psbt.b64"))
            if unsigned and not signed_existing:
                unsigned_b64 = unsigned[0].read_text(encoding="utf-8").strip()
                ref8 = unsigned[0].name[len("localwallet-unsigned-") : -len(".psbt.b64")]
                signed_path = transfer / f"localwallet-signed-{ref8}.psbt.b64"
                signed_path.write_text(
                    _simulate_device_sign(unsigned_b64) + "\n", encoding="utf-8"
                )

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "sign it again",
            "broadcast it",
            "what's the status?",
            "exit",
        ],
        ["create", "confirm", "sign", "sign", "broadcast", "status"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )

    assert flow.state is TxFlowStatus.BROADCAST
    joined = "\n".join(outputs)
    # The flow's own txid, not yet indexed → the eventual-consistency line.
    assert (
        "Transaction not found on the chain yet — it may not be indexed; "
        "try again in a moment." in joined
    )
    # The status turn quoted broadcast_txid from the FACTS (production path).
    assert f"broadcast_txid: {BROADCAST_TXID}" in fake.prompts[5]


def test_send_lifecycle_sign_and_broadcast_gate_refusals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Flow discipline at handler level: sign_tx from CREATED refused;
    sign_tx with a mismatched ref refused; broadcast_tx from CONFIRMED
    refused — every refusal value-free and state-preserving."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    table, _store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
        signer_selection=app_module.SignerSelection(
            kind="file", dir_path=transfer, fingerprint_hex="00" * 4
        ),
    )
    created = table[IntentName.CREATE_TX](validate_payload(_create_tx_envelope_json()))
    tx_ref = created["tx_ref"]

    # sign_tx from CREATED → refused, state unchanged.
    sign_env = validate_payload(
        json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}})
    )
    refused = table[IntentName.SIGN_TX](sign_env)
    assert refused == {
        "error": "sign_refused",
        "detail": "no confirmed transaction to sign",
    }
    assert flow.state is TxFlowStatus.CREATED

    # broadcast_tx from CREATED → refused.
    broadcast_env = validate_payload(
        json.dumps({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": tx_ref}})
    )
    assert table[IntentName.BROADCAST_TX](broadcast_env) == {
        "error": "broadcast_refused",
        "detail": "no signed transaction to broadcast",
    }

    # Confirm, then sign_tx quoting the WRONG ref → refused, CONFIRMED.
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    assert flow.state is TxFlowStatus.CONFIRMED
    wrong_ref = validate_payload(
        json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": "bogus-ref"}})
    )
    assert table[IntentName.SIGN_TX](wrong_ref) == {
        "error": "sign_refused",
        "detail": "tx_ref does not match the confirmed transaction",
    }
    # broadcast_tx from CONFIRMED → refused (no skip path past signing).
    assert table[IntentName.BROADCAST_TX](broadcast_env) == {
        "error": "broadcast_refused",
        "detail": "no signed transaction to broadcast",
    }
    assert flow.state is TxFlowStatus.CONFIRMED
    client.close()
    _store.close()


def test_send_lifecycle_sign_tx_result_contract_and_tamper_value_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Table-level sign_tx: the success result carries exactly the
    contract keys (status/tx_ref/txid from RevalidatedTx/signer_name/
    checksum_verified), and a tampered import yields a value-free
    revalidation_failed detail (no PSBT text, no addresses, no amounts)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    table, _store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
        signer_selection=app_module.SignerSelection(
            kind="file", dir_path=transfer, fingerprint_hex="00" * 4
        ),
    )
    created = table[IntentName.CREATE_TX](validate_payload(_create_tx_envelope_json()))
    tx_ref = created["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    sign_env = validate_payload(
        json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}})
    )

    # First call: export + signed_file_missing.
    missing = table[IntentName.SIGN_TX](sign_env)
    assert missing["error"] == "signed_file_missing"
    assert missing["signed_filename"].startswith("localwallet-signed-")
    assert missing["signed_filename"].endswith(".psbt.b64")

    # Device places a TAMPERED signed file → value-free hard stop.
    unsigned_path = transfer / f"localwallet-unsigned-{tx_ref[:8]}.psbt.b64"
    ref8 = tx_ref[:8]
    tampered = _simulate_device_sign(
        unsigned_path.read_text(encoding="utf-8").strip(), tamper=True
    )
    (transfer / f"localwallet-signed-{ref8}.psbt.b64").write_text(
        tampered + "\n", encoding="utf-8"
    )
    failed = table[IntentName.SIGN_TX](sign_env)
    assert failed["error"] == "revalidation_failed"
    detail = str(failed["detail"])
    assert detail.strip() != ""
    for value in (SEND_RECIPIENT, str(SEND_AMOUNT_SATS), tampered[:20]):
        assert value not in detail
    assert flow.state is TxFlowStatus.CONFIRMED

    # Replace with an honest signed file → success contract.
    honest = _simulate_device_sign(unsigned_path.read_text(encoding="utf-8").strip())
    (transfer / f"localwallet-signed-{ref8}.psbt.b64").write_text(
        honest + "\n", encoding="utf-8"
    )
    (transfer / f"localwallet-signed-{ref8}.psbt.b64.sha256").write_text(
        hashlib.sha256((transfer / f"localwallet-signed-{ref8}.psbt.b64").read_bytes()).hexdigest()
        + "\n",
        encoding="utf-8",
    )
    signed = table[IntentName.SIGN_TX](sign_env)
    assert set(signed.keys()) == {
        "status",
        "tx_ref",
        "txid",
        "signer_name",
        "checksum_verified",
    }
    assert signed["status"] == "signed"
    assert signed["tx_ref"] == tx_ref
    assert signed["signer_name"] == "file"
    assert signed["checksum_verified"] is True
    expected_txid = _extract_signed_tx(honest).txid().hex()
    assert signed["txid"] == expected_txid
    assert len(signed["txid"]) == 64
    assert flow.state is TxFlowStatus.SIGNED
    client.close()
    _store.close()


def test_send_lifecycle_tx_status_unknown_vs_chain_error(tmp_path: Path) -> None:
    """unknown_tx is reserved for the flow's own broadcast txid (404 =
    eventual consistency); the same 404 for any other txid is the generic
    chain_unavailable path."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict[str, Any] = {"status_404": True}
    table, _store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state),
        signer_selection=app_module.SignerSelection(
            kind="file", dir_path=transfer, fingerprint_hex="00" * 4
        ),
    )
    created = table[IntentName.CREATE_TX](validate_payload(_create_tx_envelope_json()))
    tx_ref = created["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    sign_env = validate_payload(
        json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}})
    )
    table[IntentName.SIGN_TX](sign_env)  # export + signed_file_missing
    unsigned_path = transfer / f"localwallet-unsigned-{tx_ref[:8]}.psbt.b64"
    honest = _simulate_device_sign(unsigned_path.read_text(encoding="utf-8").strip())
    (transfer / f"localwallet-signed-{tx_ref[:8]}.psbt.b64").write_text(
        honest + "\n", encoding="utf-8"
    )
    table[IntentName.SIGN_TX](sign_env)  # import → revalidate → SIGNED
    table[IntentName.BROADCAST_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    assert flow.state is TxFlowStatus.BROADCAST and flow.txid is not None

    status_env = validate_payload(
        json.dumps({"v": 0, "intent": "tx_status", "params": {"txid": flow.txid}})
    )
    assert table[IntentName.TX_STATUS](status_env) == {
        "error": "unknown_tx",
        "detail": (
            "the broadcast transaction is not indexed yet — eventual "
            "consistency; try again shortly"
        ),
    }
    other_env = validate_payload(
        json.dumps({"v": 0, "intent": "tx_status", "params": {"txid": "ab" * 32}})
    )
    other = table[IntentName.TX_STATUS](other_env)
    assert other["error"] == "chain_unavailable"
    assert "status 404" in str(other["detail"])
    assert flow.txid not in str(other["detail"])  # scrubbed chain detail
    client.close()
    _store.close()


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("LOCALWALLET_E2E_LIVE") != "1",
    reason="live-network test: set LOCALWALLET_E2E_LIVE=1 to include",
)
def test_live_testnet4_balance_via_mempool_space() -> None:
    """Real-network integration: the Phase 1 path against mempool.space
    testnet4 — lazy scan populates the store, balance reads it, and a
    new_address allocation derives a valid tb1 address at index 0."""
    vpub = os.environ.get("LOCALWALLET_E2E_VPUB", "").strip()
    if not vpub:
        pytest.skip("LOCALWALLET_E2E_VPUB not set")
    parsed = parse_wallet_key(vpub)
    descriptor = WalletDescriptor.from_key(vpub)
    client = EsploraClient()  # defaults: https://mempool.space/testnet4/api
    try:
        with Store.memory() as store:
            wallet = store.create_wallet("default", descriptor.descriptor)
            store.set_active_wallet(wallet.id)
            store.set_setting(GAP_LIMIT_SETTING, "3")  # keep the live scan light
            table = build_dispatch_table(
                store,
                wallet,
                parsed,
                client,
                lambda: scan_wallet(store, client, wallet),
            )
            loop = AgentLoop(stub_generate, table)
            balance_turn = loop.run("What's my balance?", {})
            address_turn = loop.run("give me a new address", {})
    finally:
        client.close()

    assert balance_turn.status is AgentTurnStatus.OK
    assert balance_turn.result is not None
    result = balance_turn.result
    if result.get("error") == "chain_unavailable":
        pytest.fail(f"live chain query failed: {result.get('detail')}")
    assert isinstance(result["confirmed_sats"], int) and result["confirmed_sats"] >= 0
    assert isinstance(result["unconfirmed_sats"], int) and result["unconfirmed_sats"] >= 0
    assert result["total_sats"] == result["confirmed_sats"] + result["unconfirmed_sats"]
    assert isinstance(result["addresses_scanned"], int) and result["addresses_scanned"] >= 0
    assert isinstance(result["tip_height"], int) and result["tip_height"] > 0

    assert address_turn.status is AgentTurnStatus.OK
    assert address_turn.result is not None
    assert address_turn.result["index"] == 0
    address = address_turn.result["address"]
    assert isinstance(address, str) and address.startswith("tb1")
