"""TCK-TXID-001: chat narration emits FULL 64-hex txids (copy material).

Every transcript surface that shows a txid shows the COMPLETE lowercase
64-hex value — truncation handed click-to-copy (WEB-014/LINK-001 token
scan, which qualifies exactly ``[0-9a-f]{64}`` standalone tokens) a
fragment. The value-free scrubbing applies to LOGS, not the transcript.

Direct renderer calls with handler-shaped dicts — the renderers are pure
over the result dict, so no store/chain is needed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import (
    SendSession,
    _narrate_incoming_event,
    _print_broadcast_tx,
    _print_bump_fee,
    _print_cpfp_coin_ask,
    _print_cpfp_plan,
    _print_history,
    _print_tx_status,
    _print_utxos,
)
from localwallet.chain import IncomingEvent

TXID = "d2c5204c3420" + "ab" * 26  # the user's reported txid head, completed
OTHER = "e" * 64
FULL64_STANDALONE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


def _narrate(fn, result, **kw):
    outputs: list[str] = []
    fn(result, outputs.append, **kw)
    return outputs


def _assert_full_txid_line(line: str, txid: str = TXID) -> None:
    """The line carries the COMPLETE token and no truncated fragment."""
    assert txid in line, line
    # no 12-hex + ellipsis fragment, no [:12] head anywhere in the line
    assert f"{txid[:12]}…" not in line, line
    # the token material IS a full 64-lowercase-hex standalone match
    assert re.fullmatch(r"[0-9a-f]{64}", txid)
    assert txid in FULL64_STANDALONE.findall(line), line


# ----------------------------------------------------------- broadcast ack


def test_broadcast_ack_txid_is_full() -> None:
    (line,) = _narrate(_print_broadcast_tx, {"status": "broadcast", "txid": TXID})
    assert line == f"Sent! txid {TXID} — tracking…"
    _assert_full_txid_line(line)


def test_broadcast_supersede_line_txids_are_full() -> None:
    lines = _narrate(
        _print_broadcast_tx,
        {"status": "broadcast", "txid": TXID, "replaces_txid": OTHER},
    )
    assert any(TXID in line for line in lines)
    replace_line = next(line for line in lines if "Replaces:" in line)
    _assert_full_txid_line(replace_line, OTHER)


# ------------------------------------------------------- tx_status lineage


def test_tx_status_replaced_copy_quotes_full_txids() -> None:
    (line,) = _narrate(
        _print_tx_status,
        {"lineage": "replaced", "replaced_by": TXID, "replacement_height": 900_001},
    )
    assert line == (
        f"It was replaced by {TXID} — the replacement confirmed at height 900001."
    )
    _assert_full_txid_line(line)


def test_tx_status_replaced_hedge_copy_quotes_full_txid() -> None:
    (line,) = _narrate(
        _print_tx_status, {"lineage": "replaced", "replaced_by": TXID}
    )
    _assert_full_txid_line(line)
    assert "only one of these two ever will" in line


def test_tx_status_evicted_copy_quotes_full_txid() -> None:
    (line,) = _narrate(
        _print_tx_status,
        {"lineage": "evicted", "original_txid": TXID, "original_height": 900_002},
    )
    _assert_full_txid_line(line)
    assert "at height 900002" in line


def test_tx_status_unknown_confirmation_never_narrated_unconfirmed() -> None:
    """TCK-ELECTRUM-002: a backend that could not report confirmation
    status (``confirmed`` is ``None``) must not be narrated as unconfirmed —
    the honest value-free hedge, never "In mempool (unconfirmed)."."""
    (line,) = _narrate(
        _print_tx_status,
        {"confirmed": None, "block_height": None, "block_time": None},
    )
    assert line == (
        "The server did not report confirmation status for this transaction "
        "— check an explorer if you need certainty."
    )
    assert "unconfirmed" not in line


# ------------------------------------------------------- history / utxos


def test_history_line_txid_is_full() -> None:
    (line,) = _narrate(
        _print_history,
        {"transactions": [{"txid": TXID, "direction": "out", "height": None}]},
    )
    assert line == f"tx {TXID} out unconfirmed"
    _assert_full_txid_line(line)


def test_history_missing_txid_stays_honest_unknown() -> None:
    (line,) = _narrate(_print_history, {"transactions": [{"direction": "out"}]})
    assert line == "tx <unknown> out unconfirmed"


def test_utxo_line_txid_is_full() -> None:
    (line,) = _narrate(
        _print_utxos,
        {"utxos": [{"txid": TXID, "vout": 1, "value_sats": 5000, "confirmed": 1}]},
    )
    assert line == f"5000 sats · confirmed · tx {TXID} vout 1"
    _assert_full_txid_line(line)


# ---------------------------------------------------------- bump / cpfp


def test_bump_target_ask_option_txid_is_full() -> None:
    lines = _narrate(
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
    )
    option_line = next(line for line in lines if "tx " in line)
    _assert_full_txid_line(option_line)
    assert "50,000 sats" in option_line


def test_bump_plan_card_replaces_line_txid_is_full() -> None:
    session = SendSession()
    lines = _narrate(
        _print_bump_fee,
        {
            "replaces": OTHER,
            "recipient": "bc1qx",
            "amount_sats": 40_000,
            "old_fee_sats": 141,
            "fee_sats": 200,
            "fee_delta_sats": 59,
        },
        session=session,
    )
    replace_line = next(line for line in lines if "Replaces:" in line)
    _assert_full_txid_line(replace_line, OTHER)
    # the cached /details reprint carries the same full token
    cached = next(line for line in session.card_render if "Replaces:" in line)
    assert cached == replace_line


def test_cpfp_coin_ask_option_txid_is_full() -> None:
    lines = _narrate(
        _print_cpfp_coin_ask,
        {
            "options": [
                {"index": 1, "txid": TXID, "value_sats": 25_000, "age_s": 900, "vout": 0}
            ]
        },
    )
    option_line = next(line for line in lines if "tx " in line)
    _assert_full_txid_line(option_line)


def test_cpfp_plan_card_hurries_line_txid_is_full() -> None:
    lines = _narrate(
        _print_cpfp_plan,
        {"cpfp_parent_txid": OTHER, "amount_sats": 24_000, "fee_sats": 900},
    )
    parent_line = next(line for line in lines if "Hurries:" in line)
    _assert_full_txid_line(parent_line, OTHER)


# ------------------------------------------------------------- watch lines


def test_watch_received_line_txid_is_full() -> None:
    event = IncomingEvent(
        kind="received", txid=TXID, address="bc1qx", amount_sats=5000, confirmed=False
    )
    line = _narrate_incoming_event(event)
    assert line == f"Incoming: received 5000 sats at bc1qx (in mempool, tx {TXID})."
    _assert_full_txid_line(line)


def test_watch_confirmed_line_txid_is_full() -> None:
    event = IncomingEvent(
        kind="confirmed",
        txid=TXID,
        address="bc1qx",
        amount_sats=5000,
        confirmed=True,
        height=99,
    )
    line = _narrate_incoming_event(event)
    assert line == f"Confirmed: 5000 sats at bc1qx now confirmed (height 99) (tx {TXID})."
    _assert_full_txid_line(line)


# ------------------------------------------- static contract (client side)


def test_client_txid_token_regex_is_exactly_64_lowercase_hex() -> None:
    """The shipped client scanner qualifies the full token the narration
    now emits (static/** is read here, NEVER edited — pin-only)."""
    js = (
        _SRC / "localwallet" / "ui" / "web" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "const TXID_RE = /^[0-9a-f]{64}$/;" in js
    assert "const LINK_SCAN_RE =" in js and "[0-9a-f]{64})\\b" in js
