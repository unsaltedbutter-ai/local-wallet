"""End-to-end skeleton tests (TCK-P0-006).

Full pipeline WITHOUT network or model:

    user text → AgentLoop (stub generate_fn) → handle_raw validation →
    allowlist dispatch → get_balance handler → EsploraClient over
    httpx.MockTransport (fixture UTXOs) → result dict → CLI printing.

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

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import localwallet.app as app_module
from localwallet.agent.loop import AgentLoop, AgentTurnStatus
from localwallet.app import (
    DEFAULT_SCAN_COUNT,
    PRIVACY_INDICATOR,
    ZPUB_ENV_VAR,
    build_dispatch_table,
    run,
    stub_generate,
)
from localwallet.chain import EsploraClient
from localwallet.chain import esplora as esplora_module
from localwallet.protocol import Envelope, IntentName, validate_payload
from localwallet.wallet.zpub_stub import (
    ParsedKey,
    WatchKeyError,
    derive_receive_addresses,
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

# Fixture UTXOs served by the MockTransport chain.
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

GET_BALANCE_JSON: Final[str] = '{"v": 0, "intent": "get_balance", "params": {}}'
GARBAGE: Final[str] = "this is not json at all <<<>>>"


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


def derive_fixture_addresses(count: int = DEFAULT_SCAN_COUNT) -> list[str]:
    """Addresses for the fixture wallet through the module under test."""
    return derive_receive_addresses(parse_watch_key(VPUB), count)


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


def _utxo_handler(
    utxos_by_addr: dict[str, list[dict[str, Any]]],
    recorded: list[httpx.Request],
    *,
    tip: int | list[dict[str, Any]] = TIP_HEIGHT,
    utxo_status: int = 200,
    tip_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """MockTransport handler serving fixture UTXOs and a tip height."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        path = request.url.path
        if path.endswith("/blocks/tip"):
            return httpx.Response(tip_status, json=tip)
        if path.endswith("/utxo"):
            for addr, payload in utxos_by_addr.items():
                if path.endswith(f"/address/{addr}/utxo"):
                    return httpx.Response(200, json=payload)
            # Unlisted address (or a forced failure mode): serve utxo_status.
            return httpx.Response(utxo_status, json=[] if utxo_status == 200 else None)
        return httpx.Response(200, json=[])

    return handler


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
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[dict[IntentName, Any], list[str]]:
    """Dispatch table + fixture addresses wired to a mock-transport client."""
    addresses = derive_fixture_addresses()
    client = _mock_client(handler)
    table = build_dispatch_table(client, addresses, client.get_tip_height)
    return table, addresses


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
    assert len(addresses) == DEFAULT_SCAN_COUNT


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
    assert "vpub/tpub" in message
    assert MAINNET_ZPUB not in message  # key never echoed


def test_testnet_keys_pass_the_gate() -> None:
    for key in (VPUB, UPUB, TPUB):
        addresses = derive_receive_addresses(parse_watch_key(key), count=2)
        assert len(addresses) == 2


# ------------------------------------------------------- stub model routing


def test_stub_generate_emits_get_balance_for_balance_input() -> None:
    prompt = "SYSTEM...\n\nuser: What's my balance?\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, None))
    assert envelope.intent is IntentName.GET_BALANCE


def test_stub_generate_emits_respond_otherwise() -> None:
    prompt = "SYSTEM...\n\nuser: hello there\n\nenvelope:"
    envelope = validate_payload(stub_generate(prompt, "root ::= ..."))
    assert envelope.intent is IntentName.RESPOND
    assert isinstance(envelope.params.text, str) and envelope.params.text.strip()


# ------------------------------------------------------- e2e: loop → chain


def test_balance_end_to_end_agent_to_dispatcher_to_chain() -> None:
    """'What's my balance?' flows: stub model → envelope validation →
    allowlist dispatch → get_balance handler → mock chain → totals."""
    recorded: list[httpx.Request] = []
    addresses = derive_fixture_addresses()
    handler = _utxo_handler(
        {addresses[0]: UTXOS_ADDR0, addresses[1]: UTXOS_ADDR1}, recorded
    )
    client = _mock_client(handler)
    table = build_dispatch_table(client, addresses, client.get_tip_height)

    gen = ScriptedGenerate([GET_BALANCE_JSON])
    loop = AgentLoop(gen, table)
    turn = loop.run("What's my balance?", {})

    assert turn.status is AgentTurnStatus.OK
    assert turn.envelope is not None
    assert turn.envelope.intent is IntentName.GET_BALANCE
    assert turn.turns_used == 1
    assert turn.result == {
        "confirmed_sats": EXPECTED_CONFIRMED,
        "unconfirmed_sats": EXPECTED_UNCONFIRMED,
        "total_sats": EXPECTED_TOTAL,
        "addresses_scanned": 5,
        "tip_height": TIP_HEIGHT,
    }
    # The handler really hit the chain adapter: one UTXO request per
    # derived address (sequential) plus one tip request.
    utxo_paths = [r.url.path for r in recorded if r.url.path.endswith("/utxo")]
    assert utxo_paths == [
        f"/testnet4/api/address/{addr}/utxo" for addr in addresses
    ]
    assert any(r.url.path.endswith("/blocks/tip") for r in recorded)
    # The stub model received the real envelope grammar via the seam.
    assert "root ::=" in (gen.calls[0][1] or "")


def test_respond_intent_passthrough_through_the_real_table() -> None:
    recorded: list[httpx.Request] = []
    table, _ = _build_table(_utxo_handler({}, recorded))
    respond_json = '{"v": 0, "intent": "respond", "params": {"text": "Hello!"}}'
    turn = AgentLoop(ScriptedGenerate([respond_json]), table).run("hi", {})
    assert turn.status is AgentTurnStatus.OK
    assert turn.result == {"text": "Hello!"}
    assert recorded == []  # no chain I/O for a respond turn


def test_clarify_intent_passthrough_through_the_real_table() -> None:
    recorded: list[httpx.Request] = []
    table, _ = _build_table(_utxo_handler({}, recorded))
    clarify_json = '{"v": 0, "intent": "clarify", "params": {"question": "How much?"}}'
    turn = AgentLoop(ScriptedGenerate([clarify_json]), table).run("send", {})
    assert turn.status is AgentTurnStatus.OK
    assert turn.result == {"question": "How much?"}
    assert turn.user_message == "How much?"  # model-emitted clarify surfaces
    assert recorded == []


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
    table, _ = _build_table(_utxo_handler({}, recorded))
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


# ------------------------------------------------------- chain-error path


def test_handler_chain_error_surfaces_as_result_without_raising() -> None:
    recorded: list[httpx.Request] = []
    addresses = derive_fixture_addresses()
    handler = _utxo_handler(
        {addresses[0]: UTXOS_ADDR0}, recorded, utxo_status=500
    )
    client = _mock_client(handler, max_retries=0)  # 500 fails immediately
    table = build_dispatch_table(client, addresses, client.get_tip_height)

    envelope: Envelope = validate_payload(GET_BALANCE_JSON)
    result = table[IntentName.GET_BALANCE](envelope)  # direct call: no raise

    assert result["error"] == "chain_unavailable"
    detail = str(result["detail"])
    assert detail.strip() != ""
    # Scrubbing invariant: no address material in the surfaced detail.
    assert all(addr not in detail for addr in addresses)
    assert set(result.keys()) == {"error", "detail"}


def test_chain_error_flows_through_loop_as_ok_with_error_result() -> None:
    addresses = derive_fixture_addresses()
    fail_client = _mock_client(_utxo_handler({}, [], utxo_status=503), max_retries=0)
    table = build_dispatch_table(fail_client, addresses, fail_client.get_tip_height)

    turn = AgentLoop(ScriptedGenerate([GET_BALANCE_JSON]), table).run(
        "What's my balance?", {}
    )
    assert turn.status is AgentTurnStatus.OK  # handler contained the failure
    assert turn.result is not None
    assert turn.result["error"] == "chain_unavailable"


def test_tip_failure_omits_tip_height_but_keeps_balance() -> None:
    """Tip lookup failure is non-fatal: balance totals are still returned,
    with the ``tip_height`` key omitted entirely (status OK)."""
    recorded: list[httpx.Request] = []
    addresses = derive_fixture_addresses()
    handler = _utxo_handler(
        {addresses[0]: UTXOS_ADDR0, addresses[1]: UTXOS_ADDR1},
        recorded,
        tip_status=500,
    )
    client = _mock_client(handler, max_retries=0)  # 5xx fails immediately
    table = build_dispatch_table(client, addresses, client.get_tip_height)

    turn = AgentLoop(ScriptedGenerate([GET_BALANCE_JSON]), table).run(
        "What's my balance?", {}
    )
    assert turn.status is AgentTurnStatus.OK
    assert turn.result is not None
    assert "error" not in turn.result
    assert "tip_height" not in turn.result
    assert turn.result["confirmed_sats"] == EXPECTED_CONFIRMED
    assert turn.result["unconfirmed_sats"] == EXPECTED_UNCONFIRMED
    assert turn.result["total_sats"] == EXPECTED_TOTAL
    assert turn.result["addresses_scanned"] == len(addresses)


def test_tip_list_shape_yields_correct_tip_height() -> None:
    """A list-shaped tip (mempool.space divergence) yields the max height."""
    recorded: list[httpx.Request] = []
    addresses = derive_fixture_addresses()
    handler = _utxo_handler(
        {addresses[0]: UTXOS_ADDR0, addresses[1]: UTXOS_ADDR1},
        recorded,
        tip=[{"height": 100}, {"height": 870_000}],
    )
    client = _mock_client(handler)
    table = build_dispatch_table(client, addresses, client.get_tip_height)

    envelope: Envelope = validate_payload(GET_BALANCE_JSON)
    result = table[IntentName.GET_BALANCE](envelope)
    assert result["tip_height"] == 870_000
    assert result["total_sats"] == EXPECTED_TOTAL


# --------------------------------------------------------------- CLI wiring


def _run_captured(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    lines: list[str],
) -> tuple[int, list[str]]:
    """Run app.run() with stub I/O and a mock-transport chain client."""
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        app_module, "EsploraClient", lambda **_: _mock_client(handler)
    )
    inputs = iter(lines)

    def read_line(_prompt: str) -> str:
        return next(inputs)

    outputs: list[str] = []
    code = run(argv, input_fn=read_line, output_fn=outputs.append)
    return code, outputs


def test_repl_end_to_end_with_stub_llm_prints_verbatim_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[httpx.Request] = []
    addresses = derive_fixture_addresses()
    handler = _utxo_handler(
        {addresses[0]: UTXOS_ADDR0, addresses[1]: UTXOS_ADDR1}, recorded
    )

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Banner: testnet notice + §9 privacy indicator verbatim.
    assert "TESTNET" in joined
    assert PRIVACY_INDICATOR in joined
    # Balance line verbatim from the handler result dict.
    assert (
        f"Balance (testnet): {EXPECTED_CONFIRMED} sats (confirmed) "
        f"+ {EXPECTED_UNCONFIRMED} sats (unconfirmed)" in joined
    )
    assert f"Total {EXPECTED_TOTAL} sats" in joined
    assert f"tip height {TIP_HEIGHT}" in joined
    # Privacy/secret hygiene: the zpub and addresses are never echoed.
    assert VPUB not in joined
    assert all(addr not in joined for addr in addresses)
    assert len(recorded) == 6  # 5 utxo scans + 1 tip


def test_repl_reads_zpub_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ZPUB_ENV_VAR, VPUB)
    recorded: list[httpx.Request] = []
    addresses = derive_fixture_addresses()
    handler = _utxo_handler(
        {addresses[0]: UTXOS_ADDR0, addresses[1]: UTXOS_ADDR1}, recorded
    )

    code, outputs = _run_captured(
        ["--stub-llm"], monkeypatch, handler, ["What's my balance?", "quit"]
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert f"Balance (testnet): {EXPECTED_CONFIRMED} sats" in joined
    assert VPUB not in joined  # env-sourced key never echoed either


def test_zpub_cli_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Env holds a key the Phase 0 gate would refuse; the CLI arg must win.
    monkeypatch.setenv(ZPUB_ENV_VAR, MAINNET_ZPUB)
    recorded: list[httpx.Request] = []
    handler = _utxo_handler({}, recorded)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["exit"],
    )

    assert code == 0
    assert PRIVACY_INDICATOR in "\n".join(outputs)


def test_repl_reports_chain_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esplora_module, "_sleep_for", lambda _s: None)
    handler = _utxo_handler({}, [], utxo_status=503)

    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", VPUB],
        monkeypatch,
        handler,
        ["What's my balance?", "exit"],
    )

    assert code == 0
    joined = "\n".join(outputs)
    assert "chain unavailable" in joined
    assert "Balance (testnet):" not in joined


def test_repl_refuses_mainnet_zpub_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, outputs = _run_captured(
        ["--stub-llm", "--zpub", MAINNET_ZPUB], monkeypatch, lambda _req: None, []
    )
    # The client factory is never reached (key parse fails first) — the
    # dummy handler above would fail loudly if it were.
    assert code == 2
    joined = "\n".join(outputs)
    assert "Phase 0 is testnet-only" in joined
    assert MAINNET_ZPUB not in joined


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
    handler = _utxo_handler({}, recorded)
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_: _mock_client(handler))

    outputs: list[str] = []
    code = run(["--zpub", VPUB], input_fn=lambda _p: "exit", output_fn=outputs.append)

    assert code == 2
    joined = "\n".join(outputs)
    assert "No model configured" in joined
    assert "LOCALWALLET_MODEL_PATH" in joined
    assert "--stub-llm" in joined


# ------------------------------------------------- live-network integration


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("LOCALWALLET_E2E_LIVE") != "1",
    reason="live-network test: set LOCALWALLET_E2E_LIVE=1 to include",
)
def test_live_testnet4_balance_via_mempool_space() -> None:
    """Real-network integration: runs the full loop against
    mempool.space testnet4 with a vpub from LOCALWALLET_E2E_VPUB."""
    vpub = os.environ.get("LOCALWALLET_E2E_VPUB", "").strip()
    if not vpub:
        pytest.skip("LOCALWALLET_E2E_VPUB not set")
    addresses = derive_receive_addresses(parse_watch_key(vpub), DEFAULT_SCAN_COUNT)
    client = EsploraClient()  # defaults: https://mempool.space/testnet4/api
    try:
        table = build_dispatch_table(client, addresses, client.get_tip_height)
        loop = AgentLoop(stub_generate, table)
        turn = loop.run("What's my balance?", {})
    finally:
        client.close()

    assert turn.status is AgentTurnStatus.OK
    assert turn.result is not None
    result = turn.result
    if result.get("error") == "chain_unavailable":
        pytest.fail(f"live chain query failed: {result.get('detail')}")
    assert isinstance(result["confirmed_sats"], int) and result["confirmed_sats"] >= 0
    assert isinstance(result["unconfirmed_sats"], int) and result["unconfirmed_sats"] >= 0
    assert result["total_sats"] == result["confirmed_sats"] + result["unconfirmed_sats"]
    assert result["addresses_scanned"] == DEFAULT_SCAN_COUNT
    assert isinstance(result["tip_height"], int) and result["tip_height"] > 0
