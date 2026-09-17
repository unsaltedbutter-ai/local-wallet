"""TCK-TXID-001 pins, RE-ADJUDICATED by TCK-TXID-002 (2026-09-16).

TXID-001 made every chat narration line inline the FULL 64-hex txid so
click-to-copy never grabbed a fragment. The USER CLARIFICATION reversed
the DISPLAY half of that rule: "full hex strings in messages are NOT
user-friendly". Every site TXID-001 de-truncated now prints the COMPACT
token (first 8 hex + ellipsis, TCK-TXID-002's pinned CLI shape) and the
FULL value rides the additive typed ``txid_refs`` payload — the UTXO-006
``txid`` + ``txid_copy_only`` vocabulary, so the web half renders the
same ``[tx]`` copy chip. Each test below is the re-adjudicated form of
its TXID-001 original: the line text is compact, the payload carries the
full id, the gate flag is present.

Still standing from TXID-001 (asserted here unchanged): the value-free
rule scrubs LOGS (never the transcript's ids), the ``<unknown>`` honest
marker, and the client's standalone-token copy scanner (which now serves
the DIRECT-ASK lines — see test_txid002_compact_txid.py).

Direct renderer calls with handler-shaped dicts — the renderers are pure
over the result dict, so no store/chain is needed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import (
    EVENT_TXID_REFS,
    EventEmitter,
    SendSession,
    _narrate_incoming_event,
    _print_broadcast_tx,
    _print_bump_fee,
    _print_cpfp_coin_ask,
    _print_cpfp_plan,
    _print_history,
    _print_sign_tx,
    _print_tx_status,
    _print_utxos,
)
from localwallet.chain import IncomingEvent

TXID = "d2c5204c3420" + "ab" * 26  # the user's reported txid head, completed
OTHER = "e" * 64
COMPACT = TXID[:8] + "\u2026"  # d2c5204c… — the pinned TXID-002 CLI shape
OTHER_COMPACT = OTHER[:8] + "\u2026"
FULL64_STANDALONE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


class _Capture:
    """The web-pump shape: text lines + typed events on one emitter."""

    def __init__(self) -> None:
        self.events: list = []
        self.emitter = EventEmitter(self.events.append)

    @property
    def lines(self) -> list[str]:
        return [e.payload for e in self.events if e.kind == "text"]

    def refs(self) -> list[dict[str, object]]:
        payloads = [e.payload for e in self.events if e.kind == EVENT_TXID_REFS]
        assert len(payloads) <= 1, "one txid_refs event per surface"
        return json.loads(payloads[0]) if payloads else []


def _narrate(fn, result, *, web=True, **kw):
    cap = _Capture()
    if web:
        fn(result, cap.emitter.text, emitter=cap.emitter, **kw)
        return cap
    outputs: list[str] = []
    fn(result, outputs.append, **kw)
    cap.events.extend(
        type("E", (), {"kind": "text", "payload": line})() for line in outputs
    )
    return cap


def _assert_compact_line(line: str, txid: str = TXID) -> None:
    """TCK-TXID-002 per-site pin: the line references the tx through the
    COMPACT token only — no 64-hex wall, and (unlike the pre-TXID-001
    truncation) the token is the declared 8-hex shape, not 12."""
    compact = txid[:8] + "\u2026"
    assert compact in line, line
    assert txid not in line, line  # the FULL value never rides the text
    assert not FULL64_STANDALONE.search(line), line
    assert f"{txid[:12]}…" not in line, line  # never the OLD 12-hex shape


def _assert_chip_payload(refs: list[dict[str, object]], txid: str = TXID) -> None:
    """The typed payload carries the full id + the UTXO-006 gate flag."""
    entry = next(e for e in refs if e["txid"] == txid)
    assert entry["compact"] == txid[:8] + "\u2026"
    assert entry["txid_copy_only"] is True  # clipboard payload only
    assert set(entry) == {"compact", "txid", "txid_copy_only"}


# ----------------------------------------------------------- broadcast ack


def test_broadcast_ack_txid_is_compact_with_full_payload() -> None:
    # TXID-001 pin "Sent! txid {TXID} — tracking…" RE-ADJUDICATED: the
    # surrounding copy is byte-identical, the value is the compact token;
    # the full id rides txid_refs (web chip), /details-free surface.
    cap = _narrate(_print_broadcast_tx, {"status": "broadcast", "txid": TXID}, web=True)
    (line,) = [ln for ln in cap.lines if "Sent!" in ln]
    assert line == f"Sent! txid {COMPACT} — tracking…"
    _assert_compact_line(line)
    _assert_chip_payload(cap.refs())


def test_broadcast_supersede_line_txids_are_compact_with_full_payloads() -> None:
    cap = _narrate(
        _print_broadcast_tx,
        {"status": "broadcast", "txid": TXID, "replaces_txid": OTHER},
        web=True,
    )
    assert any(TXID[:8] + "…" in line for line in cap.lines)
    replace_line = next(line for line in cap.lines if "Replaces:" in line)
    _assert_compact_line(replace_line, OTHER)
    refs = cap.refs()
    # ONE payload event carries BOTH full ids (deduped by full txid).
    _assert_chip_payload(refs, TXID)
    _assert_chip_payload(refs, OTHER)


# --------------------------------------------------------- tx_status lineage


def test_tx_status_replaced_copy_quotes_compact_with_full_payload() -> None:
    cap = _narrate(
        _print_tx_status,
        {"lineage": "replaced", "replaced_by": TXID, "replacement_height": 900_001},
        web=True,
    )
    (line,) = cap.lines
    assert line == (
        f"It was replaced by {COMPACT} — the replacement confirmed at height 900001."
    )
    _assert_compact_line(line)
    _assert_chip_payload(cap.refs())


def test_tx_status_replaced_hedge_copy_compact() -> None:
    cap = _narrate(
        _print_tx_status, {"lineage": "replaced", "replaced_by": TXID}, web=True
    )
    (line,) = cap.lines
    _assert_compact_line(line)
    assert "only one of these two ever will" in line
    _assert_chip_payload(cap.refs())


def test_tx_status_evicted_copy_compact() -> None:
    cap = _narrate(
        _print_tx_status,
        {"lineage": "evicted", "original_txid": TXID, "original_height": 900_002},
        web=True,
    )
    (line,) = cap.lines
    _assert_compact_line(line)
    assert "at height 900002" in line
    _assert_chip_payload(cap.refs())


def test_tx_status_unknown_confirmation_never_narrated_unconfirmed() -> None:
    """TCK-ELECTRUM-002 (unchanged by TXID-002): a backend that could not
    report confirmation status (``confirmed`` is ``None``) must not be
    narrated as unconfirmed — the honest value-free hedge, never "In
    mempool (unconfirmed)."."""
    (line,) = _narrate(
        _print_tx_status,
        {"confirmed": None, "block_height": None, "block_time": None},
    ).lines
    assert line == (
        "The server did not report confirmation status for this transaction "
        "— check an explorer if you need certainty."
    )
    assert "unconfirmed" not in line


# ----------------------------------------------------------- sign ack


def test_sign_ack_txid_is_compact_with_full_payload() -> None:
    cap = _narrate(
        _print_sign_tx, {"status": "signed", "txid": TXID}, web=True
    )
    (line,) = cap.lines
    assert line == (
        f"Signed and verified ✓ txid {COMPACT}. Ready to broadcast — say 'broadcast'."
    )
    _assert_compact_line(line)
    _assert_chip_payload(cap.refs())


# ------------------------------------------------------- history / utxos


def test_history_line_txid_is_compact_with_full_payload() -> None:
    cap = _narrate(
        _print_history,
        {"transactions": [{"txid": TXID, "direction": "out", "height": None}]},
        web=True,
    )
    (line,) = cap.lines
    assert line == f"tx {COMPACT} out unconfirmed"
    _assert_compact_line(line)
    _assert_chip_payload(cap.refs())


def test_history_missing_txid_stays_honest_unknown() -> None:
    # TXID-001's dedup fix STANDS: an empty txid is "<unknown>", never a
    # bare ellipsis — and (TXID-002) an unchipable marker registers no
    # payload entry.
    cap = _narrate(_print_history, {"transactions": [{"direction": "out"}]}, web=True)
    (line,) = cap.lines
    assert line == "tx <unknown> out unconfirmed"
    assert cap.refs() == []


def test_utxo_line_txid_is_compact_with_full_payload() -> None:
    # Direct-call shape WITHOUT utxo_rows (the filtered-listing/legacy
    # fallback): compact text + txid_refs (the row-chip case is pinned in
    # test_utxo005/006 — those rows are contract-frozen here).
    cap = _narrate(
        _print_utxos,
        {"utxos": [{"txid": TXID, "vout": 1, "value_sats": 5000, "confirmed": 1}]},
        web=True,
    )
    (line,) = [ln for ln in cap.lines if " sats · " in ln]
    assert line == f"5,000 sats · confirmed · tx {COMPACT} vout 1"
    _assert_compact_line(line)
    _assert_chip_payload(cap.refs())


# ---------------------------------------------------------- bump / cpfp


def test_bump_target_ask_option_txid_is_compact() -> None:
    cap = _narrate(
        _print_bump_fee,
        {
            "ask": "target",
            "options": [
                {
                    "index": 1,
                    "txid": TXID,
                    "amount_sats": 50_000,
                    "fee_rate_centisat_vb": 150,
                    "age_s": 600,
                }
            ],
        },
        web=True,
    )
    option_line = next(line for line in cap.lines if "tx " in line)
    _assert_compact_line(option_line)
    assert "50,000 sats" in option_line
    _assert_chip_payload(cap.refs())


def test_bump_plan_card_replaces_line_compact_in_chat_full_in_details() -> None:
    # TXID-001 pin "cached == printed (both full)" RE-ADJUDICATED: the
    # chat line is compact (chip payload carries the full id), the cached
    # /details reprint keeps the FULL value (the ticket's stated CLI
    # full-id surface).
    session = SendSession()
    cap = _narrate(
        _print_bump_fee,
        {
            "replaces": OTHER,
            "recipient": "bc1qx",
            "amount_sats": 40_000,
            "old_fee_sats": 141,
            "fee_sats": 200,
            "fee_delta_sats": 59,
        },
        web=True,
        session=session,
    )
    replace_line = next(line for line in cap.lines if "Replaces:" in line)
    _assert_compact_line(replace_line, OTHER)
    _assert_chip_payload(cap.refs(), OTHER)
    cached = next(line for line in session.card_render if "Replaces:" in line)
    assert cached == (
        f"Replaces: {OTHER} — the original may still confirm; only one of "
        "these two ever will"
    )


def test_cpfp_coin_ask_option_txid_is_compact() -> None:
    cap = _narrate(
        _print_cpfp_coin_ask,
        {
            "options": [
                {"index": 1, "txid": TXID, "value_sats": 25_000, "age_s": 900, "vout": 0}
            ]
        },
        web=True,
    )
    option_line = next(line for line in cap.lines if "tx " in line)
    _assert_compact_line(option_line)
    _assert_chip_payload(cap.refs())


def test_cpfp_plan_card_hurries_line_compact_in_chat_full_in_details() -> None:
    session = SendSession()
    cap = _narrate(
        _print_cpfp_plan,
        {"cpfp_parent_txid": OTHER, "amount_sats": 24_000, "fee_sats": 900},
        web=True,
        session=session,
    )
    parent_line = next(line for line in cap.lines if "Hurries:" in line)
    _assert_compact_line(parent_line, OTHER)
    _assert_chip_payload(cap.refs(), OTHER)
    cached = next(line for line in session.card_render if "Hurries:" in line)
    assert cached == f"Hurries: {OTHER} — this child pays its way too"


# ------------------------------------------------------------- watch lines


def test_watch_received_line_txid_is_compact() -> None:
    event = IncomingEvent(
        kind="received", txid=TXID, address="bc1qx", amount_sats=5000, confirmed=False
    )
    line = _narrate_incoming_event(event)  # refs=None: text-only caller
    assert line == f"Incoming: received 5000 sats at bc1qx (in mempool, tx {COMPACT})."
    _assert_compact_line(line)


def test_watch_confirmed_line_txid_is_compact() -> None:
    event = IncomingEvent(
        kind="confirmed",
        txid=TXID,
        address="bc1qx",
        amount_sats=5000,
        confirmed=True,
        height=99,
    )
    line = _narrate_incoming_event(event)
    assert line == (
        f"Confirmed: 5000 sats at bc1qx now confirmed (height 99) (tx {COMPACT})."
    )
    _assert_compact_line(line)


# ------------------------------------------- static contract (client side)


def test_client_txid_token_regex_is_exactly_64_lowercase_hex() -> None:
    """TXID-001's client-scanner pin STANDS (static/** read here, NEVER
    edited — pin-only): the full-hex click-to-copy scan still serves the
    surfaces that print FULL ids — the TCK-TXID-002 direct ask, /details,
    and explorer URLs."""
    js = (
        _SRC / "localwallet" / "ui" / "web" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "const TXID_RE = /^[0-9a-f]{64}$/;" in js
    assert "const LINK_SCAN_RE =" in js and "[0-9a-f]{64})\\b" in js
