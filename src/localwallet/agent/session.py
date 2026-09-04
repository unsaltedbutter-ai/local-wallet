"""Session context management and transcript export/scrub (TCK-P5-002, R13).

Phase 5 / R13: a small-model context discipline (PROJECT.md §13 R13, §7.1)
over a potentially long, multi-topic session. This module owns the
**deterministic** summarization and the OQ14 transcript management, both
pinned in ADR-0020.

Summarization (R13)
-------------------
Long sessions are kept inside the context budget by keeping the most recent
turns verbatim and folding older turns into a compact, **structured,
deterministic summary** of *what happened* — topics (intents) covered, flow
state transitions, and event counts — NOT the values. The summary is built
from dispatcher-owned state only:

- intent names parsed from each turn's canonical envelope JSON (the intents
  are a closed enum — no user or model free text enters the summary), and
- integer counters (turns, destructive-flow steps, watch events) recorded
  by the app.

It is **not model-generated**: no extra inference calls, no nondeterminism.
PRIVACY: the summary and the rolling recent-turns context are **value-free**
— no addresses, no amounts, no xpubs. Money values appear only in the
per-turn FACTS block for the current turn, never in the retained summary.

The budget is pinned by two constants: :data:`MAX_RECENT_TURNS` (verbatim
turns retained) and :data:`MAX_SUMMARY_CHARS` (summary block cap). A session
of any length stays within that budget because only the recent window and a
bounded summary are ever injected.

Transcript management (OQ14)
----------------------------
- **Export**: render the session transcript to plain text with sensitive
  material **redacted** (see :data:`REDACTION` for the exact set —
  addresses, amounts, xpub/keys, cookie paths). Value-free export: the
  written file carries no addresses, amounts, or xpubs.
- **Scrub**: clear the in-memory transcript/summary entirely.

These are deterministic UI features exposed as CLI commands (``/export``,
``/scrub``), NOT model intents — so no protocol/grammar/prompt change
(ADR-0020 records that choice).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = [
    "MAX_RECENT_TURNS",
    "MAX_SUMMARY_CHARS",
    "REDACTION",
    "SessionSummary",
    "build_session_context",
    "export_transcript",
    "redact_transcript",
    "render_summary",
]

#: Recent turns kept verbatim; older turns fold into the structured summary.
#: (Retains the historical 20-turn verbatim cap; beyond it, summarization —
#: R13 — replaces plain drop-oldest.)
MAX_RECENT_TURNS: Final[int] = 20

#: Hard cap on the rendered summary block, in characters. Bounds the
#: injected context for a session of any length (ADR-0020 budget).
MAX_SUMMARY_CHARS: Final[int] = 400

#: Intents that advance the dispatcher-owned destructive send flow
#: (ADR-0013). Counted in the summary as "flow steps" (value-free).
_FLOW_INTENTS: Final[frozenset[str]] = frozenset(
    {"create_tx", "confirm_tx", "sign_tx", "broadcast_tx"}
)


# ------------------------------------------------------------------ redaction

#: The export redaction set (OQ14, ADR-0020). Each entry is a compiled regex
#: matched over the raw transcript text and replaced with a value-free token.
#: Applied in order; the set is deliberately narrow and documented:
#:
#: 1. **Addresses** — testnet/mainnet bech32 (``tb1``/``bc1``/``bcrt1``)
#:    and common legacy prefixes, redacted as ``<addr>``.
#: 2. **XPUBs / keys** — extended public (and any) key strings
#:    (``xpub/ypub/zpub/tpub/upub/vpub`` and ``xprv/...``) redacted as
#:    ``<key>``. (xprv material is refused at the gate; redacted here too
#:    for defense in depth.)
#: 3. **Cookie paths** — any path ending in ``.cookie`` (Bitcoin Core RPC
#:    cookie), redacted as ``<cookie-path>``.
#: 4. **Amounts** — ``<n> sats`` / ``<n> satoshis`` figures, ``$<n>`` USD
#:    figures, and ``<n> BTC``-denominated figures, redacted as ``<amount>``.
#:
#: Ordering matters (addresses/keys are matched before the looser amount
#: patterns). The tokens are stable and value-free.
_REDACTIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(
            r"\b(?:tb1|bc1|bcrt1)[0-9a-z]{25,}\b"
            r"|\b(?:1|3|m|n|2)[1-9A-HJ-NP-Za-km-z]{25,}\b",
            re.IGNORECASE,
        ),
        "<addr>",
    ),
    (
        re.compile(r"\b(?:[xytuv]pub|xprv|tprv|yprv|zprv|vprv|uprv)[1-9A-HJ-NP-Za-km-z]{50,}\b"),
        "<key>",
    ),
    (re.compile(r"\S*\.cookie\b"), "<cookie-path>"),
    (
        re.compile(
            r"\b\d{1,12}\s*(?:sats?|satoshis)\b"
            r"|\$\s*\d{1,12}(?:\.\d{1,2})?\b"
            r"|\b\d{1,12}(?:\.\d{1,8})?\s*BTC\b"
        ),
        "<amount>",
    ),
)

#: Documented redaction set (exported for tests / ADR cross-reference).
REDACTION: Final[tuple[str, ...]] = (
    "addresses",
    "amounts",
    "xpub/keys",
    "cookie paths",
)


def redact_transcript(text: str) -> str:
    """Return ``text`` with sensitive material redacted (value-free export).

    Applies the :data:`REDACTION` set in order: addresses, xpub/key
    material, cookie paths, then amounts. Output contains no addresses,
    amounts, or xpubs — only stable ``<...>`` tokens. Deterministic.
    """
    out = text
    for pattern, token in _REDACTIONS:
        out = pattern.sub(token, out)
    return out


# ------------------------------------------------------------------- summary


@dataclass
class SessionSummary:
    """Deterministic, value-free summary of older turns (R13).

    Fields:
        total_turns: Number of turns folded into the summary so far.
        intent_counts: Intent-name → count (parsed from envelope JSON; the
            closed-intent enum — never user/model free text).
        flow_steps: Count of destructive send-flow transitions folded in
            (``create_tx``/``confirm_tx``/``sign_tx``/``broadcast_tx``).
        extras: Additional value-free integer counters the app records
            (e.g. watch events), keyed by a code-controlled label.
    """

    total_turns: int = 0
    intent_counts: dict[str, int] = field(default_factory=dict)
    flow_steps: int = 0
    extras: dict[str, int] = field(default_factory=dict)

    def fold_turn(self, envelope_json: str | None) -> None:
        """Fold one old turn's *shape* into the summary (value-free).

        Only the intent name is extracted from the canonical envelope JSON;
        no addresses, amounts, txids, or free text are retained. A turn
        without a parseable envelope is counted as a chat turn only.
        """
        self.total_turns += 1
        intent = _extract_intent(envelope_json)
        if intent is None:
            return
        self.intent_counts[intent] = self.intent_counts.get(intent, 0) + 1
        if intent in _FLOW_INTENTS:
            self.flow_steps += 1

    def record_extra(self, label: str, count: int = 1) -> None:
        """Increment a value-free extra counter (e.g. ``watch_events``)."""
        self.extras[label] = self.extras.get(label, 0) + int(count)


def _extract_intent(envelope_json: str | None) -> str | None:
    """Parse the closed-intent name from a canonical envelope JSON string.

    Returns ``None`` for a missing/unparseable envelope (an escalated turn)
    or an envelope whose ``intent`` is not a string — both are value-free
    and simply don't contribute an intent count.
    """
    if not envelope_json:
        return None
    try:
        data = json.loads(envelope_json)
    except ValueError:
        return None
    intent = data.get("intent") if isinstance(data, dict) else None
    return intent if isinstance(intent, str) else None


def render_summary(summary: SessionSummary) -> str:
    """Render the summary as a compact, value-free text block.

    Only counts and intent names (a closed enum) appear — never addresses,
    amounts, or xpubs. Capped at :data:`MAX_SUMMARY_CHARS`.
    """
    if summary.total_turns == 0 and not summary.extras:
        return ""
    lines: list[str] = ["SESSION SUMMARY (older turns)"]
    lines.append(f"- turns summarized: {summary.total_turns}")
    if summary.flow_steps:
        lines.append(f"- send-flow steps: {summary.flow_steps}")
    intents = sorted(summary.intent_counts.items())
    if intents:
        topics = ", ".join(f"{name}: {count}" for name, count in intents)
        lines.append(f"- topics: {topics}")
    for label in sorted(summary.extras):
        lines.append(f"- {label}: {summary.extras[label]}")
    text = "\n".join(lines)
    return text[:MAX_SUMMARY_CHARS]


# ------------------------------------------------------------- context build


def build_session_context(
    summary: SessionSummary,
    recent: Sequence[Any],
) -> tuple[str, list[Any]]:
    """Return ``(summary_block, recent_turns)`` for prompt injection.

    ``recent`` is the verbatim recent window (oldest first); ``summary`` is
    the folded, value-free summary of everything older. The caller injects
    the summary block (if non-empty) followed by the recent turns.
    """
    return render_summary(summary), list(recent)


# ---------------------------------------------------------------- transcript


def export_transcript(
    summary: SessionSummary,
    recent: Sequence[Any],
    path: str | Path,
) -> int:
    """Write a redacted session transcript to ``path``; return line count.

    The written file is **value-free**: every line passes through
    :func:`redact_transcript` (the :data:`REDACTION` set). Format: a short
    header with the UTC generation time, the structured summary block, then
    the recent turns verbatim-but-redacted (oldest first).

    Raises:
        OSError: the file could not be written (propagates to the caller,
            which narrates a short, value-free line).
    """
    lines: list[str] = ["local-wallet session export"]
    lines.append(f"generated (UTC): {datetime.now(UTC).isoformat()}")
    summary_block = render_summary(summary)
    if summary_block:
        lines.append(summary_block)
    if recent:
        lines.append("RECENT TURNS (oldest first):")
        for turn in recent:
            lines.append(f"user: {turn.user_text}")
            envelope = turn.envelope_json if turn.envelope_json is not None else "(no envelope)"
            lines.append(f"envelope: {envelope}")
    text = redact_transcript("\n".join(lines)) + "\n"
    Path(path).write_text(text, encoding="utf-8")
    return len(lines)
