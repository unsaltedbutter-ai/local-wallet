"""Dispatcher-owned send-flow state machine and confirm gate (TCK-P2-003,
Phase 3 extension TCK-P3-004).

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

    IDLE ──create──> CREATED ──confirm(ok)──> CONFIRMED ──mark_signed──> SIGNED ──broadcast──> BROADCAST
                       │  │  └─confirm(expired)──> EXPIRED ──reset──> IDLE
                       │  └────cancel────────────> CANCELLED ─reset──> IDLE
                       └────create──> CREATED (replacement re-quote, new tx_ref — TCK-UX-002)
    IDLE/CANCELLED/EXPIRED ──create──> CREATED
    CREATED ──reset──> refused (confirm or cancel first — explicit, not janitor)
    IDLE ──reset──> IDLE (no-op)
    IDLE/CONFIRMED/CANCELLED/EXPIRED ──confirm/cancel──> refused (FlowError)
    CONFIRMED/SIGNED/BROADCAST ──reset──> IDLE (terminal → empty)
    CONFIRMED ──mark_signed──> SIGNED (requires matching tx_ref)
    SIGNED ──broadcast──> BROADCAST (requires matching tx_ref; records txid)
    sign from CREATED/SIGNED/IDLE/... ──> refused (FlowError, state unchanged)
    broadcast from CONFIRMED/IDLE/BROADCAST/... ──> refused (FlowError)

One pending transaction at a time — and ``create`` from ``CREATED`` keeps
that invariant even harder than a refusal did (FLOW-REQUOTE, TCK-UX-002 /
ADR-0013 amendment): the dispatcher may REPLACE the staged record with a
fresh one (new ``tx_ref``, TTL reset). There is still never more than one
pending transaction, the old ``tx_ref`` goes inert with the record it named
(a ``confirm_tx`` quoting it fails the verbatim-match check — fail closed),
and callers commit only after the new build fully succeeds (the handler
validates the replacement BEFORE discarding the old pending).
A stale pending transaction is not silently reaped: ``confirm`` reports
expiry (transitioning to EXPIRED) — the explicit recovery paths are
confirm / cancel (or an intentional replacement), never an implicit reset.

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

PHASE 3 EXTENSION (SIGNED / BROADCAST, TCK-P3-004 — ADR-0013 amendment):
the same dispatcher-owned discipline extends past CONFIRMED —

- :meth:`TxFlow.mark_signed` moves CONFIRMED → SIGNED and requires a
  matching ``tx_ref`` (the model's ``sign_tx`` envelope proposes; the flow
  disposes). DELIBERATE DESIGN — no utterance gate at the flow level for
  signing: the DEVICE interaction IS the user action. Per PROJECT.md §9
  the hardware-wallet screen is the trust anchor — the user physically
  approves the exact transaction on the device, which no chat utterance
  can strengthen. What signing still requires structurally: the flow in
  CONFIRMED plus a matching ``tx_ref`` (intent routing cannot skip or
  reorder steps), and deterministic re-validation of the signed PSBT
  against the intended transaction in the HANDLER (TCK-P3-005,
  :mod:`localwallet.tx.revalidate`) — the flow only records state, it
  never parses or verifies PSBT bytes.
- :meth:`TxFlow.broadcast` moves SIGNED → BROADCAST, requires a matching
  ``tx_ref``, and records the chain-reported txid. Broadcast additionally
  requires completed signed-PSBT re-validation at the handler level (the
  flow cannot check this itself — it is a precondition of the P3-005
  wiring, enforced before :meth:`broadcast` may be called).
- ``cancel`` is NOT available once CONFIRMED: confirmation committed the
  user's decision, and a signed transaction is committed to signing —
  there is no chat-level undo past that point (the signed transaction
  simply is not broadcast if something fails; recovery is a fresh flow).

GATE-MERGE (TCK-UX-002, ADR-0013 amendment): the confirmation card's ask
verb is "sign", and the dispatcher chains the device handoff in the SAME
turn a confirm succeeds (app wiring, :func:`localwallet.app._run_turn`).
This merges the *user prompts* from two to one; it changes nothing here:
``CONFIRMED`` and ``SIGNED`` stay distinct auditable states, the dual key
still guards confirm, the device screen is still the trust anchor for
signing (the chained handoff is the ordinary ``sign_tx`` handler path —
code-invoked with the dispatcher-owned ``tx_ref``), and broadcast keeps
its own fresh same-turn gate decision.

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
                     send it, send, sign, approve, approved, do it
    DENY phrases:    no, n, no thanks, cancel, cancel it, abort, stop,
                     don't, dont, deny, reject
    FILLER tokens:   please, the, it, tx, transaction

"sign" joined the CONFIRM whitelist with the TCK-UX-002 GATE-MERGE
(ADR-0013 amendment): the confirmation card's ask verb is "sign" ("say
'sign' to review it on your device"), so the word is a confirmation
utterance — never a signing-utterance gate. The offer words the card
names ("faster"/"slower") deliberately join NO whitelist: they classify
``NOT_A_DECISION`` and are structurally incapable of confirming (pinned
by tests). "ok" is deliberately NOT whitelisted (too ambiguous alone): "ok" and
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
    "SignedTx",
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

    ``CREATED`` and ``CONFIRMED`` are the live destructive states
    (``CREATED`` awaits the dual-key confirm; ``CONFIRMED`` awaits the
    signed PSBT from the device handoff); ``SIGNED`` awaits broadcast.
    ``CANCELLED``, ``EXPIRED`` and ``BROADCAST`` are terminal (leftable
    only via :meth:`TxFlow.reset` to ``IDLE``); ``IDLE`` is the empty
    state.

    Phase 3 (TCK-P3-004, ADR-0013 amendment): ``SIGNED`` and ``BROADCAST``
    extend the machine behind the same gate — transitions only via
    matching ``tx_ref`` from the immediately preceding state, no skip
    paths (broadcast ONLY from SIGNED).
    """

    IDLE = "idle"
    CREATED = "created"
    CONFIRMED = "confirmed"
    SIGNED = "signed"
    BROADCAST = "broadcast"
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

    TCK-TX-SELF-001 (additive, default-preserving): ``self_payment_indices``
    is non-``None`` exactly for ``self_transfer`` records — the tuple of
    ``branch-0`` (receive) child indices behind the plan's payment outputs,
    in output order. ``recipient``/``amount_sats`` then describe the FIRST
    payment output (the other outputs are its positional siblings: a split
    repeats the same value, a consolidate has exactly one), a self-transfer
    NEVER carries change (the plan folds any residue into the fee), and the
    sign-time intent builder re-derives every payment script from these
    indices to re-prove the staged PSBT against the plan — the same
    independent re-derivation discipline the change output already gets.
    External sends keep ``None`` and behave byte-for-byte as before.
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
    self_payment_indices: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class SignedTx:
    """The signed-PSBT record produced by a completed device handoff.

    Built by :meth:`TxFlow.mark_signed` from the CONFIRMED record:

    - ``tx_ref`` — the confirmed transaction's reference (the flow refuses
      a mismatched one; the value is carried for audit symmetry).
    - ``psbt_base64`` — the signed PSBT exactly as the signer gateway
      returned it. The flow performs NO parsing or verification here
      (deterministic re-validation against the intended transaction is
      the handler-level broadcast gate, TCK-P3-005 /
      :mod:`localwallet.tx.revalidate`); the record is immutable so what
      was re-validated is exactly what gets broadcast.

    Frozen for the same reason as :class:`PendingTx`: the broadcast step
    must see byte-for-byte what the device returned.
    """

    tx_ref: str
    psbt_base64: str


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
    #: not itself whitelisted). "sign" joined with the TCK-UX-002 GATE-MERGE
    #: (ADR-0013 amendment): the card's ask verb — a CONFIRMATION utterance
    #: whose handoff the dispatcher runs in the same turn.
    CONFIRM_PHRASES: Final[frozenset[str]] = frozenset(
        {"yes", "y", "yes please", "confirm", "confirmed", "confirm it", "send it", "send", "sign", "approve", "approved", "do it"}
    )
    DENY_PHRASES: Final[frozenset[str]] = frozenset(
        {"no", "n", "no thanks", "cancel", "cancel it", "abort", "stop", "don't", "dont", "deny", "reject"}
    )

    #: Single tokens that carry a decision on their own (the one-word
    #: members of the phrase whitelists above).
    CONFIRM_TOKENS: Final[frozenset[str]] = frozenset(
        {"yes", "y", "confirm", "confirmed", "send", "sign", "approve", "approved"}
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
        self._confirmed: PendingTx | None = None
        self._signed: SignedTx | None = None
        self._txid: str | None = None

    # ------------------------------------------------------------- views

    @property
    def state(self) -> TxFlowStatus:
        """Current flow state."""
        return self._state

    @property
    def pending(self) -> PendingTx | None:
        """The staged pending transaction, or ``None`` outside ``CREATED``."""
        return self._pending

    @property
    def confirmed(self) -> PendingTx | None:
        """The approved transaction, or ``None`` before ``CONFIRMED``.

        Retained through ``CONFIRMED``, ``SIGNED`` and ``BROADCAST``: the
        signer handoff and the broadcast-time re-validation (TCK-P3-005)
        both build their intent from the exact record the user confirmed.
        Cleared only when the flow returns to ``IDLE``/``CREATED``.
        """
        return self._confirmed

    @property
    def signed(self) -> SignedTx | None:
        """The signed-PSBT record, or ``None`` before ``SIGNED``."""
        return self._signed

    @property
    def txid(self) -> str | None:
        """The chain-reported transaction id, or ``None`` before ``BROADCAST``."""
        return self._txid

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
        self_payment_indices: tuple[int, ...] | None = None,
    ) -> PendingTx:
        """Stage a new pending transaction (IDLE/CANCELLED/EXPIRED → CREATED).

        Stamps ``tx_ref`` (via ``id_factory``) and ``created_at`` (via
        ``clock``) onto the handler-supplied, already-validated business
        fields; the flow owns pending-identity so the confirmation card,
        the ``confirm_tx`` envelope, and the gate all reference the same
        immutable record. ``self_payment_indices`` (TCK-TX-SELF-001, default
        ``None``) marks a ``self_transfer`` record and carries the receive
        indices behind its payment outputs — see :class:`PendingTx`.

        From ``CREATED`` this is a dispatcher-owned REPLACEMENT
        (FLOW-REQUOTE, TCK-UX-002 / ADR-0013 amendment): the staged record
        is swapped for the new one — new ``tx_ref``, fresh ``created_at``
        (TTL reset), still exactly one pending. The old reference goes
        inert the moment the record is replaced (a confirm quoting it
        fails the verbatim-match check). The CALLER owns the
        commit-only-on-success ordering: nothing is replaced until the new
        build has fully validated (the create handler runs all money math
        before calling this method).

        Raises:
            FlowError: a transaction is already in flight PAST the gate —
                value-free message. From CONFIRMED/SIGNED/BROADCAST the
                approved/signed record is still live: starting a second
                flow here would silently abandon it (the silent step the
                machine exists to prevent) — finish the lifecycle or
                :meth:`reset` explicitly first. A stale pending is NOT
                auto-reaped: confirm it (reports expiry) or cancel it
                explicitly.
        """
        if self._state not in (
            TxFlowStatus.IDLE,
            TxFlowStatus.CANCELLED,
            TxFlowStatus.EXPIRED,
            TxFlowStatus.CREATED,
        ):
            raise FlowError(
                "a transaction is already in flight — finish or reset the current flow first"
            )
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
            self_payment_indices=self_payment_indices,
        )
        self._pending = pending
        self._state = TxFlowStatus.CREATED
        # A new flow starts from a clean record (invariant: a transition
        # into a non-terminal state never inherits a previous tx's state).
        self._confirmed = None
        self._signed = None
        self._txid = None
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
            confirmed — the Phase 3 signer handoff starts here). The flow
            RETAINS it (``TxFlow.confirmed``) through ``CONFIRMED``,
            ``SIGNED`` and ``BROADCAST``: the signer handoff and the
            broadcast-time re-validation build their intent from the exact
            record the user confirmed.

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
        self._confirmed = pending
        self._signed = None
        self._txid = None
        return pending

    def cancel(self) -> PendingTx:
        """Cancel the pending transaction (CREATED → CANCELLED).

        Deliberately CREATED-only: cancellation is a chat-level decision
        about a *staged* transaction. Past CONFIRMED the user's decision is
        committed, and a signed transaction is committed to signing — there
        is no chat-level undo (``SIGNED``/``BROADCAST`` leave the flow only
        via :meth:`reset` after the lifecycle ends). Recovery from any
        failure past signing is a fresh flow, never a silent rewind.

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

    def mark_signed(self, tx_ref: str, signed_psbt_base64: str) -> SignedTx:
        """Record the signed PSBT from the device handoff (CONFIRMED → SIGNED).

        Phase 3 transition (TCK-P3-004, ADR-0013 amendment). Preconditions,
        in order (every refusal leaves the state unchanged):

        1. state is ``CONFIRMED`` with a retained confirmed record — from
           any other state (``CREATED``, ``SIGNED`` — re-signing is
           refused, ``IDLE``, terminal) this is refused;
        2. ``tx_ref`` matches the confirmed transaction's reference;
        3. ``signed_psbt_base64`` is a non-empty string (value hygiene
           only — the flow NEVER parses or verifies PSBT bytes).

        NO utterance gate here, deliberately: the device interaction IS
        the user action (PROJECT.md §9 — the hardware-wallet screen is the
        trust anchor; the user physically approves the exact transaction
        on the device). The structural gates that remain are the matching
        ``tx_ref`` (intent routing cannot skip or reorder steps) and —
        downstream, at handler level — deterministic signed-PSBT
        re-validation before broadcast (TCK-P3-005).

        Args:
            tx_ref: The reference the model's ``sign_tx`` envelope quoted
                (must equal the confirmed one).
            signed_psbt_base64: The signed PSBT exactly as the signer
                gateway returned it.

        Returns:
            The immutable :class:`SignedTx` record (also exposed via
            ``TxFlow.signed``).

        Raises:
            FlowError: on any failed precondition; value-free messages.
        """
        if self._state is not TxFlowStatus.CONFIRMED or self._confirmed is None:
            raise FlowError("no confirmed transaction to sign")
        if tx_ref != self._confirmed.tx_ref:
            raise FlowError("tx_ref does not match the confirmed transaction")
        if not isinstance(signed_psbt_base64, str) or not signed_psbt_base64:
            raise FlowError("signed psbt must be a non-empty string")
        signed = SignedTx(tx_ref=self._confirmed.tx_ref, psbt_base64=signed_psbt_base64)
        self._signed = signed
        self._state = TxFlowStatus.SIGNED
        return signed

    def broadcast(self, tx_ref: str, txid: str) -> str:
        """Record the broadcast result (SIGNED → BROADCAST, terminal).

        Phase 3 transition (TCK-P3-004, ADR-0013 amendment). Preconditions,
        in order (every refusal leaves the state unchanged):

        1. state is ``SIGNED`` with a retained signed record — broadcast
           from ``CONFIRMED`` is refused (an un-revalidated, unsigned
           transaction must never reach the network; there is NO skip path
           past the signing state);
        2. ``tx_ref`` matches the signed record's reference;
        3. ``txid`` is a non-empty string (value hygiene — the shape
           authority for a txid is the chain layer, which re-validates the
           broadcast response as 64 lowercase hex before it can reach
           here).

        The handler-level duty this transition presupposes: the signed PSBT
        has already passed deterministic re-validation against the intended
        transaction (:mod:`localwallet.tx.revalidate`) — a mismatch there
        is a hard stop that never reaches :meth:`broadcast`.

        Args:
            tx_ref: The reference the model's ``broadcast_tx`` envelope
                quoted (must equal the signed one).
            txid: The transaction id verbatim from the chain layer's
                broadcast response.

        Returns:
            The recorded ``txid`` (also exposed via ``TxFlow.txid``).

        Raises:
            FlowError: on any failed precondition; value-free messages.
        """
        if self._state is not TxFlowStatus.SIGNED or self._signed is None:
            raise FlowError("no signed transaction to broadcast")
        if tx_ref != self._signed.tx_ref:
            raise FlowError("tx_ref does not match the signed transaction")
        if not isinstance(txid, str) or not txid:
            raise FlowError("txid must be a non-empty string")
        self._txid = txid
        self._state = TxFlowStatus.BROADCAST
        return txid

    def reset(self) -> None:
        """Return a terminal flow to IDLE (CONFIRMED/SIGNED/BROADCAST/
        CANCELLED/EXPIRED → IDLE).

        A no-op from IDLE. From CREATED this is refused — leaving a live
        pending without an explicit confirm/cancel decision would be the
        kind of silent step the flow exists to prevent (use
        :meth:`cancel`). All per-transaction records (confirmed, signed,
        txid) are cleared with the state.

        Raises:
            FlowError: state is CREATED.
        """
        if self._state is TxFlowStatus.CREATED:
            raise FlowError("a transaction is already pending — confirm or cancel it first")
        self._state = TxFlowStatus.IDLE
        self._pending = None
        self._confirmed = None
        self._signed = None
        self._txid = None
