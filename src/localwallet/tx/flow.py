"""Dispatcher-owned send-flow state machine and confirm gate (TCK-P2-003).

The destructive send flow is a state machine, not model improvisation
(PROJECT.md §8 invariant 6): the states and every transition live HERE, in
pure Python — the model cannot skip or reorder steps, and an LLM "yes"
never counts as user confirmation (§8.6 / ADR-0013).

Purity contract: this module performs **zero I/O** — no network (it imports
nothing from ``chain/``), no filesystem, no model, no embit. Two effects
are injected: ``clock`` (monotonic-enough wall time as epoch seconds) and
``id_factory`` (pending-transaction reference strings). Defaults are
``time.time`` / ``uuid4().hex``; tests inject deterministic stand-ins.

States and the transition table (``TxFlowStatus``)::

    IDLE ──create──> CREATED ──confirm(ok)──> CONFIRMED ──reset──> IDLE
                      │  │  └─confirm(expired)──> EXPIRED ──reset──> IDLE
                      │  └────cancel────────────> CANCELLED ─reset──> IDLE
                      └────create──> refused (FlowError, state unchanged)
    IDLE/CANCELLED/EXPIRED ──create──> CREATED
    CREATED ──reset──> refused (confirm or cancel first — explicit, not janitor)
    IDLE ──reset──> IDLE (no-op)
    IDLE/CONFIRMED/CANCELLED/EXPIRED ──confirm/cancel──> refused (FlowError)

One pending transaction at a time: ``create`` from CREATED is refused
(value-free FlowError), so two destructive flows can never interleave.
A stale pending transaction is not silently reaped: from CREATED, ``create``
stays refused and ``confirm`` reports expiry (transitioning to EXPIRED) —
the explicit recovery paths are confirm / cancel, never an implicit reset.

THE CONFIRM GATE (dual-key rule, ADR-0013): moving CREATED → CONFIRMED
requires BOTH keys on the same turn —

1. the model emitted a valid ``confirm_tx`` envelope whose ``tx_ref`` names
   the pending transaction (necessary), AND
2. the user's utterance from that same turn classifies ``CONFIRM`` under
   :class:`ConfirmGate` (sufficient, together with 1).

The caller (app layer, TCK-P2-004) passes the gate decision for the current
turn into :meth:`TxFlow.confirm`; the flow refuses when the decision is not
``CONFIRM`` even with a matching ``tx_ref``. The classifier is deterministic
app code — the user-utterance decision is never delegated to the model.

:func:`ConfirmGate.classify` is a conservative, whitelist-exact classifier:

- Normalize: lowercase; split on whitespace; strip ASCII punctuation from
  each token's edges; drop empty tokens. No tokens ⇒ ``NOT_A_DECISION``.
- If the joined token sequence equals a CONFIRM whitelist phrase →
  ``CONFIRM``; equals a DENY whitelist phrase → ``DENY``.
- Otherwise token-set classification (at least ONE decision token
  required): if some token is CONFIRM-single and every token is in
  CONFIRM-single ∪ FILLER → ``CONFIRM``; some token DENY-single and every
  token in DENY-single ∪ FILLER → ``DENY``; a mix of CONFIRM-single and
  DENY-single tokens (plus any filler) → ``AMBIGUOUS``; anything else —
  any unknown token (e.g. "ok") or filler-only token sets ("the tx") —
  → ``NOT_A_DECISION``. Filler alone carries no decision: confirmation
  requires positive evidence.

Whitelists (exact, case/punctuation-insensitive at token edges)::

    CONFIRM phrases: yes, y, yes please, confirm, confirmed, confirm it,
                     send it, send, approve, approved, do it
    DENY phrases:    no, n, no thanks, cancel, cancel it, abort, stop,
                     don't, dont, deny, reject
    FILLER tokens:   please, the, it, tx, transaction

"ok" is deliberately NOT whitelisted (too ambiguous alone): "ok" and
near-misses like "ok send" classify NOT_A_DECISION, the flow stays CREATED,
and the app re-asks. ``AMBIGUOUS`` is reserved for mixed confirm+deny
signals — the only fuzzy-adjacent case — because both a yes and a no were
literally said; everything unknown is fail-closed NOT_A_DECISION. There is
NO fuzzy/substring/edit-distance matching: a near-match that slipped through
would be a silent destructive step, while a missed match only costs one
re-ask (fail-closed asymmetry, ADR-0013).

This module is deliberately in ``tx/`` (not ``protocol/``): it is
dispatcher-side flow state, sits next to the tx engine it protects, and
imports nothing from the protocol package (the protocol layer validates
envelopes; the flow owns what happens between them).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from string import punctuation
from typing import Final

from localwallet.tx.dust import TxEngineError

__all__ = [
    "PENDING_TTL_S",
    "ConfirmGate",
    "FlowError",
    "GateDecision",
    "PendingTx",
    "TxFlow",
    "TxFlowStatus",
]

#: Pending-transaction time-to-live, seconds (10 minutes): a ``CREATED``
#: transaction older than this is expired at the next :meth:`TxFlow.confirm`.
PENDING_TTL_S: Final[int] = 600


class FlowError(TxEngineError):
    """A send-flow state transition was refused.

    Messages are value-free: they never echo the refused ``tx_ref``, the
    user utterance, addresses, or amounts (those flow into error envelopes
    and logs — PROJECT.md §7.8).
    """


class TxFlowStatus(StrEnum):
    """States of the dispatcher-owned send flow.

    ``CREATED`` is the only live state; ``CONFIRMED``, ``CANCELLED`` and
    ``EXPIRED`` are terminal (leftable only via :meth:`TxFlow.reset` to
    ``IDLE``); ``IDLE`` is the empty state.
    """

    IDLE = "idle"
    CREATED = "created"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PendingTx:
    """One staged, unsigned, not-yet-confirmed send (immutable record).

    Built by :meth:`TxFlow.create` from handler-supplied business fields
    (already validated upstream: schema + business rules + tx engine) and
    stamped by the flow with identity:

    - ``tx_ref`` — from the flow's injected ``id_factory`` (uuid4 hex by
      default); the reference the confirmation card and ``confirm_tx``
      quote verbatim.
    - ``created_at`` — from the flow's injected ``clock`` (epoch seconds);
      the expiry baseline (:data:`PENDING_TTL_S`).

    Frozen: once staged, the confirmed-against data cannot drift — the
    signer handoff (Phase 3) receives exactly the object the user confirmed.
    Field values may contain addresses/amounts (they must — the confirmation
    card renders from them); callers still never place them in logs
    (PROJECT.md §7.8).
    """

    tx_ref: str
    created_at: float
    amount_sats: int
    recipient: str
    fee_target: str | None
    fee_rate_sat_vb: int
    fee_sats: int
    change_sats: int | None
    psbt_base64: str
    inputs_count: int
    vsize: int


class GateDecision(StrEnum):
    """Deterministic classification of a user utterance while a tx pends.

    ``CONFIRM``/``DENY`` are explicit decisions; ``AMBIGUOUS`` means both
    signals were literally present (mixed tokens); ``NOT_A_DECISION`` is
    any other chat turn — the flow stays CREATED and the model envelope for
    that turn is processed normally.
    """

    CONFIRM = "confirm"
    DENY = "deny"
    AMBIGUOUS = "ambiguous"
    NOT_A_DECISION = "not_a_decision"


class ConfirmGate:
    """Deterministic user-utterance classifier (app code, never the model).

    Stateless; :meth:`classify` is a pure function of the utterance string.
    See the module docstring for the exact rules and the whitelist. Lives
    here (not a separate module) because the gate is only meaningful
    against the flow's CREATED state and its decision is consumed by
    :meth:`TxFlow.confirm` — one module, one security-review unit.
    """

    #: Full whitelist phrases (matched against the joined, normalized token
    #: sequence — covers multi-word entries like "do it" whose first word is
    #: not itself whitelisted).
    CONFIRM_PHRASES: Final[frozenset[str]] = frozenset(
        {"yes", "y", "yes please", "confirm", "confirmed", "confirm it", "send it", "send", "approve", "approved", "do it"}
    )
    DENY_PHRASES: Final[frozenset[str]] = frozenset(
        {"no", "n", "no thanks", "cancel", "cancel it", "abort", "stop", "don't", "dont", "deny", "reject"}
    )

    #: Single tokens that carry a decision on their own (the one-word
    #: members of the phrase whitelists above).
    CONFIRM_TOKENS: Final[frozenset[str]] = frozenset(
        {"yes", "y", "confirm", "confirmed", "send", "approve", "approved"}
    )
    DENY_TOKENS: Final[frozenset[str]] = frozenset(
        {"no", "n", "cancel", "abort", "stop", "don't", "dont", "deny", "reject"}
    )

    #: Tokens that carry no decision but may accompany one ("yes please",
    #: "cancel the tx"). Anything outside CONFIRM/DENY/FILLER makes the
    #: utterance not-a-decision (fail closed).
    FILLER_TOKENS: Final[frozenset[str]] = frozenset({"please", "the", "it", "tx", "transaction"})

    @staticmethod
    def _tokenize(utterance: str) -> tuple[str, ...]:
        """Lowercase, split on whitespace, strip edge punctuation, drop empties."""
        tokens: list[str] = []
        for raw in utterance.lower().split():
            token = raw.strip(punctuation)
            if token:
                tokens.append(token)
        return tuple(tokens)

    @staticmethod
    def classify(utterance: str) -> GateDecision:
        """Classify one user utterance (exact rules in the module docstring).

        Conservative by construction: only whitelisted words (plus the
        filler set) ever produce a decision; one unknown token turns the
        whole utterance into ``NOT_A_DECISION`` so the app re-asks instead
        of guessing.
        """
        tokens = ConfirmGate._tokenize(utterance)
        if not tokens:
            return GateDecision.NOT_A_DECISION

        phrase = " ".join(tokens)
        if phrase in ConfirmGate.CONFIRM_PHRASES:
            return GateDecision.CONFIRM
        if phrase in ConfirmGate.DENY_PHRASES:
            return GateDecision.DENY

        hits: set[GateDecision] = set()
        for token in tokens:
            if token in ConfirmGate.CONFIRM_TOKENS:
                hits.add(GateDecision.CONFIRM)
            elif token in ConfirmGate.DENY_TOKENS:
                hits.add(GateDecision.DENY)
            elif token not in ConfirmGate.FILLER_TOKENS:
                return GateDecision.NOT_A_DECISION  # unknown word: fail closed

        if hits == {GateDecision.CONFIRM}:
            return GateDecision.CONFIRM
        if hits == {GateDecision.DENY}:
            return GateDecision.DENY
        if not hits:
            return GateDecision.NOT_A_DECISION  # filler only: no decision evidence
        return GateDecision.AMBIGUOUS  # both signals literally present


class TxFlow:
    """The dispatcher-owned send-flow state machine (one pending tx max).

    Single-threaded dispatcher assumption: this stateful object is NOT
    internally locked — it is owned by one dispatcher in a single-user CLI
    loop (wired in TCK-P2-004), so callers must not race it. No locking is
    added because there is no concurrent caller to protect against.

    Args:
        id_factory: Zero-argument callable producing a fresh ``tx_ref``
            string per :meth:`create` (default ``uuid4().hex``). Injectable
            for deterministic tests (e.g. a counting factory).
        clock: Zero-argument callable returning current time as epoch-seconds
            float (default ``time.time``). Injectable for expiry tests.
    """

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._id_factory = id_factory if id_factory is not None else (lambda: uuid.uuid4().hex)
        self._clock = clock if clock is not None else time.time
        self._state: TxFlowStatus = TxFlowStatus.IDLE
        self._pending: PendingTx | None = None

    # ------------------------------------------------------------- views

    @property
    def state(self) -> TxFlowStatus:
        """Current flow state."""
        return self._state

    @property
    def pending(self) -> PendingTx | None:
        """The staged pending transaction, or ``None`` outside ``CREATED``."""
        return self._pending

    # ------------------------------------------------------- transitions

    def create(
        self,
        *,
        amount_sats: int,
        recipient: str,
        fee_rate_sat_vb: int,
        fee_sats: int,
        psbt_base64: str,
        inputs_count: int,
        vsize: int,
        fee_target: str | None = None,
        change_sats: int | None = None,
    ) -> PendingTx:
        """Stage a new pending transaction (IDLE/CANCELLED/EXPIRED → CREATED).

        Stamps ``tx_ref`` (via ``id_factory``) and ``created_at`` (via
        ``clock``) onto the handler-supplied, already-validated business
        fields; the flow owns pending-tx identity so the confirmation card,
        the ``confirm_tx`` envelope, and the gate all reference the same
        immutable record.

        Raises:
            FlowError: a transaction is already pending (state CREATED) —
                value-free message; confirm or cancel it first. A stale
                pending is NOT auto-reaped here: confirm it (reports
                expiry) or cancel it explicitly.
        """
        if self._state is TxFlowStatus.CREATED:
            raise FlowError("a transaction is already pending — confirm or cancel it first")
        pending = PendingTx(
            tx_ref=self._id_factory(),
            created_at=self._clock(),
            amount_sats=amount_sats,
            recipient=recipient,
            fee_target=fee_target,
            fee_rate_sat_vb=fee_rate_sat_vb,
            fee_sats=fee_sats,
            change_sats=change_sats,
            psbt_base64=psbt_base64,
            inputs_count=inputs_count,
            vsize=vsize,
        )
        self._pending = pending
        self._state = TxFlowStatus.CREATED
        return pending

    def confirm(
        self,
        tx_ref: str,
        *,
        gate_decision: GateDecision,
        at: float | None = None,
    ) -> PendingTx:
        """Confirm the pending transaction (CREATED → CONFIRMED).

        Dual-key rule (ADR-0013): every precondition must hold, in order —

        1. state is ``CREATED`` (a pending transaction exists);
        2. the pending is not expired: ``at - created_at > PENDING_TTL_S``
           (expiry checked at exactly the TTL boundary is still valid)
           ⇒ transition to ``EXPIRED`` and refuse;
        3. ``gate_decision`` is ``CONFIRM`` — the same-turn user utterance
           explicitly confirmed; an LLM "yes" never counts;
        4. ``tx_ref`` matches the pending transaction's reference.

        Args:
            tx_ref: The reference the model's ``confirm_tx`` envelope
                quoted (must equal the pending one).
            gate_decision: The :class:`ConfirmGate` classification of the
                user's utterance from the SAME turn, passed in by the app
                layer — the flow never parses utterances itself.
            at: Evaluation time (epoch seconds); defaults to the injected
                ``clock``. Injectable for deterministic expiry tests.

        Returns:
            The confirmed :class:`PendingTx` (the exact object the user
            confirmed — the Phase 3 signer handoff starts here).

        Raises:
            FlowError: on any failed precondition; the state changes only
                for the expiry case (CREATED → EXPIRED). All messages are
                value-free.
        """
        if self._state is not TxFlowStatus.CREATED or self._pending is None:
            raise FlowError("no pending transaction to confirm")
        pending = self._pending
        now = at if at is not None else self._clock()
        if now - pending.created_at > PENDING_TTL_S:
            self._state = TxFlowStatus.EXPIRED
            self._pending = None
            raise FlowError("pending transaction expired")
        if gate_decision is not GateDecision.CONFIRM:
            raise FlowError(
                "confirmation gate not satisfied: the user has not explicitly confirmed this transaction on this turn"
            )
        if tx_ref != pending.tx_ref:
            raise FlowError("tx_ref does not match the pending transaction")
        self._state = TxFlowStatus.CONFIRMED
        self._pending = None
        return pending

    def cancel(self) -> PendingTx:
        """Cancel the pending transaction (CREATED → CANCELLED).

        Raises:
            FlowError: no pending transaction to cancel (any state but
                CREATED); value-free message.
        """
        if self._state is not TxFlowStatus.CREATED or self._pending is None:
            raise FlowError("no pending transaction to cancel")
        pending = self._pending
        self._pending = None
        self._state = TxFlowStatus.CANCELLED
        return pending

    def reset(self) -> None:
        """Return a terminal flow to IDLE (CONFIRMED/CANCELLED/EXPIRED → IDLE).

        A no-op from IDLE. From CREATED this is refused — leaving a live
        pending without an explicit confirm/cancel decision would be the
        kind of silent step the flow exists to prevent (use
        :meth:`cancel`).

        Raises:
            FlowError: state is CREATED.
        """
        if self._state is TxFlowStatus.CREATED:
            raise FlowError("a transaction is already pending — confirm or cancel it first")
        self._state = TxFlowStatus.IDLE
        self._pending = None
