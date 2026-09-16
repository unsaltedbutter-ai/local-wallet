"""TCK-CHAT-006 — network-status answers + explorer links (engine half).

The deterministic pre-model intercepts (FIAT-003/CONS-002/CFG-004 pattern)
behind "what are fees like right now", "block height", "open mempool for
<txid>", "explorer for <address>" and "show me the mempool":

* fees-now / block height = ENGINE-computed facts (the SHARED estimator's
  ONE cached snapshot + the store's scan-owned tip + the tool-owned UTC
  clock), quoted verbatim into the narration; zero chain requests beyond
  the snapshot refresh the card paths already pay; absence is honest
  (no fabricated tip, no fabricated rate);
* explorer links are CODE-CONSTRUCTED from shape-validated tokens only
  (64-hex txid either case, embit-decoded mainnet bech32), the closed
  link shape ``[{"label", "url"}]`` under the code-owned
  :data:`app.EXPLORER_ROOT_URL`; a malformed token is refused value-free
  (never echoed) and the turn is consumed;
* routing: the matchers never steal the bump/settings/history/gate
  surfaces, and the registry stays 15 (no new intent — goldens 073..077
  pin the model-side fallback: the prompt's ownership block makes an
  un-intercepted phrasing a clarify, never a fabricated number or URL).

Rides the CONS-001/002 harness (production dispatch table, mock chain,
fake-device signer — no network, deterministic).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from localwallet import app
from localwallet.protocol import EnvelopeValidationError, IntentName, validate_payload
from localwallet.tx.flow import TxFlowStatus
from tests.test_chat002_consolidate import _blocks
from tests.test_cons001_conversation import _labeled, _turn
from tests.test_cons001_conversation import (
    world as _cons_world,  # noqa: F401 — the CONS-001 harness fixture, injected by name
)
from tests.test_cons002_timing import _ask, _fee_gets, _StubEst

TXID: str = "ab" * 32
TXID_UPPER: str = "AB" * 32
ADDRESS: str = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
NOW: int = 1_757_000_000  # fixed clock → byte-exact stamp pins
STAMP: str = app._status_stamp(NOW)
TIP: int = 900_123


@pytest.fixture()
def world(_cons_world):  # noqa: F811 — fixture alias, not a shadowing arg
    _labeled(_cons_world)
    _cons_world["store"].set_sync_state(
        _cons_world["wallet"].id, app.wallet_scan.TIP_KEY, str(TIP)
    )
    yield _cons_world


def _direct(world, line: str, *, estimator: Any = None) -> tuple[bool, list[str]]:
    """Call the intercept directly (fixed clock) — returns (consumed, lines)."""
    outs: list[str] = []
    consumed = app._run_network_status_turn(
        world["store"], line, outs.append, fee_estimator=estimator, now=NOW
    )
    return consumed, outs


# =========================================================================
# 1. fees-now — the FACTS answer (verbatim figures, honest absence)
# =========================================================================


def _est() -> _StubEst:
    # medium 1150c → "11.5", fast 2300c → "23", slow 800c → "8", avg 500c → "5"
    return _StubEst({"slow": 800, "medium": 1150, "fast": 2300}, 500)


def test_fees_now_quotes_facts_verbatim(world) -> None:
    consumed, outs = _direct(world, "what are fees like right now?", estimator=_est())
    assert consumed is True
    assert outs[0] == (
        f"Network status · {STAMP} · chain tip (last scan): block {TIP}."
    )
    assert outs[1] == (
        "Estimated bids · next block: 11.5 sat/vB · faster: 23 sat/vB "
        "· a block back: 8 sat/vB."
    )
    assert outs[2] == "Six-hour average of the per-block lowest fees: 5 sat/vB."
    assert outs[3] == app._FEES_NOW_HEDGE


def test_fees_now_no_avg_omits_the_line(world) -> None:
    """No per-block data (recommended fallback / native snapshot): the
    average sentence is OMITTED — never padded with a guess."""
    _, outs = _direct(
        world, "what are fees like right now", estimator=_StubEst(
            {"slow": 800, "medium": 1150, "fast": 2300}, None
        )
    )
    assert not any("Six-hour" in ln for ln in outs)
    assert any("11.5 sat/vB" in ln for ln in outs)


def test_fees_now_broken_source_fails_closed(world) -> None:
    """A fee-source failure answers the honest no-data line under the
    still-available status head — no rate is ever guessed, no crash."""
    _, outs = _direct(
        world,
        "check the fees",
        estimator=_StubEst({"slow": 0, "medium": 0, "fast": 0}, None, raise_exc=True),
    )
    assert any(ln == app._FEES_NOW_NO_DATA for ln in outs)
    assert not any("sat/vB" in ln for ln in outs)


def test_fees_now_missing_tip_is_honest(world) -> None:
    """No stored tip: the head SAYS so (never a fabricated block 0) while
    the fee figures still answer — the two facts are independent."""
    world["store"].set_sync_state(world["wallet"].id, app.wallet_scan.TIP_KEY, "junk")
    consumed, outs = _direct(world, "are fees high?", estimator=_est())
    assert consumed is True
    assert outs[0] == app._FEES_NOW_NO_TIP.format(stamp=STAMP)
    assert "block 0" not in outs[0]
    assert any("11.5 sat/vB" in ln for ln in outs)


def test_fees_now_real_turn_consumed_and_free(world) -> None:
    """End-to-end through the production ``_run_turn`` chain with a REAL
    FeeEstimator over the mock public client: the turn is consumed — the
    model never sees it, no flow is touched, nothing enters the
    transcript."""
    world["state"]["mempool_blocks"] = _blocks([10.0, 8.0, 5.0, 4.0, 3.0, 2.0])
    outs, fake, loop = _ask(world, "what are fees like right now?")
    assert fake.prompts == [] and loop.history == ()
    assert world["flow"].state is TxFlowStatus.IDLE
    assert any(
        ln.startswith("Network status \u00b7 ") and f"chain tip (last scan): block {TIP}" in ln
        for ln in outs
    )
    assert any("sat/vB" in ln for ln in outs)


def test_status_costs_zero_extra_chain_calls(world) -> None:
    """FETCH DISCIPLINE (pinned): the first fees answer pays the SHARED
    estimator's ONE regular refresh (the recommended + mempool-blocks
    pair every card already pays); every later status answer inside the
    TTL costs ZERO requests — and NO wallet-path call (no tip RPC) ever
    rides a status turn."""
    world["state"]["mempool_blocks"] = _blocks([2.0] * 6)
    _ask(world, "not-a-status-line")  # warm-up turn (model answers; nothing fetched)
    base = len(world["recorded"])
    _ask(world, "what are fees like right now?")
    assert len(_fee_gets(world)) == 2  # exactly ONE snapshot refresh
    after = len(world["recorded"])
    _ask(world, "what are fees like right now?")
    _ask(world, "block height")
    assert len(_fee_gets(world)) == 2  # cache serves, zero fee fetches
    assert len(world["recorded"]) == after  # zero requests of ANY kind
    assert all("/blocks/tip" not in r.url.path for r in world["recorded"][base:])


# =========================================================================
# 2. block height — cached tip + tool clock, no fetch
# =========================================================================


def test_height_answer_verbatim(world) -> None:
    consumed, outs = _direct(world, "what is the current block height?")
    assert consumed is True
    assert outs == [
        app._BLOCK_HEIGHT_TIP.format(tip=TIP, stamp=STAMP),
    ]
    assert f"block {TIP}" in outs[0] and STAMP in outs[0]


def test_height_no_tip_honest(world) -> None:
    world["store"].set_sync_state(world["wallet"].id, app.wallet_scan.TIP_KEY, "")
    consumed, outs = _direct(world, "block height")
    assert consumed is True
    assert outs == [app._BLOCK_HEIGHT_NO_TIP]
    assert not any(ch.isdigit() for ln in outs for ch in ln)


def test_height_works_without_estimator_real_turn(world) -> None:
    """Height rides the store cache ONLY — no estimator wired, still
    consumed model-free; and it costs zero chain requests."""
    before = len(world["recorded"])
    outs, fake, _ = _turn(world, "block height")
    assert fake.prompts == []
    # stamp-free (the clock-locked template pin is the _direct test):
    assert len(outs) == 1 and outs[0].startswith(
        f"Chain tip (last scan): block {TIP} · answered "
    )
    assert len(world["recorded"]) == before  # zero requests


# =========================================================================
# 3. explorer links — constructed from validated tokens, never raw text
# =========================================================================


def test_txid_link(world) -> None:
    consumed, outs = _direct(world, f"open mempool for {TXID}")
    assert consumed is True
    assert outs == [
        app._EXPLORER_HEAD,
        f"Transaction: {app.EXPLORER_ROOT_URL}/tx/{TXID}",
    ]


def test_txid_uppercase_normalized(world) -> None:
    _, outs = _direct(world, f"open mempool for {TXID_UPPER}")
    assert outs[1] == f"Transaction: {app.EXPLORER_ROOT_URL}/tx/{TXID}"


def test_address_link(world) -> None:
    _, outs = _direct(world, f"explorer for {ADDRESS}")
    assert outs[1] == f"Address: {app.EXPLORER_ROOT_URL}/address/{ADDRESS}"


def test_two_links_in_word_order(world) -> None:
    links = app._explorer_request(f"open the explorer for {TXID} and {ADDRESS}")
    assert isinstance(links, list) and len(links) == 2
    assert [ln["label"] for ln in links] == ["Transaction", "Address"]


def test_link_shape_is_closed(world) -> None:
    links = app._explorer_request(f"mempool {TXID} {ADDRESS}")
    assert isinstance(links, list)
    for link in links:
        assert set(link) == {"label", "url"}
        assert link["url"].startswith(app.EXPLORER_ROOT_URL)
        assert link["label"] in {"Transaction", "Address", "Mempool"}


def test_root_link(world) -> None:
    consumed, outs = _direct(world, "show me the mempool")
    assert consumed is True
    assert outs == [app._EXPLORER_HEAD, f"Mempool: {app.EXPLORER_ROOT_URL}"]


def _break_checksum(addr: str) -> str:
    """Same-length bech32 with a corrupted DATA character (last-6 are the
    checksum; mutate one position before it) — shape-valid, decode-dead."""
    pos = len(addr) - 7
    repl = "q" if addr[pos] != "q" else "p"
    return addr[:pos] + repl + addr[pos + 1 :]


@pytest.mark.parametrize(
    "bad",
    [
        "a" * 63,  # hex, too short for a txid
        "a" * 65,  # hex, too long
        _break_checksum(ADDRESS),  # bech32-shaped, checksum-dead
        "Bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",  # mixed case
    ],
)
def test_malformed_token_refused_not_echoed(world, bad: str) -> None:
    """A token-SHAPED word that fails validation is a value-free refusal
    (the bad token NEVER rides the reply) and the turn is CONSUMED — a
    half-parsed explorer ask must not reach the model either (LABEL-001)."""
    consumed, outs = _direct(world, f"open mempool for {bad}")
    assert consumed is True
    assert outs == [app._EXPLORER_BAD_TOKEN]
    assert bad not in outs[0]


def test_token_never_raw_in_url(world) -> None:
    """The URL is BUILT from the code constant + the normalized token:
    nothing else from the user's line can ride it — a non-token word
    earns the ROOT form (a shaped verb line) or nothing, never a path
    built from arbitrary text."""
    consumed, outs = _direct(world, "open mempool for evil.tld/x?a=1")
    assert consumed is True
    assert all("/tx/" not in ln and "/address/" not in ln for ln in outs)
    assert outs == [app._EXPLORER_HEAD, f"Mempool: {app.EXPLORER_ROOT_URL}"]


def test_explorer_needs_no_wiring(world) -> None:
    """Pure-code family: store None (pre-provisioning CLI) still answers
    the link asks — and the status asks STAND DOWN without their sources."""
    outs: list[str] = []
    assert app._run_network_status_turn(None, "show me the mempool", outs.append) is True
    assert app._run_network_status_turn(None, "block height", [].append) is False
    assert app._run_network_status_turn(world["store"], "block height", [].append,
                                        fee_estimator=None, now=NOW) is True
    assert app._run_network_status_turn(world["store"], "what are fees like right now",
                                        [].append, fee_estimator=None, now=NOW) is False


@pytest.mark.parametrize(
    "line",
    [
        "is my transaction in the mempool?",   # tx_status territory (referent words)
        "what is a mempool?",                  # knowledge ask — the model's to answer
        "show my addresses",                   # get_addresses keeps its route
        "don't open the explorer",             # deny token suppresses
        "explorer for my kyc coins",           # referent words + no token
        "open mempool for " + TXID + " and don't sign",  # deny suppresses the whole family
    ],
)
def test_explorer_does_not_steal(world, line: str) -> None:
    assert app._explorer_request(line) is None
    consumed, _ = _direct(world, line, estimator=_est())
    assert consumed is False


# =========================================================================
# 4. Routing — the matchers keep every neighbouring surface theirs
# =========================================================================


@pytest.mark.parametrize(
    "line",
    [
        "what are fees like right now",
        "how much are the fees?",
        "are fees high?",
        "fees?",
        "check current fees",
        "what's the fee situation",  # "fee" + question starter
    ],
)
def test_fees_matcher_positive(line: str) -> None:
    assert app._fees_now_ask(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "increase the fee on " + TXID,     # the bump_fee route
        "what is the consolidation fee ceiling?",  # CFG-004's key
        "my fees this month",              # a history question
        "set the fee to 5 sat/vB",         # a create_tx knob
        "don't worry about fees",          # deny token
        "send 50000 sats to " + ADDRESS,   # not a fee ask at all
        "fees labeled kyc",                # the label route
        "the fees were too high",          # a statement, not a question
    ],
)
def test_fees_matcher_negative(line: str) -> None:
    assert app._fees_now_ask(line) is False


@pytest.mark.parametrize(
    "line",
    ["block height", "what's the current block height?", "how many blocks are there?",
     "what is the tip?", "chain height", "the tip"],
)
def test_height_matcher_positive(line: str) -> None:
    assert app._block_height_ask(line) is True


@pytest.mark.parametrize(
    "line",
    ["did block 900000 confirm?", "confirmations at height of " + TXID,
     "orphan the block?", "don't tell me the block height",
     "how many blocks until my tx confirms", "tip: label my coin as 'kyc'"],
)
def test_height_matcher_negative(line: str) -> None:
    assert app._block_height_ask(line) is False


def test_never_trap_composition(world) -> None:
    """The never-trap invariant COMPOSES with this intercept: a status
    line while a consolidation ask stands open closes the ask (fall
    through) and is then ANSWERED by the next rung down — the user is
    never trapped and the utterance is never dead."""
    _turn(world, "consolidate my coins")
    assert world["session"].cons_ask is not None
    outs, fake, _ = _turn(world, "what is the block height")
    assert world["session"].cons_ask is None
    assert fake.prompts == []
    assert len(outs) == 1 and outs[0].startswith(f"Chain tip (last scan): block {TIP} · answered ")


def test_fees_without_estimator_falls_through(world) -> None:
    """CONS-002 discipline: no estimator wired → the fee question keeps
    the model route (where the prompt ownership line makes it a
    clarify — golden-073), never a stand-down refusal."""
    _outs, fake, _ = _turn(world, "what are fees like right now?")
    assert len(fake.prompts) == 1


def test_gate_words_keep_the_gate(world) -> None:
    """A status line while a card pends is answered WITHOUT touching the
    flow or the gate vocabulary, and the gate's own words ("sign",
    "yes") never match the status matchers."""
    _ask(world, "consolidate my small utxos")  # TCK-CONS-003: the ONE ask
    _ask(world, "100000")  # ...answered, and the plan pends
    assert world["flow"].state is TxFlowStatus.CREATED
    assert app._fees_now_ask("sign") is False
    assert app._block_height_ask("yes") is False
    assert app._explorer_request("cancel") is None
    outs, fake, _ = _turn(world, "block height")
    assert fake.prompts == [] and any("Chain tip (last scan): block 900123" in ln for ln in outs)
    assert world["flow"].state is TxFlowStatus.CREATED  # still pending, untouched


def test_store_surprise_falls_through() -> None:
    """A store failure NEVER crashes the turn: the line falls through to
    the ordinary pipeline (sugar is fail-closed, CONS-002 discipline)."""
    import sqlite3

    class _BoomStore:
        def get_active_wallet(self):
            raise sqlite3.Error("engineered store failure")

    assert app._run_network_status_turn(
        _BoomStore(), "block height", [].append, now=NOW
    ) is False


# =========================================================================
# 5. Value-free + the closed protocol surfaces
# =========================================================================


def test_status_replies_carry_no_wallet_data(world) -> None:
    """fees/height narration is PUBLIC network data only: never a wallet
    address, never a sats amount, never a txid (the explorer links are
    the one place a user-asked-for token appears — by design)."""
    _, outs = _direct(world, "what are fees like right now?", estimator=_est())
    _, outs2 = _direct(world, "block height")
    blob = " ".join(outs + outs2)
    assert "bc1" not in blob
    assert not re.search(r"[0-9a-f]{40,}", blob)
    assert " sats" not in blob  # rates are sat/vB, balances never appear


def test_registry_stays_closed() -> None:
    assert len(list(IntentName)) == 15
    for intent in ("fees_now", "block_height", "explorer_link"):
        with pytest.raises(EnvelopeValidationError):
            validate_payload(json.dumps({"v": 0, "intent": intent, "params": {}}))


def test_prompt_ownership_block() -> None:
    """The prompt block that pairs with the intercept: the model routes
    the phrasings but owns NO facts and NO links — never a stated fee,
    height, or authored URL."""
    from localwallet.agent.prompt import build_system_prompt

    prompt = build_system_prompt()
    assert "NETWORK STATUS & EXPLORER LINKS (ENGINE-OWNED)" in prompt
    assert '"what are fees like right now"' in prompt
    assert '"show me the mempool"' in prompt
    assert "never write, guess or open a URL" in prompt
    assert "emit clarify asking the user to restate it" in prompt


def test_explorer_root_is_the_code_constant() -> None:
    """The root is the pinned constant (NOT settings-derivable): a user
    repointing the public API base cannot steer a chat link to another
    host."""
    assert app.EXPLORER_ROOT_URL == "https://mempool.space"



