"""Background watch support: mempool time-since-block + incoming-tx polling.

Phase 5 (TCK-P5-001): ``watch_incoming`` polling loop and the mempool
time-since-block helper used for narration ("last block ~7 min ago").

This module owns the *deterministic* polling and narration-fact logic. The
LLM is NOT in the polling loop — detection is plain code (AGENTS.md). It
performs no network I/O itself: the probe and the chain client are injected
(so tests drive cycles with a test double, never the network), and the single
config-selected :class:`~localwallet.chain.esplora.EsploraClient` is reused
(ADR-0018) by the production probe.

Design (pinned in ADR-0019): **single-threaded / tick-driven**. The watcher
exposes :meth:`IncomingWatcher.tick` which runs exactly ONE poll cycle
synchronously and returns the events produced that cycle (the notification
"events list" the caller — the CLI — drains and prints between user turns).
There is no background thread, so no sqlite object is ever shared across
threads (the store keeps one connection per :class:`~localwallet.store.Store`).
:meth:`IncomingWatcher.poll_due` gates when a caller should run ``tick`` using
the configured interval and an injectable clock, implementing the
``watch_interval_s`` semantics (``0`` = off) in the single thread.

Value discipline: every fact a narration prints (txid, address, amount,
height) is quoted verbatim from tool output and is *user-facing UI* (the
required exception to the no-addresses/amounts rule — PROJECT.md §7.8);
nothing here logs or embeds a value in an error string. All failures are
value-free.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from localwallet.chain.esplora import ChainError, EsploraClient

__all__ = [
    "IncomingEvent",
    "IncomingWatcher",
    "WatchedTx",
    "time_since_last_block",
]

#: A watched transaction observed by the probe. Every field is tool output
#: (verbatim); the narration quotes them, never generates them.
@dataclass(frozen=True, slots=True)
class WatchedTx:
    """One wallet transaction the probe observed, shaped for watch detection.

    ``incoming`` — True when the transaction pays one of the wallet's
    watched addresses (the scan's ``in`` or ``self`` direction). ``confirmed``
    — True when it has a block height. ``address`` / ``amount_sats`` — the
    watched address that received funds and the total received (verbatim
    tool output), or ``None`` when the transaction paid no watched address.
    """

    txid: str
    incoming: bool
    confirmed: bool
    height: int | None
    block_time: int | None
    address: str | None
    amount_sats: int | None


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    """One surfacing event the CLI drains and narrates to the user.

    ``kind``:
      ``"received"`` — a brand-new incoming transaction for a watched
      address was observed (whether it is already confirmed or still in the
      mempool is carried by ``confirmed``);
      ``"confirmed"`` — a previously-surfaced incoming transaction confirmed
      (its block height is carried by ``height``).

    Every value is quoted verbatim from tool output (P2-004 FACTS pattern).
    These values are deliberately shown to the user in the UI — they are
    never logged or passed to the model.
    """

    kind: Literal["received", "confirmed"]
    txid: str
    address: str
    amount_sats: int
    confirmed: bool
    height: int | None = None
    block_time: int | None = None


def time_since_last_block(
    client: EsploraClient, *, now: float | None = None
) -> int | None:
    """Integer seconds since the last block's timestamp; None when unavailable.

    The tip timestamp comes from the configured backend
    (:meth:`EsploraClient.get_tip_block` — tolerant of the mempool.space
    ``/blocks/tip`` divergence, HANDOFF §5). Returns:

    - ``None`` when the backend exposed no timestamp (clean unavailable state)
      or the tip lookup failed — never a crash, never a fabricated value;
    - otherwise ``int(now - timestamp)`` clamped at ``0`` (a block is never
      "in the future" meaningfully; clock skew must not produce a negative
      "last block ~-3 min ago").

    Value-free: the timestamp is never echoed; the caller narrates only the
    integer seconds.
    """
    try:
        tip = client.get_tip_block()
    except ChainError:
        return None
    if tip.timestamp is None:
        return None
    seconds = int((time.time() if now is None else now) - tip.timestamp)
    return max(0, seconds)


#: The probe signature: run one chain refresh and return the wallet's
#: observed transactions. In production the app wires this to a scan over the
#: single EsploraClient (ADR-0018) plus store reads; tests inject a fake.
Probe = Callable[[], Sequence[WatchedTx]]


class IncomingWatcher:
    """Tick-driven incoming-transaction poller (single-threaded, ADR-0019).

    One :meth:`tick` runs a poll cycle against the injected probe, diffs the
    result against the poller's own in-process memory of what has already
    been surfaced, and returns the new :class:`IncomingEvent` objects to
    surface. Dedup and confirmed-transition are deterministic in-memory state
    (process-scoped, mirroring the send-flow's session state) — no store
    schema change is needed.

    Args:
        probe: Zero-argument callable returning the wallet's observed
            transactions (test double in tests; the production scan+store
            reader in the app). Must not raise on a transient chain failure
            — the caller decides error handling.
        interval_s: Seconds between poll cycles; ``<= 0`` disables watching
            (:meth:`poll_due` always returns False and ``tick`` is inert).
        clock: Time source for interval gating (``time.time`` by default;
            injectable for deterministic tests).

    Thread model (pinned): this class spawns NO thread and shares no sqlite
    object across threads. ``tick`` runs on the caller's thread.
    """

    def __init__(
        self,
        probe: Probe,
        *,
        interval_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._probe = probe
        self._interval_s = interval_s
        self._clock = clock
        #: txid -> confirmed flag of the LAST state we surfaced for it.
        self._seen: dict[str, bool] = {}
        #: Persistent-failure visibility (NOTE-1): True while the most recent
        #: poll cycle failed. The app surfaces a throttled, value-free
        #: "check failed" line ONCE per failure streak (reset on the next
        #: successful poll, which also answers once via
        #: :meth:`mark_poll_succeeded`, so the streak's end gets a single
        #: symmetric "recovered" line). Never logged.
        self._poll_failed: bool = False
        #: When the last poll ran (or, before any poll, when this watcher was
        #: built). Seeded at construction so the FIRST poll is not forced on
        #: immediately — a fast session never scans, and an idle gap of at
        #: least one interval triggers the next poll (ADR-0019 single-threaded
        #: gating).
        self._last_tick_at: float = self._clock()

    @property
    def enabled(self) -> bool:
        """True when watching is on (interval > 0)."""
        return self._interval_s > 0

    @property
    def interval_s(self) -> float:
        """The RESOLVED poll interval in seconds (``<= 0`` = off).

        TCK-UX-012(c): the resolution (env > stored setting > default) lives
        at the watcher build site; this reader exposes the single resolved
        value the watcher itself gates on, so the app's failure narration can
        name the deterministic retry delay honestly. Value-free: a configured
        timing number only — never a wallet value.
        """
        return self._interval_s

    def poll_due(self, *, now: float | None = None) -> bool:
        """True when an interval has elapsed since the last poll (or startup).

        When watching is disabled (``interval_s <= 0``) this is always False.
        """
        if not self.enabled:
            return False
        current = self._clock() if now is None else now
        return current - self._last_tick_at >= self._interval_s

    def mark_poll_failed(self) -> bool:
        """Record that a poll cycle failed; True when it STARTS a failure streak.

        Idempotent within a streak: a subsequent failed poll (no intervening
        success) returns False, so the caller surfaces the throttled
        persistent-failure line exactly once per streak (NOTE-1).
        """
        if self._poll_failed:
            return False
        self._poll_failed = True
        return True

    def mark_poll_succeeded(self) -> bool:
        """Record that a poll cycle succeeded; True when it ENDED a failure streak.

        TCK-UX-012(c): the streak-end edge answer lets the caller surface one
        throttled, value-free "recovered" line per streak (symmetric to the
        once-per-streak failure line of :meth:`mark_poll_failed`); subsequent
        successes report False, so a healthy poll never prints it.
        """
        ended = self._poll_failed
        self._poll_failed = False
        return ended

    def tick(self) -> list[IncomingEvent]:
        """Run exactly one poll cycle and return the events to surface.

        Idempotent by design: a transaction already surfaced is not re-surfaced,
        and an unconfirmed→confirmed transition is surfaced exactly once.
        Calling ``tick`` does not sleep and is deterministic for a given probe
        result — tests drive cycles synchronously with a test double.
        """
        self._last_tick_at = self._clock()
        if not self.enabled:
            return []
        events: list[IncomingEvent] = []
        for tx in self._probe():
            if not tx.incoming:
                continue
            previous = self._seen.get(tx.txid)
            if previous is None:
                # Brand-new incoming transaction: surface once (received).
                self._seen[tx.txid] = tx.confirmed
                if tx.address is not None and tx.amount_sats is not None:
                    events.append(
                        IncomingEvent(
                            kind="received",
                            txid=tx.txid,
                            address=tx.address,
                            amount_sats=tx.amount_sats,
                            confirmed=tx.confirmed,
                            height=tx.height,
                            block_time=tx.block_time,
                        )
                    )
            elif previous is False and tx.confirmed:
                # Unconfirmed -> confirmed transition: surface once.
                self._seen[tx.txid] = True
                if tx.address is not None and tx.amount_sats is not None:
                    events.append(
                        IncomingEvent(
                            kind="confirmed",
                            txid=tx.txid,
                            address=tx.address,
                            amount_sats=tx.amount_sats,
                            confirmed=True,
                            height=tx.height,
                            block_time=tx.block_time,
                        )
                    )
        return events
