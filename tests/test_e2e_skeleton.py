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
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
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
    BACKEND_MODE_OWN_NODE_LOCAL,
    BACKEND_MODE_OWN_NODE_REMOTE,
    BACKEND_MODE_PUBLIC,
    GAP_LIMIT_ENV_VAR,
    NODE_STATUS_DETECTION_DISABLED,
    OUT_OF_WINDOW_NOTICE,
    PRIVACY_INDICATOR,
    PRIVACY_INDICATOR_OWN_NODE_LOCAL,
    PRIVACY_INDICATOR_OWN_NODE_REMOTE,
    PRIVACY_INDICATOR_OWN_NODE_REMOTE_GENERIC,
    ZPUB_ENV_VAR,
    SendSession,
    _env_gap_limit,
    build_dispatch_table,
    privacy_indicator,
    run,
    stub_generate,
)
from localwallet.chain import EsploraClient, PriceOracle
from localwallet.config import Settings
from localwallet.node import LocalNodeReport, NodeStatus
from localwallet.node.detect import CoreHealth, CoreRpcProbe
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

#: Canonical mainnet fixture zpub — the SAME constant as
#: ``tests/test_wallet_descriptor.py::ZPUB`` and
#: ``tests/test_chain_backend_switch.py::ZPUB`` (one fixed seed across the
#: suite, ADR-0021 mainnet-only). Derived from DESCRIPTOR_SEED below.
ZPUB: Final[str] = (
    "zpub6qh6bF4roUgQtg2fm5SUhRsQFEidwUPPLhS82BDHjtNh2UxmgNfCS8NF4jQoBqNCeEW"
    "BaKyTxcmyBkq3iuZS5Seyz5dWMcwYxaMgpZn4cWQ"
)
UPUB: Final[str] = (
    "upub5EZAYmn7rXyfeYphHTqJ3m8umysXpxE1Fz8rYCcMBCjJ2KdSuCNf24pxTYGDyDyz"
    "aVwW7KKyF7HezfVT9APYvons4NPiRm4oht646o9zVi9"
)
TPUB: Final[str] = (
    "tpubDCPxzVARcvNjZZjy5nZi1GS2NJsRG3TkDZeuncBCT2eWFMbMhkcf5WLeMiTsY6Ae"
    "N7CfrtRKkAJFTC8VqRgPwza2kDfgBEqJ3hkN8GcfXn9"
)
# Testnet keys are the REFUSAL fixtures now (ADR-0021): the wallet layer
# refuses them outright, so VPUB only ever appears in negative tests.
VPUB: Final[str] = (
    "vpub5ZJ3cDEGGk61yWWUHFHgmG3M4je4yFD3ebC6jWHsqV8Cxh2K5zz8c6X5Hk7FkUAB"
    "FTjRkQBz3g84MYeRhjAdnq1QmrmyTRTrzs8rFVCJUyh"
)
# BIP32 test vector 1 root key — a *private* extended key (watch-only refusal).
XPRV: Final[str] = (
    "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKm"
    "PGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
)

#: The seed behind the canonical ZPUB (shared with
#: ``tests/test_wallet_descriptor.py`` — do not change one without the other).
DESCRIPTOR_SEED: Final = b"local-wallet phase 1 descriptor test seed (not a real wallet)"

# Corrupted-checksum variant of the fixture zpub (last char replaced).
CORRUPT_ZPUB: Final[str] = ZPUB[:-1] + ("1" if ZPUB[-1] != "1" else "2")

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

_EXTERNAL: Final[str] = "bc1qexternalsenderaddressnotpartofthewallet000000"


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

    descriptor = Descriptor.from_string(f"wpkh({ZPUB}/{branch}/*)")
    return [
        descriptor.derive(i, branch_index=0).address(network=NETWORKS["main"])
        for i in range(count)
    ]


def _fixture_parsed() -> ParsedKey:
    """The fixture wallet's parsed key through the wallet-engine gate."""
    return WalletDescriptor.from_key(ZPUB).parsed


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


def _funding_txs(address: str, utxos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Esplora-truthful txs mirror for a scripted utxo payload (TCK-SCAN-001).

    An address holding UTXOs necessarily has their funding transactions in
    its history; the scan no longer fetches ``/utxo`` for empty-history
    addresses, so fixtures that script only utxos must serve the matching
    funding txs too. One entry per utxo, mirroring its confirmed/height
    status; fee absent (tolerated as ``None``, never fabricated).
    """
    entries: list[dict[str, Any]] = []
    for utxo in utxos:
        status = utxo.get("status", {})
        entries.append(
            _tx_entry(
                utxo["txid"],
                vout_addresses=(address,),
                fee=None,
                confirmed=bool(status.get("confirmed")),
                height=status.get("block_height"),
                block_time=None,
            )
        )
    return entries


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
    """MockTransport handler serving per-address txs/utxo payloads + tip.

    Addresses with scripted utxos but no scripted txs get the matching
    funding-tx mirror (TCK-SCAN-001: the scan skips ``/utxo`` for
    empty-history addresses, so utxo-only fixtures must be chain-truthful
    about their history too).
    """

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
            if address in txs_by_addr:
                return httpx.Response(200, json=txs_by_addr[address])
            return httpx.Response(
                200, json=_funding_txs(address, utxos_by_addr.get(address, []))
            )
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
        base_url="https://mempool.space/api",
        timeout_s=5.0,
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
    )


def _scan_requests(recorded: list[httpx.Request]) -> list[httpx.Request]:
    """Recorded requests minus the display-only ``/v1/prices`` best-effort
    fetches (TCK-FIAT-001): every ``get_balance`` answer attempts one; the
    ``_scan_handler`` mocks 404 it, so the USD keys stay absent and the
    scan-count assertions below stay about SCAN traffic only."""
    return [r for r in recorded if not r.url.path.endswith("/v1/prices")]


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
    wd = WalletDescriptor.from_key(ZPUB)
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
    fixed seeds — proving they are real, checksum-valid SLIP-132 keys.

    The canonical ZPUB comes from the shared descriptor-test seed (one fixed
    seed across the suite); the testnet UPUB/TPUB refusal fixtures come from
    the phase-0 e2e seed.
    """
    from embit.bip32 import NETWORKS, HDKey

    def _rederive_from(seed: bytes, purpose: int, coin: int, prv_version: bytes, pub_version: bytes) -> str:
        root = HDKey.from_seed(seed, version=prv_version)
        account = root.derive([purpose + 2**31, coin + 2**31, 0])
        return account.to_public().to_base58(version=pub_version)

    assert ZPUB == _rederive_from(
        DESCRIPTOR_SEED, 84, 0, NETWORKS["main"]["zprv"], NETWORKS["main"]["zpub"]
    )
    assert UPUB == _rederive_from(
        FIXTURE_SEED, 49, 1, NETWORKS["test"]["yprv"], NETWORKS["test"]["ypub"]
    )
    assert TPUB == _rederive_from(
        FIXTURE_SEED, 44, 1, NETWORKS["test"]["xprv"], NETWORKS["test"]["xpub"]
    )
    assert VPUB == _rederive_from(
        FIXTURE_SEED, 84, 1, NETWORKS["test"]["zprv"], NETWORKS["test"]["zpub"]
    )


def _mainnet_fixture_key(script: str) -> str:
    """Mainnet ypub/xpub fixtures derived from the phase-0 e2e seed."""
    from embit.bip32 import NETWORKS

    purpose = {"ypub": 49, "xpub": 44}[script]
    prv = NETWORKS["main"][f"{script[0]}prv"]
    pub = NETWORKS["main"][script]
    return _rederive_fixture_key(purpose, 0, prv, pub)


@pytest.mark.parametrize(
    ("key_id", "expected_network", "expected_script"),
    [
        ("zpub", "main", "p2wpkh"),
        ("vpub", "testnet", "p2wpkh"),
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
        "zpub": ZPUB,
        "vpub": VPUB,
        "upub": UPUB,
        "tpub": TPUB,
        "ypub(main)": _mainnet_fixture_key("ypub"),
        "xpub(main)": _mainnet_fixture_key("xpub"),
    }[key_id]

    # parse_watch_key keeps the Phase 0 detect-only contract (gate off);
    # parse_wallet_key / WalletDescriptor enforce the ADR-0021 mainnet gate.
    parsed = parse_watch_key(key)
    assert isinstance(parsed, ParsedKey)
    assert parsed.network == expected_network
    assert parsed.script_type == expected_script
    assert not parsed.hd_key.is_private


def test_zpub_derives_deterministic_bc1_addresses_matching_descriptor() -> None:
    parsed = parse_watch_key(ZPUB)
    assert parsed.network == "main"
    assert parsed.script_type == "p2wpkh"

    addresses = derive_receive_addresses(parsed, count=5)
    # The mainnet-prefix assertion: zpub path → bech32 bc1...
    assert all(addr.startswith("bc1") for addr in addresses)
    # Cross-checked against embit's descriptor engine (independent path).
    assert addresses == _expected_addresses(5)
    # Deterministic and prefix-stable across calls and counts.
    assert derive_receive_addresses(parsed, count=5) == addresses
    assert derive_receive_addresses(parsed, count=3) == addresses[:3]
    assert len(addresses) == 5


def test_change_branch_derivation_differs_from_receive() -> None:
    parsed = parse_watch_key(ZPUB)
    receive = derive_receive_addresses(parsed, count=2, branch=0)
    change = derive_receive_addresses(parsed, count=2, branch=1)
    assert receive != change
    assert change == _expected_addresses(2, branch=1)


@pytest.mark.parametrize("count", [0, -1, True, 1001, 2.5, "3", None])
def test_derive_rejects_out_of_range_count(count: object) -> None:
    parsed = parse_watch_key(ZPUB)
    with pytest.raises(WatchKeyError, match="count"):
        derive_receive_addresses(parsed, count=count)  # type: ignore[arg-type]


@pytest.mark.parametrize("branch", [2, -1, True, "0"])
def test_derive_rejects_invalid_branch(branch: object) -> None:
    parsed = parse_watch_key(ZPUB)
    with pytest.raises(WatchKeyError, match="branch"):
        derive_receive_addresses(parsed, count=1, branch=branch)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "\t\n", "not-a-key", "1" * 30, CORRUPT_ZPUB, XPRV, f"{ZPUB} {ZPUB}"],
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


def test_testnet_vpub_refused_by_mainnet_gate() -> None:
    """ADR-0021: the testnet key is the refusal case end to end."""
    with pytest.raises(WatchKeyError) as excinfo:
        parse_wallet_key(VPUB)
    message = str(excinfo.value)
    assert "mainnet-only" in message
    assert VPUB not in message  # key never echoed
    # The derive-side gate refuses too (defense in depth): a detect-only
    # testnet ParsedKey can never reach address encoding.
    parsed = parse_watch_key(VPUB)
    with pytest.raises(WatchKeyError, match="mainnet-only"):
        derive_receive_addresses(parsed)


def test_parse_wallet_key_enforces_mainnet_gate_at_parse() -> None:
    """ADR-0021 gate: parse_wallet_key refuses testnet keys outright."""
    for key in (VPUB, UPUB, TPUB):
        with pytest.raises(WatchKeyError) as excinfo:
            parse_wallet_key(key)
        message = str(excinfo.value)
        assert "mainnet-only" in message
        assert key not in message  # key never echoed
    # Mainnet keys pass.
    assert parse_wallet_key(ZPUB).network == "main"


def test_mainnet_keys_pass_the_gate() -> None:
    for key in (ZPUB, _mainnet_fixture_key("ypub"), _mainnet_fixture_key("xpub")):
        addresses = derive_receive_addresses(parse_watch_key(key), count=2)
        assert len(addresses) == 2
    # The canonical zpub derives bech32 bc1... receive addresses.
    assert all(
        addr.startswith("bc1")
        for addr in derive_receive_addresses(parse_watch_key(ZPUB), count=2)
    )


def test_wallet_descriptor_is_canonical_and_checksummed() -> None:
    wd = WalletDescriptor.from_key(ZPUB)
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
    """'send 60000 sats to <bc1…>' → create_tx with the bc1 token and the
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
    """No parsable amount/usable bc1 token → canned fixture recipient
    (the P0 fixture address) and the canned 10000-sat amount."""
    prompt = "SYSTEM...\n\nuser: send to bc1\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.CREATE_TX
    assert envelope.params.recipient == app_module._STUB_RECIPIENT
    assert envelope.params.amount_sats == 10_000
    # The canned recipient IS the P0 fixture address (valid mainnet P2WPKH).
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
        ["--stub-llm", "--zpub", ZPUB],
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
        # TCK-SCAN-003 (ADR-0022): the tool-owned freshness flag rides every
        # cache answer; the lazy scan completed, so this one is fresh.
        "freshness": "fresh",
    }
    # The lazy scan really hit the chain adapter: one txs call per window
    # address, utxo calls only for addresses with txs (TCK-SCAN-001:
    # addr0/addr1 got funding-tx mirrors; the empty history of
    # change0/change1 skips their /utxo fetch) plus one tip request.
    utxo_paths = {r.url.path for r in recorded if r.url.path.endswith("/utxo")}
    assert utxo_paths == {
        f"/api/address/{a}/utxo"
        for a in (addr0, addr1)
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
    # No new SCAN traffic on the cached read (TCK-FIAT-001: the best-effort
    # price attempt is display sugar, not scan I/O).
    assert _scan_requests(recorded[after_first:]) == []
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
        # Fund addr0 so the scan actually reaches (and trips on) /utxo
        # — empty-history addresses are never fetched (TCK-SCAN-001).
        lambda rec: _scan_handler(
            rec, utxos_by_addr={addr0: [SEND_UTXO]}, utxo_status=500
        )
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
    addr0 = derive_fixture_addresses(1)[0]
    table, store, _wallet, client, _recorded = _build_table(
        # Funded addr0 keeps the /utxo endpoint on the scan path
        # (TCK-SCAN-001: empty-history addresses are never fetched).
        lambda rec: _scan_handler(
            rec, utxos_by_addr={addr0: [SEND_UTXO]}, utxo_status=503
        )
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
    wd = WalletDescriptor.from_key(ZPUB)
    with Store(path) as store:
        wallet = store.create_wallet("default", wd.descriptor)
        store.set_active_wallet(wallet.id)
        store.set_setting(GAP_LIMIT_SETTING, str(gap_limit))
    return wd


def _wait_scan_narration(outputs: list[str], timeout: float = 10.0) -> None:
    """Feeder-side sync for the NON-BLOCKING startup scan (TCK-SCAN-003,
    ADR-0022): hold the next user line until the engine thread has narrated
    the scan's completion or failure, so a line that must read the
    post-scan cache is deterministic (the prompt was live long before)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(
            line.startswith(
                ("Startup scan complete", "Rescan complete", "warning: startup scan failed",
                 "warning: rescan failed")
            )
            for line in outputs
        ):
            return
        time.sleep(0.005)
    raise AssertionError("startup scan never narrated its outcome")


def _run_captured(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    lines: list[str],
    *,
    store_path: Path | None = None,
    auto_scan: bool = False,
    sync_first_line: bool = False,
) -> tuple[int, list[str]]:
    """Run app.run() with stub I/O, a tmp store, and a mock chain client.

    ``sync_first_line`` gates the FIRST user line on the startup scan's
    narration (see :func:`_wait_scan_narration`) — used by tests asserting
    post-scan reads; tests of the stale/mid-scan behavior leave it off and
    race nothing (their assertions hold either way)."""
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
    outputs: list[str] = []
    state = {"first": True}

    def read_line(_prompt: str) -> str:
        line = next(inputs)
        if sync_first_line and state["first"]:
            state["first"] = False
            _wait_scan_narration(outputs)
        return line

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
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
        auto_scan=True,
        sync_first_line=True,  # TCK-SCAN-003: read AFTER the async scan lands
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Banner: §9 privacy indicator verbatim.
    assert PRIVACY_INDICATOR in joined
    # Startup scan feedback: counts + tip only (no addresses/amounts).
    assert "Startup scan complete: 3 UTXOs · tip height 870000." in joined
    # Balance line verbatim from the handler result dict.
    assert (
        f"Balance (mainnet): {EXPECTED_CONFIRMED} sats (confirmed) "
        f"+ {EXPECTED_UNCONFIRMED} sats (unconfirmed)" in joined
    )
    assert f"Total {EXPECTED_TOTAL} sats" in joined
    assert f"tip height {TIP_HEIGHT}" in joined
    # Privacy/secret hygiene: the zpub and addresses are never echoed.
    assert ZPUB not in joined
    assert all(addr not in joined for addr in (addr0, addr1))
    assert wd.descriptor not in joined
    # 9 requests: 1 tip + 6 txs (gap-2 window extends past the two funded
    # branch-0 addresses) + 2 utxo (only the addresses with txs,
    # TCK-SCAN-001). The balance turn's best-effort /v1/prices fetch
    # (TCK-FIAT-001, 404 here → sats-only answer) is excluded by design.
    assert len(_scan_requests(recorded)) == 9
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
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert PRIVACY_INDICATOR in joined
    assert (
        f"Balance (mainnet): {EXPECTED_CONFIRMED} sats (confirmed) "
        f"+ {EXPECTED_UNCONFIRMED} sats (unconfirmed)" in joined
    )
    assert f"Total {EXPECTED_TOTAL} sats" in joined
    assert f"tip height {TIP_HEIGHT}" in joined
    assert ZPUB not in joined
    assert all(addr not in joined for addr in (addr0, addr1))
    # Lazy scan only (auto-scan off): 1 tip + 6 txs + 2 utxo (funded
    # addresses only — TCK-SCAN-001 skip for the empty-history rest). The
    # balance turn's display-only /v1/prices fetch is excluded (TCK-FIAT-001).
    assert len(_scan_requests(recorded)) == 9


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
    code = run(["--stub-llm", "--zpub", ZPUB], input_fn=read_line, output_fn=outputs.append)

    assert code == 0
    assert marks["What's my balance?"] == 0  # startup made zero chain calls
    assert marks["exit"] > 0  # the balance turn performed the lazy scan
    joined = "\n".join(outputs)
    assert "Startup scan complete" not in joined  # startup scan skipped
    assert (
        f"Balance (mainnet): {EXPECTED_CONFIRMED} sats (confirmed)" in joined
    )
    # Fresh-store path: the app created exactly one wallet row itself.
    with Store(store_path) as store:
        assert len(store.list_wallets()) == 1


def test_repl_reads_zpub_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ZPUB_ENV_VAR, ZPUB)
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
    assert f"Balance (mainnet): {EXPECTED_CONFIRMED} sats" in joined
    assert ZPUB not in joined  # env-sourced key never echoed either


def test_zpub_cli_arg_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Env holds a key the ADR-0021 gate would refuse; the CLI arg must win.
    monkeypatch.setenv(ZPUB_ENV_VAR, VPUB)
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["exit"],
        store_path=store_path,
    )

    assert code == 0
    assert PRIVACY_INDICATOR in "\n".join(outputs)


# ------------------------------------------------ node_status (TCK-P4-003)

def _core_ready_report() -> LocalNodeReport:
    """A LocalNodeReport with a reachable+synced Core and a mempool indexer."""
    health = CoreHealth(
        chain="main",
        blocks=100,
        headers=100,
        verification_progress=1.0,
        initial_block_download=False,
    )
    core = (
        CoreRpcProbe(
            port=8332, status=NodeStatus.REACHABLE, cookie_present=True, health=health
        ),
    )
    return LocalNodeReport(
        core=core, mempool=NodeStatus.REACHABLE, electrs=NodeStatus.OFFLINE
    )


def _run_node_repl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    detect_report: LocalNodeReport | None,
    chain_base_url: str | None = None,
    node_detection_enabled: bool = True,
) -> tuple[int, list[str], list[LocalNodeReport]]:
    """Run the REPL with an injected (mock-transport) node detection.

    ``detect_report`` None + ``node_detection_enabled=False`` exercises the
    clean detection-disabled state (no probing). Returns the captured
    ``(code, outputs, detect_calls)``.
    """
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "0")
    if chain_base_url is None:
        monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", chain_base_url)
    if node_detection_enabled:
        monkeypatch.setenv("LOCALWALLET_NODE_DETECTION_ENABLED", "1")
    else:
        monkeypatch.setenv("LOCALWALLET_NODE_DETECTION_ENABLED", "0")
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(handler))

    detect_calls: list[LocalNodeReport] = []

    def fake_detect() -> LocalNodeReport:
        assert detect_report is not None
        detect_calls.append(detect_report)
        return detect_report

    outputs: list[str] = []
    lines = iter(["what's my node status?", "exit"])
    code = run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: next(lines),
        output_fn=outputs.append,
        node_detect_fn=fake_detect,
    )
    return code, outputs, detect_calls


def test_node_status_repl_detects_and_narrates_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'what's my node status?' → detection runs → narration quotes FACTS.

    The injected detection is the P4-001 report (a mocked local-node pass);
    the narration must come verbatim from the dispatcher-owned FACTS (backend
    mode, core sync state, indexer reachability, doctor guidance). Public
    default backend keeps the honest public-API banner and narration.
    """
    code, outputs, detect_calls = _run_node_repl(
        monkeypatch, tmp_path, detect_report=_core_ready_report()
    )

    assert code == 0
    assert len(detect_calls) == 1, "detection must have actually run"
    joined = "\n".join(outputs)
    # Public default: banner + node_status narration agree on public API.
    assert PRIVACY_INDICATOR in joined
    assert "You are querying the public API" in joined
    # FACTS quoted verbatim: core + indexer + doctor guidance.
    assert "A Bitcoin Core node is reachable and synced." in joined
    assert "Indexer reachable: mempool." in joined
    assert "Doctor: A Bitcoin Core node is ready" in joined
    assert "Guidance: Add a self-hosted mempool/electrs" in joined


def test_node_status_own_node_narration_and_banner_flip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOCALWALLET_CHAIN_BASE_URL set to a LOOPBACK host ⇒ own-node-LOCAL
    banner, narration, and watch line (TCK-SEC-004 change 5).

    The indicator flip derives from the same chain-backend selection the
    client uses (ADR-0018), so banner, node_status narration, and the
    background-watch line must all reflect own-node-local mode and the
    public wording must be absent.
    """
    code, outputs, detect_calls = _run_node_repl(
        monkeypatch,
        tmp_path,
        detect_report=_core_ready_report(),
        chain_base_url="http://127.0.0.1:3006",
    )

    assert code == 0
    assert len(detect_calls) == 1
    joined = "\n".join(outputs)
    # Banner: approved LOCAL wording, verbatim (TCK-UX-009: LOCAL is NOT
    # host-named — only the REMOTE wording grew the <host> insertion).
    assert PRIVACY_INDICATOR_OWN_NODE_LOCAL in joined
    assert PRIVACY_INDICATOR not in joined  # public wording flipped away
    assert "for transaction information" not in joined  # no REMOTE wording
    assert PRIVACY_INDICATOR_OWN_NODE_REMOTE_GENERIC not in joined
    # node_status narration: approved LOCAL predicate, verbatim.
    assert (
        "You are querying your own node on this machine — addresses and "
        "lookups stay here." in joined
    )
    assert "You are querying the public API" not in joined
    # Watch line: TCK-UX-012(b) copy — "on" is the NORMAL state, so an
    # on-launch prints NOTHING (UX-009's on-line retired; the stored rung
    # still reaches the watcher, pinned below).
    assert "Background watch" not in joined


def test_node_status_remote_own_node_narration_and_banner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOCALWALLET_CHAIN_BASE_URL pointing at a NON-loopback host (LAN/VPS)
    ⇒ own-node-REMOTE wording everywhere (TCK-SEC-004 change 5, R7
    no-over-claim), and since TCK-UX-009 the REMOTE wording NAMES THE HOST
    (scheme/port/credentials stripped) in banner and narration alike:
    "Querying <host> for transaction information. This is only private if
    you trust this machine." Nothing may claim lookups "stay on this
    machine" or the public API."""
    code, outputs, _detect_calls = _run_node_repl(
        monkeypatch,
        tmp_path,
        detect_report=_core_ready_report(),
        chain_base_url="http://192.168.1.50:3006/api",
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Banner: approved REMOTE wording with the host inserted; the path and
    # the port never ride the display (the credential-strip pin lives in
    # the pure-function tests — client build refuses userinfo on http(s)).
    assert PRIVACY_INDICATOR_OWN_NODE_REMOTE.format("192.168.1.50") in joined
    assert "192.168.1.50:3006" not in joined and "/api" not in joined
    assert PRIVACY_INDICATOR not in joined
    assert PRIVACY_INDICATOR_OWN_NODE_LOCAL not in joined
    # node_status narration: the SAME host insertion (lockstep).
    assert (
        "You are querying 192.168.1.50 for transaction information. This "
        "is only private if you trust this machine." in joined
    )
    assert "You are querying the public API" not in joined
    assert "lookups stay on this machine" not in joined
    # Watch line: TCK-UX-012(b) — watch on prints nothing.
    assert "Background watch" not in joined


def test_watch_line_off_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TCK-UX-012(b) copy: interval 0 (env rung) ⇒ exactly
    ``Background watch: off. Change it in settings.`` — the off path keeps
    the settings pointer (UX-009's claim, now only where it's actionable);
    the on path prints nothing at all (pinned by the on-state tests)."""
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    code, outputs, _ = _run_node_repl(
        monkeypatch, tmp_path, detect_report=_core_ready_report()
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert "Background watch: off. Change it in settings." in joined
    assert "Background watch: on" not in joined


def test_startup_banner_lines_are_separate_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-UX-012(a) emission pin: every startup banner line arrives as its
    OWN output_fn call — the privacy notice, the watch line and the prompt
    hint are each a WHOLE event (a merged emission would fail membership,
    not just the web-side bubble split the engine's turn delimiters drive).
    Watch off here so the banner grows the off line (the on path adds
    nothing — pinned by the copy tests)."""
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    code, outputs, _ = _run_node_repl(
        monkeypatch, tmp_path, detect_report=_core_ready_report()
    )
    assert code == 0
    assert f"Privacy notice: {PRIVACY_INDICATOR}" in outputs
    assert "Background watch: off. Change it in settings." in outputs
    assert "Type a message — 'exit' or Ctrl-D quits." in outputs
    # Each appears exactly once as its own event (no duplicates/merges).
    assert outputs.count("Background watch: off. Change it in settings.") == 1


def _watch_spy(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Wrap the app's IncomingWatcher construction to record the interval
    the watcher build site actually resolved (the ladder's live reader)."""
    real = app_module.IncomingWatcher

    def spy(probe: Any, *, interval_s: float) -> Any:
        captured["interval_s"] = interval_s
        return real(probe, interval_s=interval_s)

    monkeypatch.setattr(app_module, "IncomingWatcher", spy)


def test_watch_stored_setting_reaches_the_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-UX-009 reader proof (settings-allowlist invariant): a stored
    ``watch_interval_s`` row is RESOLVED AT THE WATCHER BUILD SITE and
    reaches the watcher — the "Change it in settings" claim is TRUE."""
    monkeypatch.delenv("LOCALWALLET_WATCH_INTERVAL_S", raising=False)
    store_path = _store_path(tmp_path)  # _run_node_repl presets the wallet row
    with Store(store_path) as store:
        store.set_setting("watch_interval_s", "45")
    captured: dict[str, Any] = {}
    _watch_spy(monkeypatch, captured)

    code, outputs, _ = _run_node_repl(
        monkeypatch, tmp_path, detect_report=_core_ready_report()
    )
    assert code == 0
    assert captured["interval_s"] == 45.0
    # TCK-UX-012(b): on is silent (the reader proof is ``captured`` above).
    assert "Background watch" not in "\n".join(outputs)


def test_watch_stored_setting_malformed_warns_once_and_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-UX-009 fail-soft read: a corrupt stored watch interval never
    stalls the launch — ONE value-free warning, the default 60 still
    builds the watcher."""
    monkeypatch.delenv("LOCALWALLET_WATCH_INTERVAL_S", raising=False)
    store_path = _store_path(tmp_path)  # _run_node_repl presets the wallet row
    with Store(store_path) as store:
        store.set_setting("watch_interval_s", "banana")
    captured: dict[str, Any] = {}
    _watch_spy(monkeypatch, captured)

    code, outputs, _ = _run_node_repl(
        monkeypatch, tmp_path, detect_report=_core_ready_report()
    )
    assert code == 0
    assert captured["interval_s"] == 60.0
    joined = "\n".join(outputs)
    assert joined.count(app_module.WATCH_INTERVAL_STALE_WARNING) == 1
    assert "banana" not in joined  # value-free, as ever
    # TCK-UX-012(b): the default 60 still builds the watcher (captured
    # above) and on-state prints no watch line at all.
    assert "Background watch" not in joined


def test_backend_mode_three_state_classification() -> None:
    """The 3-way classification (TCK-SEC-004 change 5): no configured URL ⇒
    public; loopback host ⇒ own_node_local; anything else ⇒
    own_node_remote."""
    from localwallet.app import _backend_mode
    from localwallet.config import Settings

    assert _backend_mode(Settings()) == BACKEND_MODE_PUBLIC
    assert (
        _backend_mode(Settings(chain_base_url="http://127.0.0.1:3006"))
        == BACKEND_MODE_OWN_NODE_LOCAL
    )
    assert (
        _backend_mode(Settings(chain_base_url="http://localhost:3006"))
        == BACKEND_MODE_OWN_NODE_LOCAL
    )
    assert (
        _backend_mode(Settings(chain_base_url="http://[::1]:3006"))
        == BACKEND_MODE_OWN_NODE_LOCAL
    )
    assert (
        _backend_mode(Settings(chain_base_url="  http://127.0.0.1:3006  "))
        == BACKEND_MODE_OWN_NODE_LOCAL
    )
    assert (
        _backend_mode(Settings(chain_base_url="http://192.168.1.50:3006"))
        == BACKEND_MODE_OWN_NODE_REMOTE
    )
    assert (
        _backend_mode(
            Settings(chain_base_url="https://mempool.example.lan:3006/api")
        )
        == BACKEND_MODE_OWN_NODE_REMOTE
    )


def test_configured_url_host_extraction() -> None:
    """TCK-UX-009 unit pins for the host parser the REMOTE banner mirrors:
    scheme, port, path/query/fragment and USERINFO are stripped; IPv6
    literals de-bracket; unparseable hosts return ``None`` (the caller's
    fallback trigger). Never raises."""
    from localwallet.app import _configured_url_host

    assert _configured_url_host("https://node.lan:3006/api") == "node.lan"
    assert _configured_url_host("http://127.0.0.1:3006") == "127.0.0.1"
    assert _configured_url_host("ssl://electrum.lan:50001") == "electrum.lan"
    # Credentials stripped — the userinfo never survives the split.
    assert (
        _configured_url_host("https://user:hunter2@node.lan:3006/x?a=b#f")
        == "node.lan"
    )
    assert _configured_url_host("http://[fd00::5]:443/api") == "fd00::5"
    assert _configured_url_host("http://[::1]") == "::1"
    assert _configured_url_host("barehost.example") == "barehost.example"
    assert _configured_url_host("barehost.example:8080") == "barehost.example"
    # Malformed / host-less shapes → None (never a guess, never a raise).
    assert _configured_url_host("") is None
    assert _configured_url_host("http://") is None
    assert _configured_url_host(":://::") is None
    assert _configured_url_host("https://:3006/api") is None


def test_privacy_indicator_function_selects_wording_from_settings() -> None:
    """The banner helper is a pure function of the backend selection; the
    REMOTE branch names the host, and a URL with no extractable host falls
    back to the generic wording (TCK-UX-009) rather than printing a broken
    line."""
    from localwallet.config import Settings

    assert privacy_indicator(Settings()) == PRIVACY_INDICATOR
    own = Settings(chain_base_url="http://127.0.0.1:3006")
    assert privacy_indicator(own) == PRIVACY_INDICATOR_OWN_NODE_LOCAL
    remote = Settings(chain_base_url="http://192.168.1.50:3006")
    assert privacy_indicator(remote) == PRIVACY_INDICATOR_OWN_NODE_REMOTE.format(
        "192.168.1.50"
    )
    # Credentials are stripped from the display (the userinfo-carrying
    # shape the chain layer really allows: bitcoind:// RPC URLs).
    authed = Settings(chain_base_url="bitcoind://rpcuser:s3cr3t@10.0.0.7:8332/w")
    assert privacy_indicator(authed) == PRIVACY_INDICATOR_OWN_NODE_REMOTE.format(
        "10.0.0.7"
    )
    assert "s3cr3t" not in privacy_indicator(authed)
    assert "rpcuser" not in privacy_indicator(authed)
    # Malformed URL: REMOTE mode (a configured non-loopback string) with
    # no parseable host → the generic fallback, never a half-rendered line.
    malformed = Settings(chain_base_url=":://::")
    assert privacy_indicator(malformed) == PRIVACY_INDICATOR_OWN_NODE_REMOTE_GENERIC
    assert "{}" not in privacy_indicator(malformed)


def test_node_status_detection_disabled_does_not_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOCALWALLET_NODE_DETECTION_ENABLED=0 ⇒ clean disabled state, no probe.

    The injected detection is never called and the narration says detection
    is disabled instead of fabricating findings.
    """
    code, outputs, detect_calls = _run_node_repl(
        monkeypatch,
        tmp_path,
        detect_report=None,
        node_detection_enabled=False,
    )

    assert code == 0
    assert detect_calls == []  # no probing when disabled
    joined = "\n".join(outputs)
    assert "Local node detection is disabled" in joined
    assert "A Bitcoin Core node is reachable" not in joined


def test_node_status_handler_facts_shape() -> None:
    """The handler returns dispatcher-owned FACTS with all narration keys."""
    from localwallet.app import build_dispatch_table

    recorded: list[httpx.Request] = []
    client = _mock_client(_scan_handler(recorded))
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    table = build_dispatch_table(
        store,
        wallet,
        wd.parsed,
        client,
        lambda: scan_wallet(store, client, wallet),
        node_detect_fn=lambda: _core_ready_report(),
    )
    envelope = validate_payload({"v": 0, "intent": "node_status", "params": {}})
    result = table[IntentName.NODE_STATUS](envelope)

    assert result["backend_mode"] == "public"
    assert result["detection_state"] == "ran"
    assert result["core_reachable"] is True
    assert result["core_synced"] is True
    assert result["core_auth_issue"] is False
    assert result["mempool_reachable"] is True
    assert result["electrs_reachable"] is False
    assert result["doctor_state"] == "core_ready"
    assert result["doctor_headline"] == "A Bitcoin Core node is ready"
    assert isinstance(result["doctor_next_step"], str) and result["doctor_next_step"]


def test_node_status_detection_disabled_facts_state() -> None:
    """Detection-disabled handler result carries the disabled marker only."""
    from localwallet.app import build_dispatch_table
    from localwallet.config import Settings

    client = _mock_client(_scan_handler([]))
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    table = build_dispatch_table(
        store,
        wallet,
        wd.parsed,
        client,
        lambda: scan_wallet(store, client, wallet),
        settings=Settings(node_detection_enabled=False),
    )
    envelope = validate_payload({"v": 0, "intent": "node_status", "params": {}})
    result = table[IntentName.NODE_STATUS](envelope)

    assert result["detection_state"] == NODE_STATUS_DETECTION_DISABLED
    assert result["backend_mode"] == "public"
    assert "core_reachable" not in result  # nothing fabricated


def test_repl_reports_chain_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    # addr0 funded: the /utxo failure stays on the scan path (addresses
    # with empty history are never fetched, TCK-SCAN-001).
    handler = _scan_handler(
        [], utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]},
        utxo_status=503,
    )

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
        auto_scan=True,
        sync_first_line=True,  # balance runs after the failed async scan is narrated
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Startup scan failed → scrubbed warning, but the REPL still started.
    assert "warning: startup scan failed" in joined
    assert "chain unavailable" in joined
    assert "Balance (mainnet):" not in joined


def test_rescan_flag_repairs_stale_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--rescan: re-derives from the key, rebuilds derivation state and
    replaces the stale UTXO snapshot with chain truth (Phase 1 AC)."""
    store_path = _store_path(tmp_path)
    wd = WalletDescriptor.from_key(ZPUB)
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
        ["--stub-llm", "--zpub", ZPUB, "--rescan"],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
        sync_first_line=True,  # TCK-SCAN-003: read AFTER the async rescan lands
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
    assert "Balance (mainnet): 50000 sats (confirmed) + 0 sats (unconfirmed)" in joined
    # The out-of-window warning stays cleared: usage stayed inside the window.
    assert "usage was found beyond your usual address window" not in joined


# ------------------------------------------------- TCK-SEC-002b narration


def _make_scan_summary(*, truncated: bool) -> app_module.wallet_scan.ScanSummary:
    """A ScanSummary shaped like the rescan fixtures above (branch 0 used
    through index 0, branch 1 unused), optionally flagged truncated."""
    BS = app_module.wallet_scan.BranchScanSummary
    return app_module.wallet_scan.ScanSummary(
        wallet_id=1,
        gap_limit=20,
        tip_height=870000,
        scanned_at="2026-08-31T00:00:00+00:00",
        branches={
            0: BS(
                branch=0,
                scanned=3,
                window_last_index=2,
                max_used_index=0,
                next_index=1,
                used_indices=(0,),
                truncated=truncated,
            ),
            1: BS(
                branch=1,
                scanned=2,
                window_last_index=1,
                max_used_index=-1,
                next_index=0,
                used_indices=(),
                truncated=False,
            ),
        },
        utxo_count=1,
        truncated=truncated,
    )


def test_rescan_summary_line_byte_identical_when_not_truncated() -> None:
    """TCK-SEC-002b: non-truncated narration is byte-identical to the
    pre-change text — no truncation notice is appended."""
    summary = _make_scan_summary(truncated=False)
    line = app_module._rescan_summary_line(summary)
    assert line == (
        "Rescan complete: branch 0: scanned 3, max used 0, next index 1 · "
        "branch 1: scanned 2, max used -1, next index 0 · 1 UTXOs · "
        "tip height 870000"
    )
    assert app_module._truncation_notice(summary) == ""


def test_rescan_summary_line_appends_truncation_notice_when_truncated() -> None:
    """TCK-SEC-002b: a truncated rescan appends the value-free window-cap
    notice; the counts-only body is unchanged."""
    summary = _make_scan_summary(truncated=True)
    line = app_module._rescan_summary_line(summary)
    assert line.startswith(
        "Rescan complete: branch 0: scanned 3, max used 0, next index 1 · "
        "branch 1: scanned 2, max used -1, next index 0 · 1 UTXOs · "
        "tip height 870000 "
    )
    assert app_module.TRUNCATION_NOTICE in line


def test_truncation_notice_is_value_free() -> None:
    """TCK-SEC-002b: the notice carries no addresses, amounts, or indices —
    no digits at all — and cites the cap only as the documented "window cap"
    constant reference, nudging a rescan/config review in the narration tone."""
    notice = app_module.TRUNCATION_NOTICE
    assert not any(ch.isdigit() for ch in notice)
    assert "window cap" in notice
    assert "rescan" in notice


def test_startup_scan_line_byte_identical_when_not_truncated() -> None:
    """TCK-SEC-002b: non-truncated startup-scan narration is byte-identical
    to the pre-change text (period, no notice appended).

    TCK-SCAN-003: the completion line is now produced by the engine-thread
    persist step (:func:`_scan_summary_line`, emitted by :class:`ScanFlow`
    when the startup scan lands), so the truncation contract is pinned on
    that pure builder directly."""
    assert (
        app_module._scan_summary_line(_make_scan_summary(truncated=False))
        == "Startup scan complete: 1 UTXOs · tip height 870000."
    )


def test_startup_scan_appends_truncation_notice_when_truncated() -> None:
    """TCK-SEC-002b: a truncated startup scan appends the notice to the
    normal startup narration (:func:`_scan_summary_line`)."""
    line = app_module._scan_summary_line(_make_scan_summary(truncated=True))
    assert line.startswith("Startup scan complete: 1 UTXOs · tip height 870000.")
    assert app_module.TRUNCATION_NOTICE in line


def test_watch_probe_discards_truncated_scan_summary(tmp_path: Path) -> None:
    """TCK-SEC-002b: the background-watch probe never consumes the scan
    summary — its ``truncated`` flag is ignored on the watch path — so the
    truncation notice cannot spam every poll tick. It belongs to explicit
    scan/rescan narration only (see ``_make_watch_probe`` docstring)."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    with Store(store_path) as store:
        wallet = store.get_wallet_by_name("default")
        assert wallet is not None
        addr = derive_addresses(parse_watch_key(ZPUB), 0, 0, 1)[0].address
        store.upsert_txs(
            [
                TxRecord(
                    wallet_id=wallet.id,
                    txid="cc" * 32,
                    height=800_000,
                    block_time=1_700_000_000,
                    fee_sats=1000,
                    direction="in",
                    raw_summary=None,
                )
            ]
        )
        store.replace_utxos_for_wallet(
            wallet.id,
            [
                UtxoRecord(
                    wallet_id=wallet.id,
                    txid="cc" * 32,
                    vout=0,
                    address=addr,
                    value_sats=50_000,
                    confirmed=1,
                    height=800_000,
                )
            ],
        )
        probe = app_module._make_watch_probe(
            store, wallet.id, lambda: _make_scan_summary(truncated=True)
        )
        events = probe()
    # The probe surfaces incoming events only; the truncated scan summary
    # (and its notice) is discarded — nothing to spam on repeated polls.
    assert any(e.incoming for e in events)
    assert all(
        app_module.TRUNCATION_NOTICE not in v
        for e in events
        for v in asdict(e).values()
        if isinstance(v, str)
    )


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
        ["--stub-llm", "--zpub", ZPUB],
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
    assert ZPUB not in joined and wd.descriptor not in joined


def test_out_of_window_warning_absent_without_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
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
            ["--stub-llm", "--zpub", ZPUB],
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
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["give me a new address", "exit"],
        store_path=store_path,
    )
    assert code == 0
    assert f"Fresh receive address (index 0): {expected0}" in "\n".join(outputs)
    assert recorded == []  # allocation is network-free

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
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
        ["--stub-llm", "--zpub", ZPUB],
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


def test_repl_refuses_testnet_vpub_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = _store_path(tmp_path)
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        lambda _req: None,  # the client factory is never reached
        [],
        store_path=store_path,
    )
    assert code == 2
    joined = "\n".join(outputs)
    assert "mainnet-only" in joined
    assert VPUB not in joined
    # Fail-closed before any store side effects for the rejected key.
    assert not store_path.exists()


# -------------------------------------------------- gap-limit env knob (TCK-CFG-001)


def test_env_gap_limit_unit_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOCALWALLET_GAP_LIMIT resolves to an int; empty/unset → None; a
    non-integer or out-of-range value fails closed with a value-free error."""
    monkeypatch.delenv(GAP_LIMIT_ENV_VAR, raising=False)
    assert _env_gap_limit(Settings()) is None
    assert _env_gap_limit(Settings(gap_limit="")) is None
    assert _env_gap_limit(Settings(gap_limit="  2  ")) == 2
    assert _env_gap_limit(Settings(gap_limit="1000")) == 1000
    for bad in ("abc", "30.5", "0", "-1", "1001", "2 0"):
        with pytest.raises(ValueError) as excinfo:
            _env_gap_limit(Settings(gap_limit=bad))
        assert GAP_LIMIT_ENV_VAR in str(excinfo.value)
        # Value-free: the actual (malformed) value is never echoed as itself.
        # ("0" is skipped — it legitimately appears inside "1000".)
    for bad in ("abc", "30.5", "2 0"):
        with pytest.raises(ValueError) as excinfo:
            _env_gap_limit(Settings(gap_limit=bad))
        assert bad not in str(excinfo.value)  # value-free


def test_repl_refuses_malformed_gap_limit_env_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-integer LOCALWALLET_GAP_LIMIT refuses startup with exit 2 and a
    value-free message (mirrors the zpub config-error path), before any store
    side effect."""
    store_path = _store_path(tmp_path)
    monkeypatch.setenv(GAP_LIMIT_ENV_VAR, "abc")
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        lambda _req: None,  # the client factory is never reached
        [],
        store_path=store_path,
    )
    assert code == 2
    joined = "\n".join(outputs)
    assert GAP_LIMIT_ENV_VAR in joined
    assert "abc" not in joined  # value-free
    assert not store_path.exists()  # no store side effects on the config error


def test_env_gap_limit_walk_and_precedence_over_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOCALWALLET_GAP_LIMIT=2 makes the startup scan walk a 2-gap window,
    and the env wins over a wider DB ``gap_limit`` setting. Probe-count
    assertion over MockTransport: an empty wallet with gap 2 → indices 0..1
    per branch = 4 txs probes; zero /utxo fetches — empty history means no
    UTXO (TCK-SCAN-001)."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path, gap_limit=5)  # DB key says 5 — env must win
    monkeypatch.setenv(GAP_LIMIT_ENV_VAR, "2")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")  # keep probe count deterministic
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, _outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["exit"],
        store_path=store_path,
        auto_scan=True,
    )
    assert code == 0
    txs_probes = [r for r in recorded if r.url.path.endswith("/txs")]
    utxo_probes = [r for r in recorded if r.url.path.endswith("/utxo")]
    assert len(txs_probes) == 4  # 2 branches × gap-2 window
    assert len(utxo_probes) == 0  # TCK-SCAN-001: nothing to fetch


def test_db_gap_limit_used_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With LOCALWALLET_GAP_LIMIT unset the DB ``gap_limit`` setting is
    honored unchanged: gap 2 → indices 0..1 per branch (4 txs probes,
    0 utxo — TCK-SCAN-001 skip for the empty-history wallet)."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path, gap_limit=TEST_GAP)  # DB key = 2
    monkeypatch.delenv(GAP_LIMIT_ENV_VAR, raising=False)
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")  # keep probe count deterministic
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)

    code, _outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["exit"],
        store_path=store_path,
        auto_scan=True,
    )
    assert code == 0
    txs_probes = [r for r in recorded if r.url.path.endswith("/txs")]
    utxo_probes = [r for r in recorded if r.url.path.endswith("/utxo")]
    assert len(txs_probes) == 4  # 2 branches × gap-2 window
    assert len(utxo_probes) == 0  # TCK-SCAN-001: nothing to fetch


def test_repl_without_zpub_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-LAUNCH-001 note: the store is pinned to an EMPTY tmp path —
    the new stored-key rung means a headless CLI launch otherwise reuses
    whatever wallet the repo-root default store carries (a real user's,
    in a real run; in tests, never)."""
    monkeypatch.delenv(ZPUB_ENV_VAR, raising=False)
    code, outputs = _run_captured(
        ["--stub-llm"],
        monkeypatch,
        lambda _req: None,
        [],
        store_path=tmp_path / "empty.db",
    )
    assert code == 2
    assert "No watch key configured" in "\n".join(outputs)
    assert ZPUB_ENV_VAR in "\n".join(outputs)


def test_no_resolvable_default_falls_back_to_the_demo_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-LAUNCH-001/002: a launch with no model configured AND no
    resolvable pinned default (unreadable manifest / no ``default`` entry —
    nothing the app could offer to download) still falls back to the
    deterministic dev stub with a VISIBLE banner naming the demo mode and
    the real-model env var; the session runs. (When the default IS
    resolvable but simply not downloaded, the launch instead arms the
    Yes/No download card — pinned in tests/test_launch.py. The deliberate
    ``--stub-llm`` flag prints neither — tested in tests/test_launch.py.)"""
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    # Machine-independent: a real models/bin download would make the default
    # RESOLVE and skip this branch (TCK-LAUNCH-002 resolution matrix).
    monkeypatch.setattr(app_module, "_resolve_default_model", lambda: None)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv(app_module.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    recorded: list[httpx.Request] = []
    handler = _scan_handler(recorded)
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(handler))

    outputs: list[str] = []
    code = run(
        ["--zpub", ZPUB],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "demo mode (canned data)" in joined
    assert "LOCALWALLET_MODEL_PATH" in joined
    assert "--stub-llm" not in joined  # the banner points at the model, not the flag
    assert app_module.MODEL_CARD_QUESTION not in joined  # nothing to download
    assert recorded == []  # AUTO_SCAN/monitor untouched: the exit turn scans nothing


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
        ["--stub-llm", "--zpub", ZPUB],
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
    # addr0 funded: the /utxo failure stays on the scan path (addresses
    # with empty history are never fetched, TCK-SCAN-001).
    handler = _scan_handler(
        [], utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]},
        utxo_status=503,
    )

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
        store_path=store_path,
        auto_scan=True,
        sync_first_line=True,  # balance runs after the failed async scan is narrated
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "warning: startup scan failed" in joined
    assert "chain unavailable" in joined
    assert "Balance (mainnet):" not in joined
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
#: mainnet P2WPKH address OUTSIDE the gap-2 scan window, so the chain
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

#: The txid the mock broadcast endpoint reports is COMPUTED from the posted
#: transaction hex (embit ``txid()`` — TCK-SEC-004 change 1 binds the chain
#: client's response to the tx actually sent, so the mock backend must
#: behave like an honest one and echo the posted transaction's txid).
#: Assertions derive the expected value from the flow's signed record via
#: :func:`_flow_txid`.


def _flow_txid(flow: Any) -> str:
    """The txid of the flow's signed (re-validated) transaction — what an
    honest backend echoes back and what the app must record/narrate."""
    return _extract_signed_tx(flow.signed.psbt_base64).txid().hex()

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

    Serves per-address txs (the funding-tx mirror of the scripted utxo
    payload, TCK-SCAN-001: an address with UTXOs always has their funding
    transactions in its history — the scan no longer fetches ``/utxo``
    for empty-history addresses) and utxo payloads, plus
    ``/v1/fees/recommended``,
    ``/v1/prices``, the Phase 3 broadcast POST (``/tx``) and the tx
    status GET (``/tx/<txid>/status``). ``state`` is a mutable injection
    point for the tests:

    - ``state["fees_fail"]`` / ``state["prices_fail"]`` flip those
      endpoints to 500 mid-test;
    - ``state["broadcast_fail"]`` flips the broadcast POST to 500;
    - ``state["broadcast_txid"]`` overrides the txid the POST returns
      (default: the txid COMPUTED from the posted transaction — the
      honest-backend echo the chain client binds to, TCK-SEC-004);
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
            # Honest-backend behavior (TCK-SEC-004 change 1): echo the txid
            # COMPUTED from the posted transaction (the chain client binds
            # the response to it). ``state["broadcast_txid"]`` still
            # overrides for explicit mismatch tests.
            from embit.transaction import Transaction as _Tx

            posted_txid = _Tx.parse(bytes.fromhex(request.content.decode("ascii"))).txid().hex()
            return httpx.Response(200, text=state.get("broadcast_txid", posted_txid))
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
            return httpx.Response(
                200, json=_funding_txs(address, utxos_by_addr.get(address, []))
            )
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
    wd = WalletDescriptor.from_key(ZPUB)
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

    TCK-UX-002: a confirm chains straight into the device handoff, so a
    confirmed test turn exports the unsigned PSBT — the harness defaults
    ``LOCALWALLET_SIGNER_DIR`` to a tmp folder (tests that drive the file
    signer deliberately override it) so no test ever writes into the
    repo's default ./psbt-transfer.
    """
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    env: dict[str, str | None] = {
        "LOCALWALLET_SIGNER_DIR": str(tmp_path / "transfer-harness"),
    }
    env.update(extra_env or {})
    for name, value in env.items():
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
        ["--zpub", ZPUB],
        input_fn=read_line,
        output_fn=outputs.append,
        flow=tx_flow,
        generate_fn=fake,
    )
    return code, outputs, tx_flow


def _card_refs(outputs: list[str]) -> list[str]:
    """All ``Ref:`` values shown by FULL card reprints (``/details``),
    in order. The TCK-UX-002 brief card deliberately has no Ref line —
    refs reach a human via ``/details`` and the sign-time filename
    re-print (doc §1 Ref-row: the demotion leans on those re-prints)."""
    return [line.split("Ref: ", 1)[1] for line in outputs if line.startswith("Ref: ")]


#: The exact brief-card line sequence for the canonical fixture send
#: (60,000 sats @ $20,000/BTC, medium default, 100,000-sat coin): every
#: value verbatim from the handler result, thousands separators and the
#: verbatim chain/eta.py hedge (TCK-UX-002 §1 variant A).
BRIEF_CARD_LINES: Final[list[str]] = [
    'Pending — say "sign" to review it on your device, or "cancel" to discard.',
    f"To: {SEND_RECIPIENT}",
    "Pay: 60,000 sats ($12.00 · @ $20,000/BTC)",
    "Fee: 282 sats · 2 sat/vB × 141 vB · medium — ETA ~60-70 min — estimate only, not a guarantee",
    "From: your wallet (1 source) · 39,718 sats come back as change",
    (
        'How important is this one? Say "faster" to confirm sooner (a slightly higher fee) '
        'or "slower" to save money (it may take longer) — or say "sign" to keep this rate '
        "· full breakdown: /details"
    ),
]

#: Variant B (a speed preference was stated / the offer was answered):
#: same lines, tail collapses to the details link only.
BRIEF_CARD_LINES_VARIANT_B: Final[list[str]] = [
    *BRIEF_CARD_LINES[:5],
    "full breakdown: /details",
]


def test_brief_card_full_line_sequence_variant_a() -> None:
    """The renderer's default (variant A) card, line-for-line pinned:
    ask → To → Pay → Fee (absorbs size + verbatim ETA hedge) → From
    (sources + change reassurance) → ONE conditional tail."""
    lines: list[str] = []
    app_module._print_brief_card(
        {
            "recipient": SEND_RECIPIENT,
            "amount_sats": 60_000,
            "usd_cents": 1_200,
            "btc_usd": 20_000.0,
            "rate_age_s": 0,
            "fee_sats": 282,
            "fee_rate_sat_vb": 2,
            "vsize": 141,
            "fee_target": "medium",
            "eta_wording": "~60-70 min — estimate only, not a guarantee",
            "inputs_count": 1,
            "change_sats": 39_718,
            "fee_target_defaulted": True,
        },
        lines.append,
    )
    assert lines == BRIEF_CARD_LINES


def test_brief_card_variant_b_tail_no_preference_asked_twice() -> None:
    """``fee_target_defaulted`` False (stated preference or answered
    offer): the card stops asking — tail is the /details link alone."""
    lines: list[str] = []
    app_module._print_brief_card(
        {
            "recipient": SEND_RECIPIENT,
            "amount_sats": 60_000,
            "usd_cents": 1_200,
            "btc_usd": 20_000.0,
            "rate_age_s": 0,
            "fee_sats": 282,
            "fee_rate_sat_vb": 2,
            "vsize": 141,
            "fee_target": "medium",
            "eta_wording": "~60-70 min — estimate only, not a guarantee",
            "inputs_count": 1,
            "change_sats": 39_718,
            "fee_target_defaulted": False,
        },
        lines.append,
    )
    assert lines == BRIEF_CARD_LINES_VARIANT_B
    assert "How important" not in "\n".join(lines)


def test_brief_card_plurals_and_optional_segments() -> None:
    """Multi-source pluralization; absent optional fields DROP their
    segment (no ``unavailable`` noise spliced into merged lines, no
    fabricated 0); no change ⇒ no change reassurance clause; no USD ⇒
    no rate parenthetical (the accepted re-show degrade)."""
    lines: list[str] = []
    app_module._print_brief_card(
        {
            "recipient": "bc1qtest",
            "amount_sats": 250_000,
            "fee_sats": 300,
            "inputs_count": 2,
        },
        lines.append,
    )
    assert lines[2] == "Pay: 250,000 sats"
    assert lines[3] == "Fee: 300 sats"
    assert lines[4] == "From: your wallet (2 sources)"
    assert lines[5] == "full breakdown: /details"  # defaulted key absent → variant B
    assert "unavailable" not in "\n".join(lines)


def test_brief_card_absent_amounts_fail_closed_markers() -> None:
    """The TCK-SEC-004 change-4 class carries over: a primary value that
    would print as an optional segment's base renders the explicit
    ``unavailable`` marker, never a fabricated zero."""
    lines: list[str] = []
    app_module._print_brief_card({}, lines.append)
    assert "Pay: unavailable" in lines
    assert "Fee: unavailable" in lines
    assert "From: your wallet (sources unavailable)" in lines
    assert not any(line.startswith(("Pay: 0", "Fee: 0")) for line in lines)


def test_confirmation_card_never_fabricates_absent_values() -> None:
    """TCK-SEC-004 change 4 (D LOW-3 / SR-006 class): a handler payload
    missing a card key renders an explicit ``unavailable`` marker — never a
    fabricated ``0`` on the money-critical card surface."""
    lines: list[str] = []
    app_module._print_confirmation_card({}, lines.append)
    joined = "\n".join(lines)
    assert "Amount: unavailable" in joined
    assert "Fee: unavailable" in joined
    assert "Size: unavailable" in joined
    assert "Inputs: unavailable" in joined
    assert "Expires: unavailable" in joined
    # No fabricated values anywhere on the card.
    assert "0 sats" not in joined
    assert "0 sat/vB" not in joined
    assert "0 vB" not in joined
    assert "Inputs: 0" not in joined
    assert "~0 min" not in joined


def test_confirmation_card_absent_fee_subkeys_do_not_fabricate() -> None:
    """A present ``fee_sats`` with absent rate/target degrades honestly —
    no fabricated ``0 sat/vB`` / empty-target segment."""
    lines: list[str] = []
    app_module._print_confirmation_card({"fee_sats": 282}, lines.append)
    fee_line = next(line for line in lines if line.startswith("Fee: "))
    assert fee_line == "Fee: 282 sats"
    assert "0 sat/vB" not in fee_line


def test_confirmation_card_full_payload_is_byte_identical_to_previous_format() -> None:
    """The absent-key hardening must not change the fully-populated card:
    every line identical to the pre-change format (TCK-UX-005: the fresh
    rate now renders ``@ $20,000/BTC`` instead of its age)."""
    result: dict[str, Any] = {
        "amount_sats": 60_000,
        "usd_cents": 1_200,
        "btc_usd": 20_000.0,
        "rate_age_s": 0,
        "recipient": "bc1qtest",
        "fee_sats": 282,
        "fee_rate_sat_vb": 2,
        "fee_target": "medium",
        "vsize": 141,
        "inputs_count": 1,
        "change_sats": 39_718,
        "expires_in_s": 600,
        "tx_ref": "abc12345",
    }
    lines: list[str] = []
    app_module._print_confirmation_card(result, lines.append)
    assert lines == [
        "Amount: 60000 sats ($12.00 · @ $20,000/BTC)",
        "To: bc1qtest",
        "Fee: 282 sats (2 sat/vB, medium target)",
        "Size: 141 vB",
        "Inputs: 1",
        "Change: 39718 sats",
        "Expires: ~10 min",
        "Ref: abc12345",
    ]


def test_confirmation_card_stale_rate_keeps_age_wording() -> None:
    """A stale rate (ADR-0011 ladder) shows WHY the number may be off — the
    age wording — instead of the now-untrusted rate figure (TCK-UX-005)."""
    result: dict[str, Any] = {
        "amount_sats": 60_000,
        "usd_cents": 1_200,
        "btc_usd": 20_000.0,
        "rate_age_s": 2_520,
        "rate_stale": True,
        "recipient": "bc1qtest",
        "fee_sats": 282,
        "fee_rate_sat_vb": 2,
        "fee_target": "medium",
        "vsize": 141,
        "inputs_count": 1,
        "change_sats": 39_718,
        "expires_in_s": 600,
        "tx_ref": "abc12345",
    }
    lines: list[str] = []
    app_module._print_confirmation_card(result, lines.append)
    assert "Amount: 60000 sats ($12.00 · rate age 2520s · stale)" in lines
    assert "@ $20,000/BTC" not in lines[0]


def test_confirmation_card_no_rate_omits_both_segments() -> None:
    """No rate (absent ``btc_usd``) → neither the rate figure nor a stale
    age appears; only the USD figure (TCK-UX-005 no-rate fallback)."""
    result: dict[str, Any] = {
        "amount_sats": 60_000,
        "usd_cents": 1_200,
        "recipient": "bc1qtest",
        "fee_sats": 282,
        "fee_rate_sat_vb": 2,
        "fee_target": "medium",
        "vsize": 141,
        "inputs_count": 1,
        "change_sats": 39_718,
        "expires_in_s": 600,
        "tx_ref": "abc12345",
    }
    lines: list[str] = []
    app_module._print_confirmation_card(result, lines.append)
    assert lines[0] == "Amount: 60000 sats ($12.00)"
    assert "rate age" not in lines[0]
    assert "@ $" not in lines[0]


def test_send_flow_happy_path_card_then_dual_key_confirm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'send 60000 sats …' → BRIEF card (variant A offer — no stated speed
    preference) with EXACT selection values → 'yes please' + model
    confirm_tx (real tx_ref) → CONFIRMED, and the GATE-MERGE chain hands
    the tx to the signer in the SAME turn (the old two-step seam line is
    gone; the handoff narration replaces it)."""
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
    # Card lines, EXACT — every value verbatim from the handler result.
    assert BRIEF_CARD_LINES[0] in outputs  # the ask line
    pay_fee_from = outputs[outputs.index(BRIEF_CARD_LINES[0]) + 1 : outputs.index(BRIEF_CARD_LINES[0]) + 5]
    assert pay_fee_from == BRIEF_CARD_LINES[1:5]
    assert BRIEF_CARD_LINES[5] in outputs  # the one-shot offer tail
    joined = "\n".join(outputs)
    # Independent money-math cross-checks of the card figures.
    assert SEND_FEE_SATS == SEND_VSIZE * 2  # fee == vsize × rate
    assert SEND_AMOUNT_SATS + SEND_FEE_SATS + SEND_CHANGE_SATS == 100_000
    # Dual-key confirm: same-turn "yes please" + matching tx_ref.
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.pending is None
    # GATE-MERGE (TCK-UX-002): confirm chains into the device handoff in
    # the same turn — the file signer's export narration is what the user
    # reads; the retired two-step line never prints.
    assert "Exported to " in joined
    assert "Approved. Next step: sign" not in joined
    assert "Not confirmed" not in joined
    # The sign-time re-print survives (the card's Ref demotion leans on
    # it): the expected signed filename carries the tx_ref prefix.
    assert f"localwallet-signed-{flow.confirmed.tx_ref[:8]}" in joined
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
        "btc_usd",
        "fee_target",
        "fee_target_defaulted",
        "fee_requote",
        "expires_in_s",
        "eta_blocks",
        "eta_minutes",
        "eta_wording",
        # TCK-UTXO-004: display-only narration flags on the FINAL selection
        # (mix warning + consolidation clause) — renderer material, never a
        # FACTS/model field.
        "mixed",
        "folded_count",
    }
    # An unlabeled single-coin send selects exactly as before the amendment:
    # no mix, no fold (the flags default to the render-off shape).
    assert result["mixed"] is False
    assert result["folded_count"] == 0
    assert result["recipient"] == SEND_RECIPIENT
    assert result["fee_sats"] == result["vsize"] * result["fee_rate_sat_vb"]
    assert result["amount_sats"] == SEND_AMOUNT_SATS
    assert result["change_sats"] == SEND_CHANGE_SATS
    assert result["usd_cents"] == SEND_USD_CENTS
    assert result["rate_stale"] is False
    assert result["rate_age_s"] == 0
    assert result["fee_target"] == "medium"  # MEDIUM default when omitted
    # Display-only plumbing (TCK-UX-002 §2.0): the omitted-vs-defaulted
    # distinction survives to render time ONLY as this result key — the
    # flow record still carries just the resolved target.
    assert result["fee_target_defaulted"] is True  # envelope omitted it
    assert result["fee_requote"] is False
    assert result["expires_in_s"] == 600
    # Narration-only ETA (TCK-P5-002): MEDIUM base = 6 blocks × 10 min.
    assert result["eta_blocks"] == 6
    assert result["eta_minutes"] == 60
    assert "estimate only" in result["eta_wording"]
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
    assert 'Still pending — say "sign" to send it to your device' in joined
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
            "/details",  # reprint the full card → surfaces its Ref line
            "no thanks",
            f"send 60000 sats to {SEND_RECIPIENT}",
            "/details",
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


def test_send_flow_different_destination_while_pending_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second send at a DIFFERENT amount while one pends still refuses
    (the re-quote is same-destination only — money never silently
    switches): still-pending guide + the SAME pending re-shown (same
    tx_ref via the /details reprint)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    other = json.dumps(
        {
            "v": 0,
            "intent": "create_tx",
            "params": {"recipient": SEND_RECIPIENT, "amount_sats": 61_000},
        }
    )
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            f"send 61000 sats to {SEND_RECIPIENT}",
            "/details",
            "exit",
        ],
        ["create", other],
    )
    joined = "\n".join(outputs)
    assert 'Still pending — say "sign" to send it to your device' in joined
    refs = _card_refs(outputs)
    assert refs == [flow.pending.tx_ref]  # the SAME pending transaction
    assert flow.pending.amount_sats == SEND_AMOUNT_SATS
    assert flow.state is TxFlowStatus.CREATED


def test_send_flow_identical_reissue_is_a_same_rung_requote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FLOW-REQUOTE at the REPL: an identical recipient+amount create_tx
    while CREATED is a dispatcher-owned REPLACEMENT, not a refusal —
    same-rung (both-defaulted medium) rebuild: plain re-quote lead, fresh
    tx_ref, and the offer STILL shown (the user never stated a speed
    preference, so ``fee_target_defaulted`` stays true — §2.0 trigger)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "/details",
            f"send 60000 sats to {SEND_RECIPIENT}",
            "/details",
            "exit",
        ],
        ["create", "create"],
    )
    joined = "\n".join(outputs)
    assert app_module._CARD_REQUOTE_LEAD_SAME_RUNG in joined
    assert "Still pending" not in joined  # not a refusal anymore
    refs = _card_refs(outputs)
    assert len(refs) == 2 and refs[0] != refs[1]  # replaced: new identity
    assert refs[1] == flow.pending.tx_ref
    assert flow.state is TxFlowStatus.CREATED
    # The re-quoted card re-offers the choice (both envelopes defaulted).
    assert joined.count(BRIEF_CARD_LINES[5]) == 2


# --------------------------------------------------- FLOW-REQUOTE mechanics
#
# (TCK-UX-002 deliverable 3; doc §2.1/§2.3.) Table-level for determinism:
# the ladder moves, ceiling/floor refusals, commit-only-on-success, and
# the inert old ref — everything the dispatcher-owned replacement must
# guarantee on the money path.


def _requote_envelope(fee_target: str | None):
    """A same-destination create_tx envelope at an explicit (or omitted)
    rung — what the model emits for "faster"/"slower"/"medium"."""
    body: dict[str, Any] = {"recipient": SEND_RECIPIENT, "amount_sats": SEND_AMOUNT_SATS}
    if fee_target is not None:
        body["fee_target"] = fee_target
    return validate_payload(json.dumps({"v": 0, "intent": "create_tx", "params": body}))


def _rate_envelope(fee_rate_sat_vb: int):
    """A same-destination create_tx envelope with the USER-QUOTED literal
    rate (TCK-FEE-002) — the answer to the ceiling ask."""
    return validate_payload(
        json.dumps(
            {
                "v": 0,
                "intent": "create_tx",
                "params": {
                    "recipient": SEND_RECIPIENT,
                    "amount_sats": SEND_AMOUNT_SATS,
                    "fee_rate_sat_vb": fee_rate_sat_vb,
                },
            }
        )
    )


def _send_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    utxo: dict[str, Any],
    *,
    clock: Any = None,
):
    addr0 = derive_fixture_addresses(1)[0]
    table, store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [utxo]}),
        flow=TxFlow(clock=clock) if clock is not None else None,
    )
    return table, store, client, flow, session


def test_faster_requote_replaces_pending_fresh_ref_ttl_and_inert_old(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """medium → "faster" (fast): coin selection RE-RUNS at the new rung —
    fee/vsize/change/ETA all refresh together, NEW tx_ref + fresh TTL,
    variant B (explicit target retires the offer); the OLD ref is inert
    (confirm fails the verbatim-match), and the NEW ref confirms under
    the dual key."""
    ticks = {"now": 1_000.0}
    table, store, client, flow, session = _send_table(
        monkeypatch, tmp_path, SEND_UTXO, clock=lambda: ticks["now"]
    )
    first = table[IntentName.CREATE_TX](_requote_envelope(None))
    assert first["fee_target_defaulted"] is True and first["fee_requote"] is False

    ticks["now"] = 1_050.0  # time passes between the rungs
    faster = table[IntentName.CREATE_TX](_requote_envelope("fast"))
    assert faster.get("error") is None
    assert faster["fee_requote"] is True
    assert faster["requote_direction"] == "faster"
    assert faster["fee_target"] == "fast"
    assert faster["fee_target_defaulted"] is False  # explicit → offer retired
    # The ladder is estimator-driven: fast rung = 3 sat/vB on this fixture.
    assert faster["fee_rate_sat_vb"] == 3
    assert faster["fee_sats"] == faster["vsize"] * 3 > first["fee_sats"]
    assert faster["change_sats"] < first["change_sats"]  # money math re-ran
    assert faster["eta_minutes"] < first["eta_minutes"]  # ETA refreshed too
    # NEW ref + FRESH TTL (created_at re-read from the flow clock).
    assert faster["tx_ref"] != first["tx_ref"]
    assert faster["expires_in_s"] == 600
    assert flow.pending is not None and flow.pending.created_at == 1_050.0
    assert flow.pending.tx_ref == faster["tx_ref"]

    # The old ref is inert — fail closed even with a same-turn CONFIRM.
    session.gate_decision = GateDecision.CONFIRM
    stale = table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": first["tx_ref"]}})
        )
    )
    assert stale["error"] == "confirm_refused"
    assert "does not match" in str(stale["detail"])
    assert flow.state is TxFlowStatus.CREATED

    fresh = table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": faster["tx_ref"]}})
        )
    )
    assert fresh["status"] == "confirmed"
    client.close()
    store.close()


def test_slower_and_same_rung_medium_requotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit medium onto a defaulted medium is the legal same-rung
    rebuild (identical numbers, NO direction word, fresh ref, §2.3);
    medium → "slower" (slow) then moves down the ladder ("slower")."""
    table, store, client, flow, _session = _send_table(
        monkeypatch, tmp_path, SEND_UTXO
    )
    first = table[IntentName.CREATE_TX](_requote_envelope(None))
    medium = table[IntentName.CREATE_TX](_requote_envelope("medium"))
    assert "requote_direction" not in medium  # same-rung: no direction word
    assert medium["fee_sats"] == first["fee_sats"]  # identical numbers
    assert medium["tx_ref"] != first["tx_ref"]  # fresh identity anyway
    assert medium["fee_target_defaulted"] is False  # explicit → offer retired
    slower = table[IntentName.CREATE_TX](_requote_envelope("slow"))
    assert slower["requote_direction"] == "slower"
    assert slower["fee_rate_sat_vb"] == 1
    assert slower["fee_sats"] < first["fee_sats"]
    assert flow.state is TxFlowStatus.CREATED
    client.close()
    store.close()


def test_requote_ceiling_and_floor_refuse_pending_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """"faster" when already fast → ceiling refusal; "slower" when already
    slow → floor. The staged record is UNTOUCHED (same ref, same money),
    and no replacement is even attempted (§2.3)."""
    table, store, client, flow, _session = _send_table(
        monkeypatch, tmp_path, SEND_UTXO
    )
    fast = table[IntentName.CREATE_TX](_requote_envelope("fast"))
    ceiling = table[IntentName.CREATE_TX](_requote_envelope("fast"))
    assert ceiling["error"] == "tx_pending"
    assert ceiling["rate_notice"] == "ceiling"
    assert ceiling["tx_ref"] == fast["tx_ref"]  # the pending re-shown intact
    assert flow.pending.tx_ref == fast["tx_ref"]

    slow = table[IntentName.CREATE_TX](_requote_envelope("slow"))
    assert slow["tx_ref"] != fast["tx_ref"]  # fast→slow DOES move (down)
    floor = table[IntentName.CREATE_TX](_requote_envelope("slow"))
    assert floor["rate_notice"] == "floor"
    assert flow.pending.tx_ref == slow["tx_ref"]
    client.close()
    store.close()


def test_explicit_rate_fresh_create_bids_the_literal_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-FEE-002: a create_tx carrying fee_rate_sat_vb bids the USER-QUOTED
    rate, not the ladder — no estimator call at all — and stages with NO
    rung (fee_target None, no fabricated ETA, offer retired like any stated
    preference). The fixture's medium rung is 2 sat/vB; 5 proves the override."""
    addr0 = derive_fixture_addresses(1)[0]
    table, store, _wallet, client, recorded, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]})
    )
    result = table[IntentName.CREATE_TX](_rate_envelope(5))
    assert result.get("error") is None
    assert result["fee_rate_sat_vb"] == 5
    assert result["fee_sats"] == result["vsize"] * 5  # the literal bid, verbatim
    assert result["fee_target"] is None  # explicit rate: no rung recorded
    assert result["fee_target_defaulted"] is False  # stated preference → offer retired
    assert "eta_minutes" not in result  # rung-based ETA fails closed, never fabricated
    assert result["fee_requote"] is False
    # The estimator was NEVER consulted (the price oracle still was, for display).
    assert not any(r.url.path.endswith("/v1/fees/recommended") for r in recorded)
    pending = flow.pending
    assert pending is not None
    assert pending.fee_rate_sat_vb == 5 and pending.fee_target is None
    # Full card render survives the None rung (no "None target" line ever).
    lines: list[str] = []
    app_module._print_confirmation_card(result, lines.append)
    assert not any("None target" in ln for ln in lines)
    assert any("5 sat/vB" in ln for ln in lines)
    client.close()
    store.close()


def test_ceiling_answer_with_explicit_rate_rebuilds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The UX-004 loop closes end to end (TCK-FEE-002): at the top rung a
    third "faster" refuses with the ceiling ask; the ANSWER — a same-
    destination re-quote with fee_rate_sat_vb — bypasses the rung guard and
    rebuilds through the SAME FLOW-REQUOTE path: commit-only-on-success,
    fresh tx_ref/TTL, direction "faster", OLD ref inert, NEW ref confirmable."""
    ticks = {"now": 1_000.0}
    table, store, client, flow, session = _send_table(
        monkeypatch, tmp_path, SEND_UTXO, clock=lambda: ticks["now"]
    )
    fast = table[IntentName.CREATE_TX](_requote_envelope("fast"))
    assert fast["fee_rate_sat_vb"] == 3  # ladder's top rung on this fixture
    ceiling = table[IntentName.CREATE_TX](_requote_envelope("fast"))
    assert ceiling["error"] == "tx_pending" and ceiling["rate_notice"] == "ceiling"

    ticks["now"] = 1_060.0
    answered = table[IntentName.CREATE_TX](_rate_envelope(5))
    assert answered.get("error") is None  # the ask's answer DOES rebuild
    assert answered["fee_requote"] is True
    assert answered["requote_direction"] == "faster"  # 5 > the staged 3 sat/vB
    assert answered["fee_rate_sat_vb"] == 5
    assert answered["fee_sats"] > fast["fee_sats"]  # money math re-ran
    assert answered["tx_ref"] != fast["tx_ref"] and answered["expires_in_s"] == 600
    assert flow.pending.tx_ref == answered["tx_ref"] and flow.pending.created_at == 1_060.0

    # The superseded ref stays inert — fail closed even with a same-turn CONFIRM.
    session.gate_decision = GateDecision.CONFIRM
    stale = table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": fast["tx_ref"]}})
        )
    )
    assert stale["error"] == "confirm_refused"
    fresh = table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": answered["tx_ref"]}})
        )
    )
    assert fresh["status"] == "confirmed"
    client.close()
    store.close()


def test_explicit_rate_requote_direction_and_equal_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit-rate re-quotes derive direction from the LITERAL rates
    (no rungs to compare): 1 sat/vB onto the defaulted 2 sat/vB reads
    "slower"; re-quoting the SAME rate is the legal no-op-ish rebuild
    (identical numbers, no direction word, fresh ref — §2.3 precedent)."""
    table, store, client, flow, _session = _send_table(monkeypatch, tmp_path, SEND_UTXO)
    first = table[IntentName.CREATE_TX](_requote_envelope(None))  # medium → 2 sat/vB
    down = table[IntentName.CREATE_TX](_rate_envelope(1))
    assert down["requote_direction"] == "slower"
    assert down["fee_sats"] < first["fee_sats"]
    same = table[IntentName.CREATE_TX](_rate_envelope(1))
    assert "requote_direction" not in same  # equal rate: no direction word
    assert same["fee_sats"] == down["fee_sats"]  # identical numbers
    assert same["tx_ref"] != down["tx_ref"]  # fresh identity anyway
    assert flow.state is TxFlowStatus.CREATED
    client.close()
    store.close()


def test_explicit_rate_requote_failure_keeps_original_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Commit-only-on-success holds on the explicit-rate path: a rate whose
    higher fee pushes the wallet short surfaces insufficient_funds and the
    ORIGINAL pending survives — the rung-guard bypass never half-replaces."""
    table, store, client, flow, session = _send_table(monkeypatch, tmp_path, SEND_UTXO)
    near = validate_payload(
        json.dumps(
            {
                "v": 0,
                "intent": "create_tx",
                "params": {"recipient": SEND_RECIPIENT, "amount_sats": 99_700},
            }
        )
    )
    original = table[IntentName.CREATE_TX](near)  # 2 sat/vB, residue folded, fee 300
    assert original.get("error") is None
    too_high = validate_payload(
        json.dumps(
            {
                "v": 0,
                "intent": "create_tx",
                "params": {
                    "recipient": SEND_RECIPIENT,
                    "amount_sats": 99_700,
                    "fee_rate_sat_vb": 3,  # would need 100_030 sats
                },
            }
        )
    )
    refused = table[IntentName.CREATE_TX](too_high)
    assert refused == {
        "error": "insufficient_funds",
        "needed_sats": 100_030,
        "available_sats": 100_000,
    }
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is not None and flow.pending.tx_ref == original["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    confirmed = table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps(
                {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": original["tx_ref"]}}
            )
        )
    )
    assert confirmed["status"] == "confirmed"
    client.close()
    store.close()


def test_requote_insufficient_funds_keeps_original_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Commit-only-on-success ordering (§2.1, ticket-mandated): a "faster"
    re-quote whose higher fee pushes the wallet short surfaces the
    existing insufficient_funds line and the ORIGINAL pending survives —
    the replacement is validated BEFORE the old record is discarded."""
    table, store, client, flow, session = _send_table(
        monkeypatch, tmp_path, SEND_UTXO
    )
    # 99,700 of a 100,000 coin: finalizes at 2 sat/vB (residue folded,
    # fee 300) but NOT at 3 sat/vB (would need 100,030 sats).
    near = validate_payload(
        json.dumps(
            {
                "v": 0,
                "intent": "create_tx",
                "params": {"recipient": SEND_RECIPIENT, "amount_sats": 99_700},
            }
        )
    )
    original = table[IntentName.CREATE_TX](near)
    assert original.get("error") is None
    assert original["change_sats"] is None
    assert original["fee_sats"] == 300

    fast = table[IntentName.CREATE_TX](
        validate_payload(
            json.dumps(
                {
                    "v": 0,
                    "intent": "create_tx",
                    "params": {
                        "recipient": SEND_RECIPIENT,
                        "amount_sats": 99_700,
                        "fee_target": "fast",
                    },
                }
            )
        )
    )
    assert fast == {
        "error": "insufficient_funds",
        "needed_sats": 100_030,
        "available_sats": 100_000,
    }
    # The ORIGINAL pending is intact — same identity, still confirmable.
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is not None and flow.pending.tx_ref == original["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    confirmed = table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps(
                {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": original["tx_ref"]}}
            )
        )
    )
    assert confirmed["status"] == "confirmed"
    client.close()
    store.close()


def test_requote_narration_and_ceiling_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The renderer wires the §2.3 copy: direction-bearing lead +
    variant-B card after a faster re-quote; ceiling/floor results print
    ONLY the notice line (the staged card stays valid on screen). The
    ceiling copy (TCK-UX-004) asks for an explicit sat/vB rate rather than
    silently refusing at the top rung."""
    lines: list[str] = []
    app_module._print_create_tx(
        {
            "tx_ref": "r2",
            "amount_sats": 60_000,
            "recipient": "bc1qtest",
            "fee_sats": 423,
            "fee_rate_sat_vb": 3,
            "vsize": 141,
            "change_sats": 39_577,
            "inputs_count": 1,
            "fee_target": "fast",
            "eta_wording": "~10-20 min — estimate only, not a guarantee",
            "fee_target_defaulted": False,
            "fee_requote": True,
            "requote_direction": "faster",
            "expires_in_s": 600,
        },
        lines.append,
    )
    assert lines[0] == "Re-quoted at the faster rate — review the new fee below:"
    assert lines[1] == app_module._CARD_ASK_LINE
    assert "Fee: 423 sats · 3 sat/vB × 141 vB · fast — ETA ~10-20 min — estimate only, not a guarantee" in lines
    assert lines[-1] == "full breakdown: /details"  # variant B — offer retired

    ceiling: list[str] = []
    app_module._print_create_tx(
        {"error": "tx_pending", "rate_notice": "ceiling"}, ceiling.append
    )
    assert ceiling == [app_module._CARD_RATE_CEILING]
    assert "tell me a rate in sat/vB" in app_module._CARD_RATE_CEILING
    # ...and the ask never leaks an address/amount (value-free, per §2.2.3).
    assert "bc1qtest" not in app_module._CARD_RATE_CEILING
    floor: list[str] = []
    app_module._print_create_tx(
        {"error": "tx_pending", "rate_notice": "floor"}, floor.append
    )
    assert floor == [app_module._CARD_RATE_FLOOR]


# ------------------------------------------------ GATE-MERGE (TCK-UX-002)
#
# "sign" joins the CONFIRM whitelist and the dispatcher chains
# confirm→device-handoff in ONE turn. Pins: the merged happy path, the
# MUST-NOT-WEAKEN list (dual key intact — a gate word without a
# confirm_tx envelope confirms nothing; a confirm_tx with an invented
# tx_ref is refused even on a CONFIRM-classified turn; speed words never
# confirm), and broadcast stays separately gated.


def test_gate_merge_sign_word_confirms_and_chains_in_one_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE merged flow: user says "sign" at CREATED — the gate classifies
    it CONFIRM (whitelist), the model emits confirm_tx (the card asked
    for the device, so sign_tx is not the right envelope; the flow-state
    gate refuses it anyway), and the dispatcher chains the handoff in the
    SAME turn. No separate 'sign' utterance is needed; broadcast is NOT
    reached (separately gated)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "sign", "exit"],
        ["create", "confirm"],
    )
    joined = "\n".join(outputs)
    assert app_module._CARD_ASK_LINE in joined
    # One user utterance carried review + device handoff (file signer:
    # the export IS the handoff; the sign-time ref re-print rides it).
    assert "Exported to " in joined
    assert f"localwallet-signed-{flow.confirmed.tx_ref[:8]}" in joined
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.signed is None and flow.txid is None  # broadcast NOT reached
    assert "Not confirmed" not in joined
    assert "Approved." not in joined  # retired seam line


def test_gate_merge_gate_word_without_envelope_confirms_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MUST NOT WEAKEN: "sign" satisfies the GATE key, but with no
    same-turn confirm_tx envelope (the model responds) the dual key is
    incomplete — the flow stays CREATED with a still-pending guide."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "sign", "exit"],
        ["create", "respond"],
    )
    joined = "\n".join(outputs)
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is not None
    assert 'Still pending — say "sign"' in joined
    assert "Exported to " not in joined


def test_gate_merge_invented_tx_ref_refused_even_on_sign_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MUST NOT WEAKEN (red-team shape): "sign" classifies CONFIRM, but a
    confirm_tx quoting an INVENTED tx_ref still fails the verbatim-match —
    CREATED is preserved and nothing is handed to the device."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    bogus = json.dumps(
        {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "delegated-not"}}
    )
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "sign", "exit"],
        ["create", bogus],
    )
    joined = "\n".join(outputs)
    assert "Not confirmed — tx_ref does not match the pending transaction." in joined
    assert flow.state is TxFlowStatus.CREATED
    assert "Exported to " not in joined


def test_gate_merge_sign_word_wrong_envelope_fails_closed_at_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A model that misreads the card's ask word and emits sign_tx while
    CREATED is refused by the FLOW's state gate (signing cannot skip the
    confirm) — value-free refusal, flow untouched."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    premature_sign = json.dumps(
        {"v": 0, "intent": "sign_tx", "params": {"tx_ref": "pending-not-confirmed"}}
    )
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "sign", "exit"],
        ["create", premature_sign],
    )
    joined = "\n".join(outputs)
    assert "Not signed — no confirmed transaction to sign." in joined
    assert flow.state is TxFlowStatus.CREATED
    assert "Exported to " not in joined


def test_gate_merge_speed_words_never_confirm_even_with_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.2.1 end-to-end: "faster" classifies NOT_A_DECISION, so even a
    model-emitted confirm_tx quoting the REAL pending ref is refused —
    the turn cannot advance past CREATED. (The correct model behaviour —
    a re-quote — is the FLOW-REQUOTE section's territory.)"""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "faster", "exit"],
        ["create", "confirm"],  # confirm quotes the live pending ref
    )
    joined = "\n".join(outputs)
    assert "Not confirmed — confirmation gate not satisfied" in joined
    assert flow.state is TxFlowStatus.CREATED
    assert "Exported to " not in joined


# ----------------------------------------------------------- /details view

def test_details_reprints_full_card_and_gates_on_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``/details`` (ADR-0020 channel, doc §1): reprints the cached FULL
    nine-line card verbatim (Size/Inputs/Expires/Ref — everything the
    brief view merges or demotes) while a tx pends; after the pending is
    cancelled the view is gone (no stale card). The spoken word
    "details" is NOT gate-whitelisted (see test_tx_flow collision pins)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    _code, outputs, _flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "/details",
            "no thanks",
            "/details",
            "exit",
        ],
        ["create", "respond"],
    )
    while_pending = "\n".join(outputs)
    # The full nine-line classic render, verbatim (the brief card's
    # stricter sibling — same data, two depths).
    assert f"Amount: {SEND_AMOUNT_SATS} sats ($12.00 · @ $20,000/BTC)" in while_pending
    assert f"Size: {SEND_VSIZE} vB" in while_pending
    assert "Inputs: 1" in while_pending
    assert "Expires: ~10 min" in while_pending
    refs = _card_refs(outputs)
    assert len(refs) == 1  # exactly the one /details reprint
    assert "Transaction cancelled." in while_pending
    # After the DENY-cancel, /details reports nothing pending (the brief
    # card scrolled off; the cache is not a live-transaction oracle).
    assert app_module._DETAILS_NONE in outputs
    assert outputs.count(app_module._DETAILS_NONE) == 1


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
    assert "Pay: 60,000 sats ($12.00 · @ $20,000/BTC)" in joined
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
    assert "Pay: 60,000 sats" in joined
    assert "rate age" not in joined
    assert (
        "Fee: 282 sats · 2 sat/vB × 141 vB · medium "
        "— ETA ~60-70 min — estimate only, not a guarantee" in joined
    )
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
    assert 'That was ambiguous — say "sign" to proceed' in joined
    assert 'or "cancel" to discard it.' in joined
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
    still supplies the gate's key. GATE-MERGE: the confirm chains into
    the device handoff in the SAME turn (the file-signer export line
    replaces the retired two-step 'Approved' line)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fake = FactsQuotingGenerate(["create", "confirm"])
    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "/details",  # the full-card reprint carries the Ref line
            "yes please",
            "exit",
        ],
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
    (card_ref,) = _card_refs(outputs)
    assert fact_ref == card_ref  # FACTS value == the printed card's ref
    assert app_module._CARD_ASK_LINE in joined
    # Merged flow: the handoff narration is what the user reads (no seam).
    assert "Exported to " in joined
    assert "Approved." not in joined
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
    # The confirm quoted the FACTS ref → dual-key confirm completed, and
    # (GATE-MERGE) chained straight into the device handoff in the same
    # turn — the file-signer export narration replaces the old seam line.
    assert "Exported to " in joined
    assert "Approved." not in joined
    assert flow.state is TxFlowStatus.CONFIRMED


def test_send_flow_eta_reaches_card_and_model_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-P5-002 (ETA as FACTS): the confirmation card prints the ETA line
    and the CREATED-turn FACTS carry the narration-only ETA fact for the
    model (mock path — no network). The ETA is dispatcher-owned and
    deterministic: MEDIUM base = 6 blocks × 10 min = ~60 min."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fake = FactsQuotingGenerate(["create", "respond"])
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "whatever", "exit"],
        ["create", "respond"],
        generate=fake,
    )

    joined = "\n".join(outputs)
    # The brief card's Fee line carries the ETA hedge VERBATIM (the
    # standalone ETA line survives in the /details full render).
    assert " — ETA ~60-70 min — estimate only, not a guarantee" in joined
    # The CREATED-turn FACTS inject the ETA fact so the model can narrate it
    # verbatim (the card is terminal output the model never sees).
    assert "pending_tx_eta_minutes: 60" in fake.prompts[1]
    assert "pending_tx_eta_wording: ~60-70 min — estimate only, not a guarantee" in fake.prompts[1]
    assert flow.state is TxFlowStatus.CREATED


def test_send_flow_reshowed_card_shows_remaining_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FIX 3 (expiry honesty) survives the brief redesign: a re-show 500 s
    after staging (via a DIFFERENT-destination refusal — an identical one
    would be a fresh re-quote) advertises the REMAINING ttl, not the
    nominal 600 s/10 min. Expires is demoted off the brief card, so the
    honest remaining TTL is read off the cached full render (/details)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    clock = {"now": 1000.0}
    reads = {"n": 0}

    def before_line() -> None:
        reads["n"] += 1
        if reads["n"] == 2:  # just before the SECOND (different) send
            clock["now"] = 1500.0  # 500 s have passed

    other = json.dumps(
        {
            "v": 0,
            "intent": "create_tx",
            "params": {"recipient": SEND_RECIPIENT, "amount_sats": 61_000},
        }
    )
    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            f"send 61000 sats to {SEND_RECIPIENT}",  # refusal + re-show
            "/details",
            "exit",
        ],
        ["create", other],
        flow=TxFlow(clock=lambda: clock["now"]),
        before_line=before_line,
    )

    joined = "\n".join(outputs)
    assert 'Still pending — say "sign"' in joined
    # The cached full render of the re-shown card: ~100 s ≈ 1 min left.
    assert "Expires: ~1 min" in joined
    assert "Expires: ~10 min" not in joined
    assert flow.state is TxFlowStatus.CREATED


def test_repl_transcript_export_scrub_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OQ14 (TCK-P5-002): the transcript CLI commands (``/export``, ``/scrub``,
    ``/help``) are deterministic UI features plumbed through the REPL
    ``input_fn`` seam — no protocol/model change. The export file is
    value-free (no addresses)."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    handler = _scan_handler([], utxos_by_addr={})
    export_path = tmp_path / "transcript.txt"
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        [
            "give me a new address",
            f"/export {export_path}",
            "/scrub",
            "/help",
            "exit",
        ],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "Transcript exported" in joined
    assert "Transcript cleared." in joined
    assert "Commands: /details" in joined
    text = export_path.read_text(encoding="utf-8")
    assert "local-wallet session export" in text
    assert "bc1q" not in text


def test_repl_export_refuses_existing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OQ14 (TCK-P5-002): ``/export`` must not silently overwrite an existing
    file — it refuses with a short value-free message and leaves the original
    content unchanged."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    handler = _scan_handler([], utxos_by_addr={})
    existing = tmp_path / "transcript.txt"
    existing.write_text("ORIGINAL CONTENT", encoding="utf-8")
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        handler,
        [
            f"/export {existing}",
            "exit",
        ],
        store_path=store_path,
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "export: file already exists, choose another path" in joined
    assert "Transcript exported" not in joined
    assert existing.read_text(encoding="utf-8") == "ORIGINAL CONTENT"


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
#: The fake device's account node: the private counterpart of the
#: canonical mainnet fixture zpub (coin 0, ADR-0021; see DESCRIPTOR_SEED).
FIXTURE_ACCOUNT_DERIVATION: Final[list[int]] = [84 + 2**31, 0 + 2**31, 0]


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
    account = bip32.HDKey.from_seed(DESCRIPTOR_SEED).derive(FIXTURE_ACCOUNT_DERIVATION)
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
    """Fake hwilib client for the TCK-HW-002 gate: serves the fixture
    ACCOUNT key's pubkey at the descriptor's account path (hwi base
    ``Client.get_pubkey_at_path`` contract — JadeClient jade.py:164
    shape), which the post-open bind hashes to the wallet fingerprint.
    Also serves the device MASTER fingerprint (base
    ``Client.get_master_fingerprint`` → bytes, hwwclient.py:59-67): the
    TCK-HW-003 sign-time patch rewrites this wallet's bip32 derivation
    fingerprints from the account fp to it. It remains a HINT reader only
    — nothing in the trust gate consumes it (ADR-0015 amendments #2/#3)."""

    #: The same master fp ``enumerate`` reports (jade.py MW-4 trace shape).
    MASTER_FP = bytes.fromhex("40dbb192")

    def __init__(self, fingerprint_hex: str, recorder: dict[str, bool]) -> None:
        del fingerprint_hex  # kept call-compatible; the gate never uses it
        self._recorder = recorder

    def get_pubkey_at_path(self, bip32_path: str):
        from types import SimpleNamespace

        from embit import bip32

        # The descriptor origin path this wallet was built for (p2wpkh,
        # mainnet coin 0) — pinned here so a wrong path is a hard test fail.
        assert bip32_path == "m/84'/0'/0'", f"unexpected bind path {bip32_path}"
        account = bip32.HDKey.from_seed(DESCRIPTOR_SEED).derive(
            FIXTURE_ACCOUNT_DERIVATION
        )
        return SimpleNamespace(pubkey=account.key.sec())  # compressed, 33 B

    def get_master_fingerprint(self) -> bytes:
        return self.MASTER_FP

    def close(self) -> None:
        self._recorder["closed"] = True


class _FakeDeviceCommands:
    """hwilib.commands stand-in whose device REALLY signs with the fixture
    wallet key (enumerate → open → account-key bind → signtx, hwi 3.2.0
    API shape). Enumeration reports a MASTER fingerprint distinct from the
    wallet's account fingerprint — the MW-4 debugger repro (TCK-HW-002):
    nothing may compare those two. ``fail_first_sign`` raises a name-mapped
    locked error on the first signtx (DeviceLockedError mid-flow →
    guidance → retry works)."""

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
                # hwilib reports the MASTER fingerprint here (the user's
                # Jade: 40dbb192-style) — never the wallet account fp.
                "fingerprint": "40dbb192",
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
    dual-key confirm → (GATE-MERGE) SAME-TURN chained device handoff (the
    confirm exports; signed_file_missing) → device places the signed file
    → "sign it" (import → revalidate → SIGNED) → broadcast (POST hits the
    mock) → status quotes broadcast_txid from FACTS → confirmed-at-height
    narration → history shows the outbound row. The broadcast POST carries
    exactly the re-validated transaction. NOTE: the merged flow needs ONE
    fewer model step than the old two-step (approve, then sign) flow —
    the export rides the confirm turn, never a separate model call."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict[str, Any] = {}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)
    fake = FactsQuotingGenerate(
        ["create", "confirm", "sign", "broadcast", "status", "history"]
    )

    def before_line() -> None:
        # Device simulation between the confirm-chained export and the
        # import turn (see helper).
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
            "yes please",  # confirm → chained export (no separate sign turn)
            "sign it",  # device placed the file → import → SIGNED
            "broadcast it",
            "what's the status?",
            "show my transactions",
            "exit",
        ],
        ["create", "confirm", "sign", "broadcast", "status", "history"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )

    assert code == 0
    joined = "\n".join(outputs)
    # --- chained handoff on the CONFIRM turn: export + file-missing (§10) --
    assert "Exported to " in joined
    assert "(say: signed localwallet-signed-" in joined
    exported = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
    assert len(exported) == 1
    assert (transfer / (exported[0].name + ".sha256")).exists()  # ADR-0014 sidecar
    # --- import turn: revalidate → SIGNED ---------------------------------
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
    expected_txid = _flow_txid(flow)
    assert f"Sent! txid {expected_txid} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST
    assert flow.txid == expected_txid
    # --- status: the model quoted broadcast_txid from the FACTS ---------
    status_prompt = fake.prompts[4]  # create/confirm/sign/broadcast/STATUS
    assert f"broadcast_txid: {expected_txid}" in status_prompt
    assert "Confirmed at height 870001." in joined
    # --- history: the outbound row (store upsert after broadcast) -------
    assert f"tx {expected_txid[:12]}… out unconfirmed" in joined
    store_path = tmp_path / "store.db"
    with Store(store_path) as store:
        wallet_row = store.get_wallet_by_name("default")
        assert wallet_row is not None
        rows = store.get_txs_for_wallet(wallet_row.id)
        # The funded UTXO's funding tx rides along in history (the mock
        # chain serves the truthful mirror, TCK-SCAN-001).
        assert [(r.txid, r.height, r.direction, r.fee_sats) for r in rows] == [
            (expected_txid, None, "out", SEND_FEE_SATS),
            ("d" * 64, None, "in", None),
        ]


def test_send_lifecycle_hwi_signer_locked_retry_then_broadcast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HWI path with a fake commands module: candidate gate + post-open
    account-key bind (master-fp enumeration never gates, TCK-HW-002) →
    GATE-MERGE: the confirm turn's CHAINED device handoff hits
    DeviceLockedError mid-flow → §10 guidance line (flow stays CONFIRMED) →
    bare 'retry' (DETERMINISTICALLY INTERCEPTED at CONFIRMED — no model
    call for it, TCK-HW-002) → sign → revalidate → broadcast. Flow
    discipline throughout; the merged flow removes the old separate
    'sign it' turn (the handoff now rides the confirm)."""
    from localwallet.signer.hwi import HwiUsbSigner as RealHwiUsbSigner

    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fingerprint = _fixture_parsed().hd_key.my_fingerprint.hex()
    commands = _FakeDeviceCommands(fingerprint, fail_first_sign=True)
    monkeypatch.setattr(
        app_module,
        "HwiUsbSigner",
        lambda fp, account_path: RealHwiUsbSigner(
            fp, account_path, commands_module=commands
        ),
    )
    # 'retry' at CONFIRMED never reaches the model (TCK-HW-002): the plan
    # has no step for it — 'broadcast it' consumes "broadcast".
    fake = FactsQuotingGenerate(["create", "confirm", "broadcast"])

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",  # confirm → chained sign → locked → CONFIRMED
            "retry",  # intercepted re-sign → SIGNED
            "broadcast it",
            "exit",
        ],
        ["create", "confirm", "broadcast"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER": "hwi"},
    )

    joined = "\n".join(outputs)
    # First attempt (the chained handoff): the locked-device error surfaces
    # its guidance VERBATIM (code-owned §10 text). The retry: the fake
    # device signs; revalidation passes; the broadcast completes.
    assert "Enter your PIN/passphrase on the device, then say 'retry'." in joined
    assert "Signed and verified ✓ txid " in joined
    assert commands.sign_calls == 2  # the locked (chained) attempt + the retry
    assert commands.rec["closed"] is True  # device handle released both times
    assert f"Sent! txid {_flow_txid(flow)} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST
    # The retry turn was model-free: four REPL utterances before 'exit',
    # the model saw three — and no prompt was ASKED about 'retry'
    # (a re-injected history line is mid-prompt, never the final turn).
    assert len(fake.prompts) == 3
    assert not any(p.endswith("user: retry\n\nenvelope:") for p in fake.prompts)


def test_retry_at_confirmed_resigns_deterministically_without_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-HW-002 pin (GATE-MERGE-aware): the confirm's CHAINED handoff
    hits the locked device (flow stays CONFIRMED); at ``CONFIRMED`` a bare
    ``"retry"`` (stripped, case-insensitive) re-invokes the sign_tx handler
    DIRECTLY with the dispatcher-owned confirmed tx_ref — the model is
    never consulted for that turn — and the full sign pipeline (gate →
    bind → device sign → revalidate → SIGNED) runs. Confirmed by the
    model-call count: the retry never reaches the model."""
    from localwallet.signer.hwi import HwiUsbSigner as RealHwiUsbSigner

    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fingerprint = _fixture_parsed().hd_key.my_fingerprint.hex()
    commands = _FakeDeviceCommands(fingerprint, fail_first_sign=True)
    monkeypatch.setattr(
        app_module,
        "HwiUsbSigner",
        lambda fp, account_path: RealHwiUsbSigner(
            fp, account_path, commands_module=commands
        ),
    )
    fake = FactsQuotingGenerate(["create", "confirm", "broadcast"])

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",  # confirm + chained handoff → locked → CONFIRMED
            "  Retry  ",  # exact-utterance test: stripped + case-insensitive
            "broadcast it",
            "exit",
        ],
        ["create", "confirm", "broadcast"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER": "hwi"},
    )

    joined = "\n".join(outputs)
    assert f"Signed and verified ✓ txid {_flow_txid(flow)}." in joined
    assert flow.state is TxFlowStatus.BROADCAST
    # signtx ran twice: the failed chained handoff + the intercepted retry.
    assert commands.sign_calls == 2
    # The retry turn was model-free; the model saw send/confirm/broadcast
    # (3 prompts for 4 live utterances). Prompt-COUNT proves the bypass: a
    # CONFIRMED re-sign via the model would have needed a fourth call.
    assert len(fake.prompts) == 3


@pytest.mark.parametrize(
    ("lines_prefix", "plan"),
    [
        # IDLE: bare 'retry' must NOT be intercepted (no pending flow) —
        # the very first model call carries the utterance.
        (["retry"], ["respond"]),
        # CREATED: 'retry' is an ordinary utterance for the model.
        (
            [f"send 60000 sats to {SEND_RECIPIENT}", "retry"],
            ["create", "respond"],
        ),
    ],
    ids=["idle", "created"],
)
def test_retry_outside_confirmed_still_goes_through_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lines_prefix: list[str],
    plan: list[str],
) -> None:
    """States other than CONFIRMED are untouched by the interception: the
    utterance reaches the model through the normal generate → validate →
    dispatch pipeline (recorded prompt proves it)."""
    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    # Every model call answers with a canned respond (or the create); the
    # retry-at-CONFIRMED path would bypass the model entirely, so the
    # prompts below disprove interception here.
    fake = FactsQuotingGenerate(plan)

    _code, _outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [*lines_prefix, "exit"],
        [],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER": "hwi"},
    )

    assert any("user: retry" in p for p in fake.prompts)
    if len(lines_prefix) == 1:
        assert flow.state is TxFlowStatus.IDLE
    else:
        assert flow.state is TxFlowStatus.CREATED


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
    assert f"Sent! txid {_flow_txid(flow)} — tracking…" in joined
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
    assert f"broadcast_txid: {_flow_txid(flow)}" in fake.prompts[5]


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


def _file_sign_ready(
    table: dict[IntentName, Any],
    session: SendSession,
    *,
    signer_param: str | None,
) -> tuple[str, Any]:
    """Drive the send flow to CONFIRMED and run sign_tx once, returning
    ``(tx_ref, sign_result)`` — the sign turn carries an optional
    model-emitted ``signer`` param (TCK-HW-004 matrix)."""
    created = table[IntentName.CREATE_TX](validate_payload(_create_tx_envelope_json()))
    tx_ref = created["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    params: dict[str, Any] = {"tx_ref": tx_ref}
    if signer_param is not None:
        params["signer"] = signer_param
    sign_env = validate_payload(
        json.dumps({"v": 0, "intent": "sign_tx", "params": params})
    )
    return tx_ref, table[IntentName.SIGN_TX](sign_env)


def test_signer_config_is_authoritative_over_model_signer_param(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-HW-004: the app-configured signer (env) is AUTHORITATIVE. A
    model-emitted ``signer:"hwi"`` param never reroutes the airgap-vs-device
    choice: the configured FILE backend runs (export to
    ``LOCALWALLET_SIGNER_DIR`` with ADR-0014 naming + sidecar),
    ``HwiUsbSigner`` is never constructed, and the result carries a
    value-free guidance note naming the configured kind (never the model's)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    monkeypatch.setenv("LOCALWALLET_SIGNER", "file")
    monkeypatch.setenv("LOCALWALLET_SIGNER_DIR", str(transfer))

    constructed: list[tuple[Any, Any]] = []

    def spy(fp: Any, account_path: Any) -> Any:
        constructed.append((fp, account_path))
        raise AssertionError("HwiUsbSigner must not run when file is configured")

    monkeypatch.setattr(app_module, "HwiUsbSigner", spy)
    table, _store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
    )
    _tx_ref, result = _file_sign_ready(table, session, signer_param="hwi")

    assert result["error"] == "signed_file_missing"  # configured file ran
    assert constructed == []  # HwiUsbSigner never constructed
    assert flow.state is TxFlowStatus.CONFIRMED
    exported = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
    assert len(exported) == 1
    assert (transfer / (exported[0].name + ".sha256")).exists()  # ADR-0014 sidecar
    # Value-free guidance names the CONFIGURED kind, not the model's "hwi".
    assert result["guidance"] == "Using your configured signer (file)."
    assert "hwi" not in result["guidance"]
    client.close()
    _store.close()


def test_signer_param_matching_config_runs_file_without_guidance_noise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-HW-004: a model ``signer:"file"`` param that MATCHES the
    configured file kind runs the file branch with NO guidance note."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    monkeypatch.setenv("LOCALWALLET_SIGNER", "file")
    monkeypatch.setenv("LOCALWALLET_SIGNER_DIR", str(transfer))
    table, _store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
    )
    _tx_ref, result = _file_sign_ready(table, session, signer_param="file")

    assert result["error"] == "signed_file_missing"
    assert "guidance" not in result  # no noise when the param agrees
    assert flow.state is TxFlowStatus.CONFIRMED
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
def test_live_mainnet_balance_via_mempool_space() -> None:
    """Real-network integration: the Phase 1 path against mempool.space
    mainnet — lazy scan populates the store, balance reads it, and a
    new_address allocation derives a valid bc1 address at index 0."""
    zpub = os.environ.get("LOCALWALLET_E2E_ZPUB", "").strip()
    if not zpub:
        pytest.skip("LOCALWALLET_E2E_ZPUB not set")
    parsed = parse_wallet_key(zpub)
    descriptor = WalletDescriptor.from_key(zpub)
    client = EsploraClient()  # defaults: https://mempool.space/api
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
    assert isinstance(address, str) and address.startswith("bc1")


# --------------------------------------------- startup progress UX (TCK-UX-001)


def test_startup_scan_notice_before_scan_completion_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TCK-UX-001 re-scoped onto the non-blocking scan (TCK-SCAN-003): the
    pre-scan notice still precedes everything, the fetch runs on the chain
    WORKER and receives the strict zero-argument progress callback (two
    bare dots stream to stdout, newline-closed), and the completion
    narration follows on the ENGINE thread when the record set persists —
    AFTER the live prompt hint (the old blocking contract's inversion)."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    events: list[str] = []

    def fake_fetch(
        plan: object,
        client: object,
        *,
        progress_fn: Callable[[], None] | None = None,
    ) -> object:
        events.append("scan-start")
        assert progress_fn is not None  # strict zero-arg tick shape
        progress_fn()
        progress_fn()
        events.append("scan-end")
        return object()  # engine-side persist_scan is patched below

    monkeypatch.setattr(app_module.wallet_scan, "fetch_scan", fake_fetch)
    monkeypatch.setattr(
        app_module.wallet_scan,
        "persist_scan",
        lambda store, records: _make_scan_summary(truncated=False),
    )
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB],
        monkeypatch,
        lambda request: pytest.fail(f"unexpected chain call: {request.url}"),
        ["exit"],
        store_path=store_path,
        auto_scan=True,
    )

    assert code == 0
    assert events == ["scan-start", "scan-end"]
    notice_idx = outputs.index(app_module.SCAN_PROGRESS_NOTICE)
    type_idx = outputs.index("Type a message — 'exit' or Ctrl-D quits.")
    complete_idx = outputs.index(
        "Startup scan complete: 1 UTXOs · tip height 870000."
    )
    assert notice_idx < type_idx < complete_idx
    assert capsys.readouterr().out == "..\n"  # two bare ticks, newline-closed


def test_rescan_path_notice_before_scan_completion_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-UX-001 re-scoped (TCK-SCAN-003 item 5): ``--rescan`` RIDES THE
    SPLIT — plan with ``rebuild=True`` on the engine thread, the derive+fetch
    on the worker (immutable record set out), the persist + completion
    narration on the engine — with the same notice → hint → summary
    ordering as the ordinary startup scan."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    events: list[str] = []
    real_plan = app_module.wallet_scan.plan_scan

    def spy_plan(store: Store, wallet: object, *, gap_limit: int | None = None,
                 rebuild: bool = False):
        events.append(f"plan:{rebuild}")
        return real_plan(store, wallet, gap_limit=gap_limit, rebuild=rebuild)

    def fake_fetch(
        plan: object,
        client: object,
        *,
        progress_fn: Callable[[], None] | None = None,
    ) -> object:
        events.append("fetch")
        assert progress_fn is not None
        progress_fn()
        return object()

    monkeypatch.setattr(app_module.wallet_scan, "plan_scan", spy_plan)
    monkeypatch.setattr(app_module.wallet_scan, "fetch_scan", fake_fetch)
    monkeypatch.setattr(
        app_module.wallet_scan,
        "persist_scan",
        lambda store, records: _make_scan_summary(truncated=False),
    )
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", ZPUB, "--rescan"],
        monkeypatch,
        lambda request: pytest.fail(f"unexpected chain call: {request.url}"),
        ["exit"],
        store_path=store_path,
    )

    assert code == 0
    assert events == ["plan:True", "fetch"]  # the split, in order
    notice_idx = outputs.index(app_module.SCAN_PROGRESS_NOTICE)
    type_idx = outputs.index("Type a message — 'exit' or Ctrl-D quits.")
    complete_idx = next(
        i for i, line in enumerate(outputs) if line.startswith("Rescan complete:")
    )
    assert notice_idx < type_idx < complete_idx
    assert outputs[complete_idx] == (
        "Rescan complete: branch 0: scanned 3, max used 0, next index 1 · "
        "branch 1: scanned 2, max used -1, next index 0 · 1 UTXOs · "
        "tip height 870000"
    )


def test_prompt_is_live_while_startup_scan_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-SCAN-003 (ADR-0022 decision 1; re-scopes the TCK-UX-001
    "hint-last" pin, which the non-blocking startup scan supersedes): the
    notice and the "Type a message" hint print WHILE the startup scan is
    still fetching (held on the mock chain) — the prompt is live in well
    under a second; a turn taken during the scan answers from the cache
    with the tool-owned stale flag + value-free note (no second scan
    fires); the completion narration lands after the hint, once the
    engine persists the worker's records; the later turn reads the
    populated cache verbatim, fresh."""
    store_path = _store_path(tmp_path)
    wd = _preset_store(store_path)
    recorded: list[httpx.Request] = []
    addr0, addr1 = derive_fixture_addresses(2)
    inner = _scan_handler(
        recorded, utxos_by_addr={addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1}
    )
    release = threading.Event()

    def gating(request: httpx.Request) -> httpx.Response:
        # The scan cannot complete until the test releases it: any turn
        # taken before then is DETERMINISTICALLY mid-first-scan. The
        # balance turn's best-effort /v1/prices fetch (TCK-FIAT-001) is
        # NOT gated: parking it would stall the mid-scan turn for the
        # release timeout — it answers 404 (sats-only) immediately.
        if request.url.path.endswith("/v1/prices"):
            return inner(request)
        release.wait(10)
        return inner(request)

    lines = iter(["What's my balance?", "What's my balance?", "exit"])
    outputs: list[str] = []
    asked = {"n": 0}

    def read_line_gated(_prompt: str) -> str:
        line = next(lines)
        if line == "What's my balance?":
            asked["n"] += 1
            if asked["n"] == 2:
                # Second ask: let the scan finish, and wait for the engine's
                # completion narration before the turn runs.
                release.set()
                _wait_scan_narration(outputs)
        return line

    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "1")
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(gating))
    try:
        code = run(
            ["--stub-llm", "--zpub", ZPUB],
            input_fn=read_line_gated,
            output_fn=outputs.append,
        )
    finally:
        release.set()

    assert code == 0
    notice_idx = outputs.index(app_module.SCAN_PROGRESS_NOTICE)
    type_idx = outputs.index("Type a message — 'exit' or Ctrl-D quits.")
    complete_idx = next(
        i for i, line in enumerate(outputs) if line.startswith("Startup scan complete")
    )
    # Prompt-live contract: notice → hint → (scan still running) → completion.
    assert notice_idx < type_idx < complete_idx
    # The mid-scan balance: cache-served zeros + the value-free stale note,
    # both BEFORE the completion line (the tool owns the flag).
    first_balance_idx = next(
        i for i, line in enumerate(outputs) if line.startswith("Balance (mainnet):")
    )
    assert first_balance_idx < complete_idx
    assert "Balance (mainnet): 0 sats (confirmed) + 0 sats (unconfirmed)" in outputs
    note_idx = outputs.index(app_module.FRESHNESS_NOTE)
    assert first_balance_idx < note_idx < complete_idx
    # The post-scan balance: real values, no trailing stale note after it.
    assert (
        f"Balance (mainnet): {EXPECTED_CONFIRMED} sats (confirmed) "
        f"+ {EXPECTED_UNCONFIRMED} sats (unconfirmed)" in outputs
    )
    last_balance_idx = max(
        i for i, line in enumerate(outputs) if line.startswith("Balance (mainnet):")
    )
    assert last_balance_idx > complete_idx
    assert app_module.FRESHNESS_NOTE not in outputs[last_balance_idx:]
    # One scan (9 requests: 1 tip + 6 txs + 2 utxo, TCK-SCAN-001) — the
    # mid-scan turn did NOT trigger a second scan. The two balance turns'
    # display-only /v1/prices fetches are excluded (TCK-FIAT-001).
    assert len(_scan_requests(recorded)) == 9
    joined = "\n".join(outputs)
    assert ZPUB not in joined and wd.descriptor not in joined
    assert all(addr not in joined for addr in (addr0, addr1))
    with Store(store_path) as store:
        rows = store.list_wallets()
        assert len(rows) == 1
        assert store.get_sync_state(rows[0].id, "last_scan_cursor") is not None


def test_prompt_live_and_startup_failure_narrated_after_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TCK-SCAN-003 failure path (re-scopes the TCK-UX-001 failure pin):
    notice → "Type a message" (prompt live) → scrubbed warning on the
    engine thread when the worker's scan fails — the REPL still runs. A
    later balance turn then lazy-retries via the worker and surfaces the
    chain state honestly (gate lifted → lazy path, as ever)."""
    store_path = _store_path(tmp_path)
    _preset_store(store_path)
    # addr0 funded: the /utxo failure stays on the scan path (addresses
    # with empty history are never fetched, TCK-SCAN-001).
    handler = _scan_handler(
        [], utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]},
        utxo_status=503,
    )

    outputs: list[str] = []
    lines = iter(["What's my balance?", "exit"])

    def read_line(_prompt: str) -> str:
        line = next(lines)
        if line == "What's my balance?":
            # Deterministic: wait for the engine to narrate the startup
            # failure before the balance turn runs.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not any(
                ln.startswith("warning: startup scan failed") for ln in outputs
            ):
                time.sleep(0.005)
        return line

    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "1")
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(handler))

    code = run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=read_line,
        output_fn=outputs.append,
    )

    assert code == 0
    notice_idx = outputs.index(app_module.SCAN_PROGRESS_NOTICE)
    warning_idx = next(
        i for i, line in enumerate(outputs) if line.startswith("warning: startup scan failed")
    )
    type_idx = outputs.index("Type a message — 'exit' or Ctrl-D quits.")
    assert notice_idx < type_idx < warning_idx  # prompt live BEFORE the outcome
    joined = "\n".join(outputs)
    # The lazy retry after the failed startup scan: chain still down →
    # the honest chain-unavailable line, never a fabricated balance.
    assert "chain unavailable" in joined
    assert "Balance (mainnet):" not in joined


# ============================ TCK-UTXO-004: tag/consolidation card narration
#
# docs/ux-utxo-notes-design.md §4: the create_tx handler joins coin_labels
# onto the snapshot (dispatcher-side, plain booleans only — §4.1), resolves
# the three policy settings PER SELECTION, and the brief card renders the mix
# warning (§4.3, above the ask line) and the consolidation clause (on the
# From data line) from the FINAL selection — so a re-quote can never
# silently change the tag-mix (§4.2). All strings code-owned; nothing here
# is model-visible (the flags ride the RESULT dict → renderer only).

_MIX_WARNING_TEXT: Final[str] = (
    'Heads up: this mixes coins you marked KYC with coins you didn\'t — '
    'say "cancel" if that\'s not what you want.'
)


def _utxo(txid: str, value: int) -> dict[str, Any]:
    return {"txid": txid, "vout": 0, "value": value, "status": {"confirmed": True}}


def test_brief_card_mix_warning_sits_above_the_ask_line() -> None:
    """(a) The mandatory mixing warning: a dedicated conditional line,
    ABOVE the ask line (doc §4.3 slot table), verbatim code-owned copy —
    the honesty frame is "coins you marked" (the user's claim, our echo)."""
    lines: list[str] = []
    app_module._print_brief_card(
        {
            "recipient": "bc1qtest",
            "amount_sats": 100_000,
            "fee_sats": 831,
            "inputs_count": 3,
            "change_sats": 169,
            "mixed": True,
            "folded_count": 0,
        },
        lines.append,
    )
    assert lines[0] == _MIX_WARNING_TEXT == app_module._CARD_MIX_WARNING
    assert lines[1] == app_module._CARD_ASK_LINE
    # No jargon, no new gate vocabulary beyond the existing "cancel" (§4.4).
    assert "UTXO" not in lines[0] and "consolidat" not in lines[0].lower()


def test_brief_card_consolidation_clause_on_the_from_line() -> None:
    """(b) The fold clause rides the From data line (count only, doc §2.2):
    after the sources count and the change segment — never the tail slot."""
    lines: list[str] = []
    app_module._print_brief_card(
        {
            "recipient": "bc1qtest",
            "amount_sats": 110_000,
            "fee_sats": 165,
            "inputs_count": 3,
            "change_sats": 9_835,
            "mixed": False,
            "folded_count": 2,
        },
        lines.append,
    )
    assert not any(line.startswith("Heads up") for line in lines)
    assert lines[4] == (
        "From: your wallet (3 sources) · 9,835 sats come back as change"
        " · folding in 2 small ones now to save fees later"
    )


def test_brief_card_silent_when_flags_absent_or_zero() -> None:
    """Absent/zero flags render NOTHING — the pre-amendment card is
    byte-identical (and the pending re-show, whose flow record cannot know,
    degrades silent rather than guessing)."""
    lines: list[str] = []
    app_module._print_brief_card(
        {
            "recipient": "bc1qtest",
            "amount_sats": 1,
            "fee_sats": 2,
            "inputs_count": 1,
            "mixed": False,
            "folded_count": 0,
        },
        lines.append,
    )
    assert lines[0] == app_module._CARD_ASK_LINE  # no warning above it
    assert lines[4] == "From: your wallet (1 source)"
    assert "folding" not in "\n".join(lines)


def test_requote_renarrates_the_mix_warning_present_and_absent() -> None:
    """(c) FLOW-REQUOTE (§4.2): the new card describes the new final
    selection — a re-quote that changes the mix flips the line's presence on
    the rendered card (confirm is only valid against the card the user is
    reading); no separate warning/refusal is added."""
    base: dict[str, Any] = {
        "tx_ref": "r2",
        "amount_sats": 100_000,
        "recipient": "bc1qtest",
        "fee_sats": 627,
        "fee_rate_sat_vb": 3,
        "vsize": 209,
        "change_sats": None,
        "inputs_count": 2,
        "fee_target": "fast",
        "fee_target_defaulted": False,
        "fee_requote": True,
        "requote_direction": "faster",
        "expires_in_s": 600,
    }
    mixed_out: list[str] = []
    app_module._print_create_tx({**base, "mixed": True, "folded_count": 0}, mixed_out.append)
    assert mixed_out[0].startswith("Re-quoted at the faster rate")
    assert mixed_out[1] == app_module._CARD_MIX_WARNING  # re-narrated IN
    pure_out: list[str] = []
    app_module._print_create_tx({**base, "mixed": False, "folded_count": 0}, pure_out.append)
    assert pure_out[0].startswith("Re-quoted at the faster rate")
    assert pure_out[1] == app_module._CARD_ASK_LINE  # re-narrated OUT


def test_tag_aware_selection_flips_the_mix_on_requote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full handler ride: labeled kyc coins + one untagged coin. At the
    slow rung the pure kyc pool funds (no mix); the faster re-quote breaks
    the pure pool's finalization and the full-set fallback SPANS partitions —
    the result flags follow the final selection, and the label TEXT (tag and
    note) never rides any result field or rendered line (never model
    context, never output)."""
    addrs = derive_fixture_addresses(3)
    kyc_a, kyc_b, other = ("f" * 64, "a" * 64, "b" * 64)
    table, store, wallet, client, _rec, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec,
            utxos_by_addr={
                addrs[0]: [_utxo(kyc_a, 60_000)],
                addrs[1]: [_utxo(kyc_b, 40_500)],
                addrs[2]: [_utxo(other, 500)],
            },
        )
    )
    try:
        store.set_coin_label(wallet.id, kyc_a, 0, ["kyc"], "alice refund zebra")
        store.set_coin_label(wallet.id, kyc_b, 0, ["exchange"])

        def envelope(target: str) -> Envelope:
            return validate_payload(
                json.dumps(
                    {
                        "v": 0,
                        "intent": "create_tx",
                        "params": {
                            "recipient": SEND_RECIPIENT,
                            "amount_sats": 100_000,
                            "fee_target": target,
                        },
                    }
                )
            )

        slow = table[IntentName.CREATE_TX](envelope("slow"))  # 1 sat/vB
        assert slow.get("error") is None, slow
        assert slow["mixed"] is False  # the pure kyc pool funded
        assert slow["folded_count"] == 0
        fast = table[IntentName.CREATE_TX](envelope("fast"))  # 3 sat/vB
        assert fast.get("error") is None, fast
        assert fast["fee_requote"] is True and fast["requote_direction"] == "faster"
        assert fast["mixed"] is True  # no pure pool funds; the fallback spans
        assert fast["inputs_count"] == 3

        slow_card: list[str] = []
        app_module._print_brief_card(slow, slow_card.append)
        fast_card: list[str] = []
        app_module._print_brief_card(fast, fast_card.append)
        assert slow_card[0] == app_module._CARD_ASK_LINE
        assert fast_card[0] == app_module._CARD_MIX_WARNING

        # Cardinal rule (§1.1): label text is display-frozen to /details —
        # not in the narration, not in the handler result, not in FACTS.
        joined = "\n".join([*slow_card, *fast_card])
        assert "zebra" not in joined and "alice refund" not in joined
        assert "zebra" not in json.dumps(slow) and "zebra" not in json.dumps(fast)
        assert "kyc" not in json.dumps(_flow_facts_of(flow))
    finally:
        client.close()
        store.close()


def _flow_facts_of(flow: Any) -> dict[str, object]:
    """The exact FACTS dict a next turn would inject (CREATED → pending
    facts) — the negative pin: mix/label material has no path to the model."""
    return app_module._flow_facts(flow)


def test_low_fee_consolidation_narrates_the_fold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(b) full ride: at the slow rung the two below-target coins fold into
    the funding selection (step 5, store-set target min), the result counts
    them, and the From line narrates the count — no mix, no warning."""
    addrs = derive_fixture_addresses(3)
    table, store, _wallet, client, _rec, _flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec,
            utxos_by_addr={
                addrs[0]: [_utxo("a" * 64, 5_000), _utxo("b" * 64, 5_000)],
                addrs[1]: [_utxo("c" * 64, 120_000)],
                addrs[2]: [_utxo("d" * 64, 90_000)],
            },
        )
    )
    try:
        # Stored rung only — no restart, no env: the settings resolve PER
        # SELECTION (why the entries honestly say requires_restart False).
        store.set_coin_setting("utxo_target_min_sats", "50000")
        created = table[IntentName.CREATE_TX](
            validate_payload(
                json.dumps(
                    {
                        "v": 0,
                        "intent": "create_tx",
                        "params": {
                            "recipient": SEND_RECIPIENT,
                            "amount_sats": 110_000,
                            "fee_target": "slow",
                        },
                    }
                )
            )
        )
        assert created.get("error") is None, created
        assert created["mixed"] is False
        assert created["folded_count"] == 2  # the two 5,000-sat coins
        assert created["inputs_count"] == 3  # 120k funds + 2 folded
        assert created["change_sats"] is not None
        lines: list[str] = []
        app_module._print_brief_card(created, lines.append)
        assert lines[0] == app_module._CARD_ASK_LINE  # no warning
        assert lines[4].endswith(" · folding in 2 small ones now to save fees later")
        assert not any("consolidat" in line.lower() or "utxo" in line.lower() for line in lines)
    finally:
        client.close()
        store.close()


def test_malformed_selection_settings_refuse_the_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed env rung (min above the default max) is caught fail-closed
    at selection time too (the re-check inside resolve): the refusal names
    the keys and the rule — VALUE-FREE, nothing stages."""
    monkeypatch.setenv("LOCALWALLET_UTXO_TARGET_MIN_SATS", "15000000")  # > 10M default max
    addrs = derive_fixture_addresses(1)
    table, store, _wallet, client, _rec, flow, _session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={addrs[0]: [SEND_UTXO]}
        )
    )
    try:
        result = table[IntentName.CREATE_TX](
            validate_payload(
                json.dumps(
                    {
                        "v": 0,
                        "intent": "create_tx",
                        "params": {
                            "recipient": SEND_RECIPIENT,
                            "amount_sats": SEND_AMOUNT_SATS,
                        },
                    }
                )
            )
        )
        assert result["error"] == "selection_failed"
        assert "utxo_target_min_sats" in str(result["detail"])
        assert "15000000" not in json.dumps(result)  # value-free
        assert flow.pending is None
    finally:
        client.close()
        store.close()
